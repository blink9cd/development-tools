"""Filesystem primitives: atomic writes, hashing, bounded reads, the run lock, a rooted reader.

The temp-then-replace itself is the shared ``atomic_write`` library's; the two
writers here only add this tool's parent-directory creation and its mode.

The regular-file reader lives here, beside the other primitives, because two feature modules
use it -- an archive member and a promoted manifest are each read beneath a root -- and
neither should import the other to reach it.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import sys
import threading
from collections.abc import Callable
from pathlib import Path

# atomic_write, the shared atomic file replacement, at ONE fixed place: its folder directly
# under the repository root, as backup_proc.py places bounded_run.
sys.path.append(str(Path(__file__).resolve().parents[2] / "atomic_write"))

import atomic_write  # noqa: E402 - import follows the sys.path bootstrap above
from backup_json import is_safe_relative_name  # noqa: E402 - same bootstrap

# flock(2) is POSIX-only, and the tool refuses any platform but macOS. The split is an
# ``if``, not a module-level ``assert``: a type check is also run for win32, and such an
# assert makes that pass skip everything below it, so every module importing this one then
# fails there on attributes it can no longer see. The win32 arm exists only for that pass.
if sys.platform == "win32":

    def _flock(fd: int, operation: int) -> None:  # pragma: only-on win32
        """Refuse: Windows has no flock(2), and this tool never runs there."""
        raise OSError(errno.ENOSYS, "RunLock needs flock(2), which Windows lacks")

    _LOCK_EXCLUSIVE_NOWAIT = _LOCK_RELEASE = 0  # pragma: only-on win32
    _OPEN_PROBE = os.O_RDONLY  # pragma: only-on win32
    _OPEN_LOCK = os.O_RDWR | os.O_CREAT  # pragma: only-on win32
else:  # pragma: only-on posix
    from fcntl import LOCK_EX, LOCK_NB, LOCK_UN
    from fcntl import flock as _flock

    _LOCK_EXCLUSIVE_NOWAIT = LOCK_EX | LOCK_NB
    _LOCK_RELEASE = LOCK_UN
    #: A probe opens the lock read-only, and never waits in open() on a FIFO put in its place.
    #: Neither it nor a holder opens the lock through a symlink planted in its place.
    _OPEN_PROBE = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
    _OPEN_LOCK = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW

#: Chunk size for streaming a file into a digest without loading it whole.
_HASH_CHUNK = 1 << 20
#: The mode every atomic write gets, set explicitly so it depends on neither a staging
#: file's own mode nor the caller's umask: it is what a plain open gave under the usual umask,
#: and launchd refuses a plist anyone but its owner can write.
_WRITE_MODE = 0o644
#: What the tool's own directory, and every directory beneath it, is made with: owner-only,
#: since staging holds a build in progress, every backed-up file included.
PRIVATE_DIR_MODE = 0o700
#: What each state file under that directory is written with.
PRIVATE_FILE_MODE = 0o600

ReadBytesFn = Callable[[Path], bytes]


def atomic_write_text(path: Path, text: str, *, private: bool = False) -> None:
    """Write *text* to *path* via temp-then-replace, so a reader never sees half a file."""
    # Encoded here rather than written in text mode, so recorded state is byte-identical
    # whatever the host's newline: the UTF-8 bytes are exactly what an LF text write gave.
    atomic_write_bytes(path, text.encode("utf-8"), private=private)


def write_private_text(path: Path, text: str) -> None:
    """:func:`atomic_write_text` for a state file: owner-only, as are the folders it makes."""
    atomic_write_text(path, text, private=True)


def atomic_write_bytes(path: Path, data: bytes, *, private: bool = False) -> None:
    """The bytes twin of :func:`atomic_write_text`, and the one write body both use.

    The parent directory is created first; the replace is ``atomic_write``'s, at
    ``_WRITE_MODE``: staged beside *path* under a name nothing can predict, opened
    exclusively, fsynced and renamed. *private* makes each missing parent
    ``PRIVATE_DIR_MODE`` and the file ``PRIVATE_FILE_MODE``. A failure removes the staging
    file and re-raises, leaving *path* untouched.
    """
    if private:
        make_private_dirs(path.parent)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    mode = PRIVATE_FILE_MODE if private else _WRITE_MODE
    atomic_write.replace_bytes(path, data, mode=mode)


def make_private_dirs(path: Path) -> None:
    """Create *path* and each missing parent ``PRIVATE_DIR_MODE``; an existing one is kept.

    One at a time, because ``mkdir(parents=True)`` gives the parents it makes the default
    mode rather than the one asked for.
    """
    missing: list[Path] = []
    directory = path
    # Ends at the root at the latest, which always exists.
    while not directory.exists():
        missing.append(directory)
        directory = directory.parent
    for directory in reversed(missing):
        directory.mkdir(mode=PRIVATE_DIR_MODE, exist_ok=True)


def sha256_bytes(data: bytes) -> str:
    """Hex sha256 of *data*."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    """Hex sha256 of the file at *path*, streamed in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_bytes_bounded(
    path: Path, timeout: float, *, reader: ReadBytesFn = Path.read_bytes
) -> "bytes | None":
    """Read *path* on a watchdog thread; None when the read fails or outlives *timeout*.

    iCloud materializes an offloaded file on first read, which can block on the network
    with no timeout parameter to pass -- so the wait is bounded here instead. An abandoned
    read keeps its daemon thread until the read returns; the caller has already moved on.
    """
    result: list[bytes] = []

    def _read() -> None:
        try:
            result.append(reader(path))
        except OSError:
            # Unreadable is reported to the caller as None below; nothing to add here.
            return

    worker = threading.Thread(target=_read, daemon=True)
    worker.start()
    worker.join(timeout)
    return result[0] if result else None


