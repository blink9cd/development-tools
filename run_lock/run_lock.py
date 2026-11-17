"""Hold an exclusive OS lock on a file for a process's lifetime, and probe whether one is held.

Why this exists
---------------
A scheduled job that must never run twice, and a status reader that must tell a RUNNING job
from a DEAD one, need the same primitive: a lock the kernel releases when its holder dies,
SIGKILL included. With it, "the record says in progress and the lock is free" means the holder
died, with no process-ID guesswork. Process IDs recycle fast enough on a busy machine that a
recorded one proves nothing an hour later.

``flock(2)`` on POSIX and ``msvcrt.locking`` on Windows both have that property. Each lock
belongs to the OPEN FILE rather than the process, so a second :func:`acquire` of the same path
is refused even inside the process that holds the first.

Windows frees a dead holder's lock after the process exits rather than as it exits, so there a
probe made just after a kill can still read held for a moment. A free lock still proves its
holder is gone; only a held one can briefly outlive it.

The lock file is never deleted. Unlinking it races a concurrent opener: the second process can
lock a file the first has just removed, and two holders then each own "the" lock.

``private=True`` is for a caller keeping private state: the lock path's last component is
opened with ``O_NOFOLLOW``, so a symlink planted there is refused rather than followed to
whatever it names, what is created belongs to the running user alone, and a parent or lock file
that is not that user's own is refused rather than trusted. ``O_NOFOLLOW`` is POSIX-only, so
on Windows that request raises ENOSYS rather than quietly giving less than it asked for.

Entry point assumptions
-----------------------
Import-only: no CLI and no output. Standard library only. Paths come from the caller.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
from pathlib import Path
from types import TracebackType

# An ``if``, not a module-level assert: mypy can check this file once per platform (win32,
# linux and darwin), and an assert would hide every name below it from the other passes.
if sys.platform == "win32":
    import msvcrt  # pragma: only-on win32

    #: A probe needs only read access; LockFile accepts a read-only handle.
    _OPEN_PROBE = os.O_RDONLY  # pragma: only-on win32

    def _try_lock(fd: int) -> bool:  # pragma: only-on win32
        """Lock the file's first byte without waiting; False when another handle holds it.

        ``msvcrt.locking`` locks from the CURRENT position, so the position is reset first.
        Locking past the end of an empty file is allowed, and contention raises EACCES.
        """
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno == errno.EACCES:
                return False
            raise
        return True

    def _unlock(fd: int) -> None:  # pragma: only-on win32
        """Release the byte :func:`_try_lock` locked."""
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

    def _no_follow() -> int:  # pragma: only-on win32
        """Refuse: a private lock is opened with O_NOFOLLOW, which Windows lacks."""
        raise OSError(errno.ENOSYS, "a private lock needs O_NOFOLLOW")

    def _euid() -> int:  # pragma: only-on win32
        """Refuse: never reached, since a private lock is refused on Windows first."""
        raise OSError(errno.ENOSYS, "a private lock needs POSIX ownership")

else:  # pragma: only-on posix
    import fcntl

    #: Read-only, and never blocking in open() on a FIFO planted where the lock file should be.
    _OPEN_PROBE = os.O_RDONLY | os.O_NONBLOCK

    def _try_lock(fd: int) -> bool:
        """Take ``flock`` without waiting; False when another open file holds it."""
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _unlock(fd: int) -> None:
        """Release the ``flock`` :func:`_try_lock` took."""
        fcntl.flock(fd, fcntl.LOCK_UN)

    def _no_follow() -> int:
        """The open flag that refuses a symlink as the path's last component."""
        return os.O_NOFOLLOW

    def _euid() -> int:
        """This process's effective user: the user a private lock's files must belong to."""
        return os.geteuid()


class Lock:
    """A held lock. Leave the ``with`` block, or call :meth:`release`, to drop it."""

    def __init__(self, fd: int) -> None:
        """Wrap *fd*, an open descriptor this process has already locked."""
        self._fd: "int | None" = fd

    def release(self) -> None:
        """Drop the lock if still held. The descriptor is closed even when unlocking raises."""
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            _unlock(fd)
        finally:
            os.close(fd)

    def __enter__(self) -> Lock:
        """Return the lock itself, already held."""
        return self

    def __exit__(
        self,
        exc_type: "type[BaseException] | None",
        exc: "BaseException | None",
        traceback: "TracebackType | None",
    ) -> None:
        """Release on the way out, whether or not the block raised."""
        self.release()


#: The lock file's mode when :func:`acquire` creates it, unless the caller asks for another.
_DEFAULT_MODE = 0o644
#: What ``private=True`` creates: the lock file, and each parent directory it has to make.
_PRIVATE_FILE_MODE = 0o600
_PRIVATE_DIR_MODE = 0o700
#: The group and other permission bits, none of which a private directory may carry.
_SHARED_BITS = 0o077
#: Take: read-write, creating the file when absent.
_OPEN_LOCK = os.O_RDWR | os.O_CREAT


