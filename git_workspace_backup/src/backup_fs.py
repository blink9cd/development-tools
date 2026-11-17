"""Filesystem primitives: atomic writes, hashing, bounded reads, a rooted reader.

The temp-then-replace itself is the shared ``atomic_write`` library's; the two
writers here only add this tool's parent-directory creation and its mode.

The regular-file reader lives here, beside the other primitives, because two feature modules
use it (an archive member and a promoted manifest are each read beneath a root), and
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

# atomic_write, the shared atomic file replacement, at one fixed place: its folder directly
# under the repository root, as backup_proc.py places bounded_run.
sys.path.append(str(Path(__file__).resolve().parents[2] / "atomic_write"))

import atomic_write  # noqa: E402 - import follows the sys.path bootstrap above
from backup_json import is_safe_relative_name  # noqa: E402 - same bootstrap

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
    with no timeout parameter to pass, so the wait is bounded here instead. An abandoned
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


# O_NOFOLLOW, O_NONBLOCK and set_blocking are POSIX-only. The split is an ``if``, not a
# module-level ``assert``: a type check is also run for win32, and such an assert makes that
# pass skip everything below it, so every module importing this one then fails there on
# attributes it can no longer see. The win32 arm exists only for that pass.
if sys.platform == "win32":

    def _open_regular(
        root: Path, rel: str
    ) -> "tuple[int, os.stat_result]":  # pragma: only-on win32
        """Refuse: a member is read with O_NOFOLLOW_ANY or O_NOFOLLOW, which Windows lacks."""
        raise OSError(errno.ENOSYS, "reading a member needs O_NOFOLLOW")

else:  # pragma: only-on posix
    #: O_NOFOLLOW_ANY refuses a symlink in any component of the path it is given, not only
    #: the last. The walk lists a directory and the read opens its files afterward, so a
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
        absolute path refuses a symlink above the root as well: ``/var`` on macOS, or a
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


#: Reads *count* bytes from a descriptor; ``os.read`` but for tests.
ReadFdFn = Callable[[int, int], bytes]
#: The file flag macOS sets on a file iCloud evicted, leaving its name and no data
#: (``SF_DATALESS`` in sys/stat.h). The stat module names it only from Python 3.13, so the
#: header's value stands in on darwin; elsewhere no file carries it.
_DARWIN_SF_DATALESS = 0x40000000
SF_DATALESS: int = getattr(
    stat, "SF_DATALESS", _DARWIN_SF_DATALESS if sys.platform == "darwin" else 0
)


class ReadStalled(Exception):
    """A bounded hash read no data within its stall bound, and was abandoned."""


class FileOffloaded(Exception):
    """The file is dataless: iCloud evicted its data, and reading it would fetch it first."""


class SizeChanged(Exception):
    """The opened file's size is not the one the caller expected, so it was never read."""

    def __init__(self, rel: str, size: int) -> None:
        """Name the file *rel* and the *size* its descriptor reported."""
        super().__init__(f"{rel} holds {size} bytes")
        #: The size the opened descriptor reported.
        self.size = size


class _HashProgress:
    """What a hashing worker has read so far, shared with the thread watching it."""

    def __init__(self) -> None:
        """An empty digest, no chunks yet, and the done and cancel flags clear."""
        self.digest = hashlib.sha256()
        self.size = 0
        self.chunks = 0
        self.error: "Exception | None" = None
        self.done = threading.Event()
        #: Set by the watcher when it gives up, so an abandoned worker stops between chunks.
        self.cancel = threading.Event()


def hash_regular_file_bounded(
    root: Path,
    rel: str,
    stall_s: float,
    *,
    expected_size: "int | None" = None,
    read: ReadFdFn = os.read,
) -> "tuple[str, int]":
    """Hex sha256 and size of the regular file *rel* under *root*, read in chunks on a watchdog.

    Opened as :func:`read_regular_file` opens it, so a symlink, a FIFO or anything but a
    regular file raises OSError, and so does an unsafe *rel*. A dataless file (iCloud evicted
    its data) raises FileOffloaded before any read, and so does a file whose descriptor reports
    a size other than *expected_size*, given one, with SizeChanged: a file swapped for a far
    larger one, a sparse one say, is then never read to its end. The bound is on progress, not on the
    whole read: a read that keeps returning data is never cut off, but one that yields no
    chunk within *stall_s*, as an iCloud file can while it is fetched, raises ReadStalled and
    tells the worker to stop at its next chunk. Whatever the worker raised, OSError or not,
    is raised here, so a partial digest is never returned.
    """
    progress = _HashProgress()
    worker = threading.Thread(
        target=_hash_into,
        args=(root, rel, expected_size, read, progress),
        daemon=True,
    )
    worker.start()
    seen = 0
    # Each bound that passes with a new chunk read waits again; one that passes with none
    # gives up, unless the worker finished in the gap since the wait. The wait is the poll
    # interval, so no separate sleep is needed.
    while not progress.done.wait(stall_s):
        if progress.chunks == seen and not progress.done.is_set():
            progress.cancel.set()
            raise ReadStalled(f"{rel}: no data within {stall_s:g} s")
        seen = progress.chunks
    if progress.error is not None:
        raise progress.error
    return progress.digest.hexdigest(), progress.size


def _hash_into(
    root: Path,
    rel: str,
    expected_size: "int | None",
    read: ReadFdFn,
    progress: _HashProgress,
) -> None:
    """Hash *rel* under *root* into *progress*, recording any error and then setting done."""
    try:
        fd, status = _open_regular(root, rel)
        try:
            if getattr(status, "st_flags", 0) & SF_DATALESS:
                raise FileOffloaded(f"{rel} is in iCloud only: its data is not here")
            # The descriptor's own size, so no rename after the open can answer for it.
            if expected_size is not None and status.st_size != expected_size:
                raise SizeChanged(rel, status.st_size)
            _read_chunks(fd, read, progress)
        finally:
            os.close(fd)
    # Any error, not only OSError: it is raised again by the caller, never swallowed.
    except Exception as exc:
        progress.error = exc
    finally:
        progress.done.set()


def _read_chunks(fd: int, read: ReadFdFn, progress: _HashProgress) -> None:
    """Feed *fd* into *progress* until end of file, or until the watcher cancels."""
    while not progress.cancel.is_set():
        chunk = read(fd, _HASH_CHUNK)
        if not chunk:
            return
        progress.digest.update(chunk)
        progress.size += len(chunk)
        progress.chunks += 1
