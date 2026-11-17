#!/usr/bin/env python3
"""One atomic file replacement, for every tool that ships a file.

A file another process reads while its writer runs (a state file a later run
loads, a document a person opens) is exposed half-written by a
truncate-and-refill, which surfaces as an intermittent failure that never
reproduces on its own. Writing beside the target and renaming closes
the window: the old file stays whole until one indivisible step swaps in the
new one.

Why this module exists rather than the recipe being re-typed: it was written out
by hand in seven tools, and the copies had already drifted apart on every axis
that matters. Two fsynced and five did not, three set a mode and four left
`mkstemp`'s 0600, readable to nothing but the account that wrote it, to be
renamed into place, and one cleaned up after itself not at all, so a failed
replace stranded a `.tmp` beside committed data. None of those differences was
a decision; each was whatever the tool in front of the author happened to do
that day.

What a caller gets, and why each part is here rather than optional:

* the staging file is created in the target's own directory, because
  `os.replace` is atomic only within one filesystem: across a mount boundary
  it quietly becomes a copy, which is the very window this closes;
* the bytes are fsynced before the rename, because the rename's atomicity says
  nothing about durability: a crash afterward could otherwise publish an empty
  file under the target's name;
* the mode is set on the descriptor rather than left to the umask, because
  `mkstemp` creates at 0600 and the rename carries that through, so a
  published file would be readable by the account that wrote it alone;
* a failure at any point removes the staging file and re-raises, so an
  interrupted build leaves the old file and nothing else.

A caller that must re-check its target as late as possible (a digest taken
when it read the file, compared again just before the swap) passes a
`before_rename` check. It runs on the finished staging file, after the write,
the mode and the fsync, so the only step left after it is the rename itself;
returning False vetoes the replace, and raising does the same and propagates.

A caller that must make a file once and never write over it (a secret generated on
first run, say) calls `create_bytes` instead. It finishes the staging file the same
way and then hard-links it to the target, which refuses any existing entry, so of
two writers racing to make the file exactly one lands; it needs hard links. A secret
must pass `mode=0o600`: the default, `SERVED_MODE`, is readable by every account.

The careful read is here too, since the same callers need it: a tool that acts on
a record another process wrote calls `read_bytes_nofollow`, which reads only from
the regular file the path itself names. A symbolic link, a Windows reparse point,
a folder, a pipe or a device is refused; so is a link or a non-file swapped in
after the check, a file of another identity where the volume reports file
numbers, and one over the byte cap. What it cannot refuse is on the function.

Entry point assumptions: a library, imported by the tools that write files;
there is no CLI here. Callers put this folder on `sys.path` from one fixed place
derived from their own file, as `stdio_utf8/stdio_utf8.md` describes.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Final, NamedTuple

#: The mode a generated file is published with: readable by whatever serves
#: the tree, writable only by the account that wrote it. `mkstemp` creates at
#: 0600 and `os.replace` carries the staging file's mode to the target, so
#: without this a served file arrives unreadable to the server. Set from the
#: descriptor, so no window exists in which the file is on disk under the
#: wrong mode.
SERVED_MODE = 0o644

#: Ask for the mode the target already carries, rather than imposing one. A file
#: that does not exist yet has none to keep and gets what a plain write would
#: have given it: `_NEW_FILE_MODE` filtered by the caller's umask. This is what
#: a plain `Path.write_text` does by construction, and what five of the seven
#: writers this module replaces did deliberately: forcing them onto a flat mode
#: would strip an execute bit from a file that had one and ignore the umask on a
#: file that had none. Spelled as None so the signature stays `int | None`, and
#: named so a call site says which policy it means.
PRESERVE: Final = None

#: What a plain `open(path, "wb")` asks the kernel for on a new file, before the
#: umask is subtracted. Handed to `os.open` so a file written for the first time
#: under `PRESERVE` gets the mode a direct write would have given it. Asking the
#: kernel for what a plain write asks for is what makes that umask-correct
#: without reading the umask, which `os.umask` allows only by setting it: a
#: process-wide race inside the caller's own run.
_NEW_FILE_MODE = 0o666

#: What a staging file is created with whenever a mode is then set on it: its
#: writer's alone. Created any wider, it could be opened by another account in
#: the moment before the wanted mode lands, and a descriptor outlives a chmod.
_PRIVATE_MODE = 0o600

#: What an unfinished write is called while it is unfinished. Leading dot so it
#: sorts out of the way and most tools ignore it; the target's own name in the
#: middle so a stranded one says what it was going to be.
#:
#: Public because a caller's suite pins it: several tools assert that the rename's
#: source sat beside its target under a name nothing could have planted in advance,
#: and that assertion has to name the suffix. Reading it from here rather than
#: repeating the string is what keeps those suites from pinning a spelling this
#: module is free to change.
STAGING_SUFFIX = ".partial"

#: The optional check a caller runs just before the rename: handed the finished
#: staging file, it answers whether the replace may go ahead. See `replace_bytes`.
BeforeRename = Callable[[Path], bool]

#: Bytes of randomness in a staging file's name: enough that nothing can plant a
#: path at it in advance, and `O_EXCL` refuses any that somehow exists.
_STAGING_NAME_BYTES = 8

#: Windows' `FILE_ATTRIBUTE_REPARSE_POINT`, spelled out because `stat` names it
#: only there. Set on a symbolic link, a junction, a mount point and every other
#: reparse point: each is a path that can hand a reader some other file.
REPARSE_POINT: Final = 0x400

#: `O_NOFOLLOW` where the platform has it (POSIX): the open itself then fails on a
#: link at the path's last component. Windows has none, which is why
#: `read_bytes_nofollow` also checks with `lstat` and `fstat` on every host. A
#: name of its own, read at each open, so a suite can patch it to 0 and measure
#: the host without it.
_O_NOFOLLOW: Final = getattr(os, "O_NOFOLLOW", 0)

#: How a host with `O_NOFOLLOW` says the open met a link at the path's last
#: component: `ELOOP` in POSIX, on Linux and on macOS, `EMLINK` on FreeBSD, and
#: `EFTYPE` on NetBSD (a number only some hosts have). `ELOOP` also means too
#: many links met on the way down the path, which the refusal's message allows
#: for: the open's error alone cannot say which of the two it was.
_LINK_AT_OPEN_ERRNOS: Final = frozenset(
    {errno.ELOOP, errno.EMLINK, getattr(errno, "EFTYPE", errno.ELOOP)}
)

#: The most one `os.read` is asked for. A read may return fewer bytes than asked,
#: so the loop continues either way; this only keeps a large cap from allocating
#: its whole size up front for a file of a few hundred bytes.
_READ_CHUNK_BYTES = 64 * 1024


class ReadRefused(OSError):
    """`read_bytes_nofollow` would not read what the path names.

    An `OSError`, so a caller that treats every unreadable file alike needs one
    `except`; its own class, so a caller that must tell "someone put something
    else here" from "the disk failed" can. A missing file is not a refusal: that
    stays `FileNotFoundError`, from the check or from the open.
    """


class NofollowRead(NamedTuple):
    """What `read_bytes_nofollow` read, and the status of the file it read from."""

    #: The file's whole contents, never more than the caller's cap.
    data: bytes
    #: `os.fstat` of the open descriptor, taken before the read: the size, times
    #: and identity of the file the bytes came from, not of whatever the path
    #: names by the time the caller looks.
    status: os.stat_result


def staging_name(target_name: str, token: str) -> str:
    """The name a staging file for `target_name` takes, with `token` as its random part.

    Public so a tool that must recognize a staging file (an ignore rule written
    into a directory these files land in, a suite scoping what a run may leave)
    derives the shape from here rather than spelling it a second time. Handed a
    glob wildcard for each part, it is the pattern: `staging_name("*", "*")`
    matches every staging file this module makes, so respelling the name here
    moves every such pattern with it.
    """
    return f".{target_name}.{token}{STAGING_SUFFIX}"


def _existing_mode(path: Path) -> int | None:
    """The permission bits `path` already carries, or None when it is new.

    `FileNotFoundError` alone: any other reason a stat fails is a reason the
    write is about to fail too, and belongs in the caller's `OSError` rather
    than being read quietly as "this file is new".
    """
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return None


def _stage(path: Path, creation_mode: int) -> tuple[int, Path]:
    """Open a staging file beside `path`; its descriptor and its own path.

    Beside it because `os.replace` is atomic only within one filesystem.
    `O_EXCL` refuses any path already at the name, a symlink included, so a
    served file can never be written through a planted link, and the random name
    leaves nothing to plant one at.

    `os.open`, not `tempfile.mkstemp`, which takes no mode: `creation_mode` is
    `_PRIVATE_MODE` for a file whose mode is set next, and `_NEW_FILE_MODE` only
    for a new file under `PRESERVE`, which must land where a plain write would
    have put it and keeps the mode it is created with, so is never wider than that.
    """
    staging = path.parent / staging_name(
        path.name, secrets.token_hex(_STAGING_NAME_BYTES)
    )
    # O_BINARY exists only on Windows, where a descriptor opened without it is in
    # text mode and the C runtime writes each "\n" as "\r\n" under a "wb" handle.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    return os.open(staging, flags, creation_mode), staging


def _set_mode(descriptor: int, mode: int) -> None:
    """Put `mode` on the open staging file, where the platform has modes to put.

    Nothing on Windows: it has no POSIX mode bits to carry, and `os.fchmod` only
    reached it in Python 3.13; below that the name is absent outright, so an
    unguarded call would raise `AttributeError` rather than the `OSError` a
    caller is told to expect.
    """
    if sys.platform == "win32":
        return
    os.fchmod(descriptor, mode)  # pragma: only-on posix


def replace_bytes(
    path: Path,
    data: bytes,
    *,
    mode: int | None = SERVED_MODE,
    before_rename: BeforeRename | None = None,
) -> bool:
    """Replace `path` with `data` in one step no reader can see half of.

    :param path: The file to replace. Its parent directory must exist.
    :param data: The whole new contents.
    :param mode: The mode the published file carries, or `PRESERVE` to keep
        the one the target already has; see `SERVED_MODE` and `PRESERVE`.
    :param before_rename: An optional check, handed the staging file once it
        holds its final bytes and mode and has been fsynced. False vetoes the
        replace; an exception it raises propagates. Either way the staging file
        is removed and `path` is left as it was.
    :returns: True when `path` was replaced, False when `before_rename` vetoed.
    :raises OSError: If staging, writing or renaming fails. The staging file is
        removed first, so a failure leaves the old contents and nothing else.
    """
    # Read before the staging file exists, so a target that vanishes mid-write
    # cannot be mistaken for one that was always new.
    wanted = _existing_mode(path) if mode is PRESERVE else mode
    descriptor, staging = _stage(
        path, _NEW_FILE_MODE if wanted is None else _PRIVATE_MODE
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            if wanted is not None:
                # On the descriptor, not the path: a mode set afterward would
                # leave a window in which the finished bytes are on disk under
                # the wrong mode, and would race anything watching the
                # directory. Set before the write for the same reason.
                _set_mode(handle.fileno(), wanted)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # As late as the check can run: everything that finishes the staging
        # file is behind it, so the window a caller's re-check leaves open is
        # the rename alone, the narrowest it can be without a lock.
        vetoed = before_rename is not None and not before_rename(staging)
        if not vetoed:
            os.replace(staging, path)
    except BaseException:
        # BaseException, not Exception: a Ctrl-C between the write and the
        # rename would otherwise strand the staging file, and the operator who
        # interrupted the build is exactly the person who then finds it.
        staging.unlink(missing_ok=True)
        raise
    if vetoed:
        staging.unlink(missing_ok=True)
    return not vetoed


def create_bytes(path: Path, data: bytes, *, mode: int = SERVED_MODE) -> None:
    """Create `path` holding `data`, whole, or refuse because something is there.

    The create-only twin of `replace_bytes`, for a file that must be made once and
    never written over: the staging file is finished exactly as there, then hard
    linked to `path`. A link refuses an existing name, so of two writers racing to
    make the same file exactly one lands and the other gets `FileExistsError`. A
    `before_rename` re-check could only narrow that race; the link closes it.

    :param path: The file to create. Its parent directory must exist.
    :param data: The whole contents.
    :param mode: The mode the new file carries: `SERVED_MODE`, readable by anyone,
        unless given, so a secret must pass `0o600`. No `PRESERVE`: nothing to keep.
    :raises FileExistsError: If anything is at `path`, a dangling symlink included.
        Nothing is followed and nothing at `path` is changed.
    :raises OSError: If staging, writing or linking fails, including a filesystem
        with no hard links (FAT, exFAT, some SMB shares). There is no fallback,
        since a plain write could not refuse an existing file. When the link is
        refused, the staging file is removed and `path` is left as it was. An
        error or interrupt arriving after the link was made (removing the staging
        name, say) still raises, with `path` already created whole; a caller that
        must know which checks `path` itself.
    """
    descriptor, staging = _stage(path, _PRIVATE_MODE)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            # Before the write, for the reason `replace_bytes` gives.
            _set_mode(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # A link never follows or replaces its destination: whatever is at `path`,
        # a symlink to nowhere included, makes this raise and stays as it was.
        os.link(staging, path)
    finally:
        # On success too: `path` now names the same file, and the staging name is
        # only a second link to it. BaseException-wide by being a `finally`.
        staging.unlink(missing_ok=True)


def replace_text(
    path: Path,
    text: str,
    *,
    mode: int | None = SERVED_MODE,
    before_rename: BeforeRename | None = None,
) -> bool:
    """Replace `path` with `text`, encoded UTF-8 with LF line endings.

    Encoded here and written as bytes rather than opened in text mode, so the
    line endings are the ones given rather than the host's. Committed outputs in
    this project are byte-compared against a fresh run, and text mode writes
    CRLF on Windows, which every caller would otherwise have to remember to
    suppress with an explicit newline keyword, and four of seven did.

    :param path: The file to replace. Its parent directory must exist.
    :param text: The whole new contents.
    :param mode: The mode the published file carries, or `PRESERVE` to keep
        the one the target already has; see `SERVED_MODE` and `PRESERVE`.
    :param before_rename: As `replace_bytes`.
    :returns: As `replace_bytes`.
    :raises OSError: As `replace_bytes`.
    """
    return replace_bytes(
        path, text.encode("utf-8"), mode=mode, before_rename=before_rename
    )


def is_link_like(mode: int, attributes: int) -> bool:
    """Whether a file of `mode` and Windows `attributes` is a link or reparse point.

    :param mode: The `st_mode` of an `lstat`, never of a `stat`, which has already
        followed the link this asks about.
    :param attributes: Its `st_file_attributes`, or 0 where the host has none.
    """
    return stat.S_ISLNK(mode) or bool(attributes & REPARSE_POINT)


def _refuse_unless_plain_file(path: Path, info: os.stat_result) -> None:
    """`ReadRefused` unless `info`, the path's own `lstat`, is a plain regular file."""
    if is_link_like(info.st_mode, getattr(info, "st_file_attributes", 0)):
        raise ReadRefused(f"{path.name} is a symbolic link or reparse point")
    if not stat.S_ISREG(info.st_mode):
        raise ReadRefused(f"{path.name} is not a regular file")