def acquire(
    path: Path, *, mode: "int | None" = None, private: bool = False
) -> "Lock | None":
    """Take the lock on *path* without waiting; None when another holder has it.

    Creates *path* and its parent directories when absent, the file with *mode* (0o644 when
    omitted, less the umask; an existing file keeps its own). Any failure other than
    contention (an unwritable directory, a path that is a directory) raises OSError after the
    descriptor is closed, so a failed attempt holds nothing open.

    *private* refuses a symlink at *path* with an OSError (ELOOP on Linux and macOS; the errno
    is the platform's), creates the file 0o600 and each missing parent 0o700 (less the umask;
    an existing parent keeps its mode), and refuses with OSError a parent that someone else
    put in its place first, or an existing lock file that is not this user's own regular file
    with a single link. It sets the mode itself, so ANY *mode* beside it, even 0o644, raises
    ValueError. POSIX-only: on Windows it raises OSError (ENOSYS) before anything is created
    or opened.
    """
    if private and mode is not None:
        raise ValueError("private=True sets the lock file's mode; do not pass mode too")
    if private:
        flags = _OPEN_LOCK | _no_follow()
        _make_private_dirs(path.parent)
        mode = _PRIVATE_FILE_MODE
    else:
        flags = _OPEN_LOCK
        path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, flags, _DEFAULT_MODE if mode is None else mode)
    try:
        if private:
            _refuse_unless_own_file(fd, path)
        held = _try_lock(fd)
    except OSError:
        os.close(fd)
        raise
    if not held:
        os.close(fd)
        return None
    return Lock(fd)


def _make_private_dirs(path: Path) -> None:
    """Create *path* and each missing parent ``_PRIVATE_DIR_MODE``; an existing one is kept.

    One at a time, because ``mkdir(parents=True)`` gives the parents it makes the default
    mode rather than the one asked for. A parent found missing that someone else creates
    before this mkdir does is accepted only as this user's own private directory (see
    :func:`_refuse_unless_own_dir`); without that, another user's directory, or a symlink,
    planted in the gap would silently stand in for the private one.
    """
    missing: list[Path] = []
    directory = path
    # Ends at the root at the latest, which always exists.
    while not directory.exists():
        missing.append(directory)
        directory = directory.parent
    for directory in reversed(missing):
        try:
            os.mkdir(directory, _PRIVATE_DIR_MODE)
        except FileExistsError:
            _refuse_unless_own_dir(directory)


def _refuse_unless_own_dir(directory: Path) -> None:
    """Raise OSError unless *directory* is a real directory, ours, closed to everyone else.

    Its exact mode cannot be demanded, since what this process makes is 0o700 less the umask;
    no group or other bit at all is the property that matters.
    """
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode):
        reason = "is not a real directory"
    elif info.st_uid != _euid():
        reason = "is owned by another user"
    elif info.st_mode & _SHARED_BITS:
        reason = "is open to other users"
    else:
        return
    raise OSError(errno.EPERM, f"private lock parent {reason}", str(directory))


def _refuse_unless_own_file(fd: int, path: Path) -> None:
    """Raise OSError unless *fd* is a regular file this user owns, with a single link.

    Read from the open descriptor, so it describes the very file about to be locked. Another
    user's file, or a hard link to one they can open, would let them hold the lock forever.
    """
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        reason = "is not a regular file"
    elif info.st_uid != _euid():
        reason = "is owned by another user"
    elif info.st_nlink > 1:
        reason = "has more than one link"
    else:
        return
    raise OSError(errno.EPERM, f"private lock file {reason}", str(path))


def is_held(path: Path, *, private: bool = False) -> bool:
    """Whether another holder has the lock on *path*, asked without waiting or creating it.

    An absent file is free, because every holder creates it. The probe takes the lock and lets
    it go at once, so it can briefly contend with a real :func:`acquire`, which then returns
    None: the same answer as losing to any other holder. An existing file that cannot be opened
    raises OSError, since whether it is held is then unknown.

    *private* refuses with OSError a symlink at *path*, even one naming nothing (ELOOP on Linux
    and macOS), and an existing file that is not this user's own regular file with a single
    link. POSIX-only: on Windows it raises OSError (ENOSYS) before anything is opened.
    """
    flags = (_OPEN_PROBE | _no_follow()) if private else _OPEN_PROBE
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return False
    try:
        if private:
            _refuse_unless_own_file(fd, path)
        if not _try_lock(fd):
            return True
        _unlock(fd)
        return False
    finally:
        os.close(fd)