class RunLock:
    """An exclusive, non-blocking ``flock`` held for the life of one run.

    ``flock`` locks belong to the open file description, so a second ``RunLock`` on the same
    path -- in this process or another -- is refused until the first releases.
    """

    def __init__(self, path: Path) -> None:
        """Bind the lock to *path*; nothing is opened until :meth:`acquire`."""
        self._path = path
        self._fd: "int | None" = None

    def acquire(self) -> bool:
        """Take the lock; False, without waiting, when another holder has it.

        Any other error closes the descriptor before it is raised: a failed attempt holds
        nothing open.
        """
        make_private_dirs(self._path.parent)
        fd = os.open(self._path, _OPEN_LOCK, PRIVATE_FILE_MODE)
        try:
            _flock(fd, _LOCK_EXCLUSIVE_NOWAIT)
        except BlockingIOError:
            os.close(fd)
            return False
        except OSError:
            os.close(fd)
            raise
        self._fd = fd
        return True

    def release(self) -> None:
        """Drop the lock if held; the lock file itself stays for the next run.

        The descriptor is closed even when unlocking raises, and closing it drops the lock.
        """
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            _flock(fd, _LOCK_RELEASE)
        finally:
            os.close(fd)


def lock_is_held(path: Path) -> bool:
    """Whether another holder has :class:`RunLock`'s flock on *path*, asked without waiting.

    Nothing is created: every holder creates the lock file, so an absent one is free. The
    lock is taken without blocking and let go at once, and the descriptor is closed whatever
    happens. An existing file that cannot be opened -- no permission to read it -- raises
    OSError, since whether it is held is then unknown.
    """
    try:
        fd = os.open(path, _OPEN_PROBE)
    except FileNotFoundError:
        return False
    try:
        _flock(fd, _LOCK_EXCLUSIVE_NOWAIT)
    except BlockingIOError:
        return True
    else:
        _flock(fd, _LOCK_RELEASE)
        return False
    finally:
        os.close(fd)


# O_NOFOLLOW, O_NONBLOCK and set_blocking are POSIX-only. The split is an ``if``, as above:
# a type check is also run for win32, and the win32 arm exists only for that pass.
if sys.platform == "win32":

    def _open_regular(
        root: Path, rel: str
    ) -> "tuple[int, os.stat_result]":  # pragma: only-on win32
        """Refuse: a member is read with O_NOFOLLOW_ANY or O_NOFOLLOW, which Windows lacks."""
        raise OSError(errno.ENOSYS, "reading a member needs O_NOFOLLOW")

else:  # pragma: only-on posix
    #: O_NOFOLLOW_ANY refuses a symlink in any component of the path it is given, not only
    #: the last. The walk lists a directory and the read opens its files afterwards, so a
    #: middle directory swapped for a symlink in that window would otherwise be followed
    #: out of the root. Read by getattr: typeshed declares it for darwin alone, and an
    #: interpreter without it falls back to guarding only the last component.
    _NO_SYMLINK_COMPONENT: int = getattr(os, "O_NOFOLLOW_ANY", os.O_NOFOLLOW)
    #: O_NONBLOCK keeps open() on a swapped-in FIFO from waiting forever for a writer.
    _OPEN_MEMBER = os.O_RDONLY | _NO_SYMLINK_COMPONENT | os.O_NONBLOCK
    #: The root is configuration, so it alone is opened following links.
    _OPEN_ROOT = os.O_RDONLY | os.O_DIRECTORY

    def _open_regular(root: Path, rel: str) -> "tuple[int, os.stat_result]":
        """A descriptor reading *rel* under *root*, and its fstat; OSError unless regular.

        *rel* is opened relative to a descriptor on *root*, because O_NOFOLLOW_ANY given an
        absolute path refuses a symlink above the root as well -- ``/var`` on macOS, or a
        symlinked root. Non-blocking only until the type is known; the flag is then
        cleared, so the read is an ordinary blocking one and the caller's watchdog bounds it.

        *rel* must be a safe relative name, refused before anything is opened: ``dir_fd`` is
        ignored for an absolute path, and O_NOFOLLOW_ANY does not refuse a ``..`` component,
        so either would read a regular file outside the root.
        """
        if not is_safe_relative_name(rel):
            raise OSError(errno.EINVAL, f"not a safe name beneath the root: {rel!r}")
        root_fd = os.open(root, _OPEN_ROOT)
        try:
            fd = os.open(rel, _OPEN_MEMBER, dir_fd=root_fd)
        finally:
            os.close(root_fd)
        try:
            status = os.fstat(fd)
            if not stat.S_ISREG(status.st_mode):
                raise OSError(errno.EINVAL, f"not a regular file: {rel}")
            os.set_blocking(fd, True)
        except OSError:
            os.close(fd)
            raise
        return fd, status


def read_regular_file(
    root: Path, rel: str, *, limit: "int | None" = None
) -> "tuple[bytes, os.stat_result]":
    """The bytes of the regular file *rel* under *root*, and the fstat they were read under.

    OSError when *rel* is not a safe relative name, is a symlink, passes through one (where
    O_NOFOLLOW_ANY exists), or is a FIFO, a socket, a directory or unreadable, and when it
    holds more than *limit* bytes; without *limit*, it reads to end of file. Unbounded in time:
    a caller that needs that bound runs it through :func:`read_bytes_bounded`.
    """
    fd, status = _open_regular(root, rel)
    with os.fdopen(fd, "rb") as handle:
        # One byte past the limit is enough to tell an over-long file, without reading it.
        data = handle.read() if limit is None else handle.read(limit + 1)
    if limit is not None and len(data) > limit:
        raise OSError(errno.EFBIG, f"{rel} holds more than {limit} bytes")
    return data, status