def _refuse_unless_same_file(
    path: Path, checked: os.stat_result, opened: os.stat_result
) -> None:
    """`ReadRefused` unless `opened`, the descriptor's `fstat`, is the file `checked`.

    First, whether there is anything to compare: on a volume that reports no file
    number every file "matches", so where the open could not refuse a link either
    (no `O_NOFOLLOW`) nothing would catch one swapped in, and the read is refused.
    The identity next: whatever the open reached, by a link the host followed or a
    rename that landed in between, it is not the file that passed the check. The
    type last, and not redundantly: a file system may hand a removed file's number
    to the next thing created, and only the descriptor's own type shows a non-file.
    """
    if not _O_NOFOLLOW and not (checked.st_ino and opened.st_ino):
        raise ReadRefused(
            f"{path.name} cannot be checked against a swap on this volume"
        )
    if (opened.st_dev, opened.st_ino) != (checked.st_dev, checked.st_ino):
        raise ReadRefused(f"{path.name} was replaced between its check and its open")
    if not stat.S_ISREG(opened.st_mode):
        raise ReadRefused(f"{path.name} is no longer a regular file")


def _read_flags() -> int:
    """What a careful read opens with, each flag where the host has it.

    `O_NONBLOCK`, so a named pipe swapped in after the check is opened at once and
    then refused, never waited on for a writer. `O_NOCTTY`, so a terminal device
    swapped in cannot become this process's controlling terminal before it is
    refused. `O_BINARY`, because a Windows descriptor opened without it is in text
    mode, where `os.read` turns each CRLF into a bare LF and stops at a Ctrl-Z.
    Built at each open, not once at import, so a suite on any host can stand in
    for another's flags.
    """
    return (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOCTTY", 0)
        | getattr(os, "O_BINARY", 0)
        | _O_NOFOLLOW
    )


def _open_nofollow(path: Path) -> int:
    """Open `path` for a careful read; `ReadRefused` where the host refuses a link.

    Only with `O_NOFOLLOW` in the flags does one of `_LINK_AT_OPEN_ERRNOS` mean
    the path became a link after its check, so only then is it a refusal. Every
    other failure, a file gone missing since the check included, is the host's
    own error and leaves as it came.
    """
    try:
        return os.open(path, _read_flags())
    except OSError as failure:
        if _O_NOFOLLOW and failure.errno in _LINK_AT_OPEN_ERRNOS:
            raise ReadRefused(
                f"{path.name} is a symbolic link, or its path loops"
            ) from failure
        raise


def _read_at_most(descriptor: int, limit: int) -> bytes:
    """Up to `limit` bytes from `descriptor`: all of it, or the first `limit`.

    A loop because one `os.read` may return fewer bytes than asked with more to
    come; only an empty read means the end.
    """
    chunks: list[bytes] = []
    remaining = limit
    while remaining > 0:
        chunk = os.read(descriptor, min(remaining, _READ_CHUNK_BYTES))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_bytes_nofollow(path: Path, *, max_bytes: int) -> NofollowRead:
    """Read `path` only from the regular file the path itself names, up to a cap.

    For a file whose contents the caller is about to act on, in a folder something
    else can write to. The path is checked with `lstat`, opened without following a
    link where the host can promise that, and the open descriptor is checked again
    with `fstat`, so the bytes returned come from a file with the checked file's
    device and file numbers. Windows has no `O_NOFOLLOW`, so both checks run on
    every host and do not depend on it.

    Not covered, by design. Only the path's last component is protected: a parent
    folder that is itself a link is followed, so the folder has to be one the
    caller already trusts or has checked. And a regular file written in place of
    the expected one before the check is read as given: this says where the bytes
    came from, never that they are true; nor is another regular file that takes
    over the checked file's device and file numbers told from it. Where a volume
    reports file number zero (some Windows network and FAT volumes do) only the
    device is compared: a host without `O_NOFOLLOW` refuses the read, and one
    with it reads another regular file swapped in, as it would one forged in place.

    :param path: The file to read.
    :param max_bytes: The largest file accepted, in bytes. At most one byte more
        is ever read, so an endless or enormous file costs the cap, not its size.
    :returns: The bytes, and the open descriptor's `os.fstat` (see `NofollowRead`).
    :raises ValueError: If `max_bytes` is negative; nothing is touched.
    :raises FileNotFoundError: If nothing is at `path`, at the check or the open.
    :raises ReadRefused: If `path` is a symbolic link or reparse point, or not a
        regular file; if a link was swapped in after the check (refused by the
        open where the host has `O_NOFOLLOW`, with the host's error as the cause,
        and by the identity check elsewhere); if the file opened is not the one
        checked, where the volume reports file numbers, or is no longer a regular
        file; if neither the open nor a
        file number can rule a swap out; or if it holds more than `max_bytes`.
    :raises OSError: If the check, the open or the read fails for a reason of the
        host's own. The descriptor is closed on every path out, these included.
    """
    if max_bytes < 0:
        raise ValueError(f"max_bytes must not be negative, got {max_bytes}")
    checked = path.lstat()
    _refuse_unless_plain_file(path, checked)
    descriptor = _open_nofollow(path)
    try:
        opened = os.fstat(descriptor)
        _refuse_unless_same_file(path, checked, opened)
        # One past the cap, so "exactly at the cap" and "over it" read differently.
        data = _read_at_most(descriptor, max_bytes + 1)
    finally:
        os.close(descriptor)
    if len(data) > max_bytes:
        raise ReadRefused(f"{path.name} is over {max_bytes} bytes")
    return NofollowRead(data, opened)
