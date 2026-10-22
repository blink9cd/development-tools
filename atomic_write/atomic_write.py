#!/usr/bin/env python3
"""One atomic file replacement, for every tool that ships a file.

A file another process READS while its writer runs -- a state file a later run
loads, a document a person opens -- is exposed half-written by a
truncate-and-refill, which surfaces as an intermittent failure that never
reproduces on its own. Writing beside the target and renaming closes
the window: the old file stays whole until one indivisible step swaps in the
new one.

WHY THIS MODULE EXISTS RATHER THAN THE RECIPE BEING RE-TYPED. It was written out
by hand in seven tools, and the copies had already drifted apart on every axis
that matters -- two fsynced and five did not, three set a mode and four left
`mkstemp`'s 0600 -- readable to nothing but the account that wrote it -- to be
renamed into place, and one cleaned up after itself not at all, so a failed
replace stranded a `.tmp` beside committed data. None of those differences was
a decision; each was whatever the tool in front of the author happened to do
that day.

WHAT A CALLER GETS, and why each part is here rather than optional:

* the staging file is created in the TARGET'S OWN directory, because
  `os.replace` is atomic only within one filesystem -- across a mount boundary
  it quietly becomes a copy, which is the very window this closes;
* the bytes are FSYNCED before the rename, because the rename's atomicity says
  nothing about durability: a crash afterwards could otherwise publish an empty
  file under the target's name;
* the mode is SET ON THE DESCRIPTOR rather than left to the umask, because
  `mkstemp` creates at 0600 and the rename carries that through, so a
  published file would be readable by the account that wrote it alone;
* a failure at any point removes the staging file and re-raises, so an
  interrupted build leaves the old file and nothing else.

A caller that must re-check its target as LATE as possible -- a digest taken
when it read the file, compared again just before the swap -- passes a
`before_rename` check. It runs on the finished staging file, after the write,
the mode and the fsync, so the only step left after it is the rename itself;
returning False vetoes the replace, and raising does the same and propagates.

Entry point assumptions: a library, imported by the tools that write files;
there is no CLI here. Callers put this folder on `sys.path` from one fixed place
derived from their own file, as `stdio_utf8/stdio_utf8.md` describes.
"""

from __future__ import annotations

import os
import secrets
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Final

#: The mode a generated file is published with: readable by whatever serves
#: the tree, writable only by the account that wrote it. `mkstemp` creates at
#: 0600 and `os.replace` carries the staging file's mode to the target, so
#: without this a served file arrives unreadable to the server. Set from the
#: DESCRIPTOR, so no window exists in which the file is on disk under the
#: wrong mode.
SERVED_MODE = 0o644

#: Ask for the mode the TARGET already carries, rather than imposing one. A file
#: that does not exist yet has none to keep and gets what a plain write would
#: have given it -- `_NEW_FILE_MODE` filtered by the caller's umask. This is what
#: a plain `Path.write_text` does by construction, and what five of the seven
#: writers this module replaces did deliberately: forcing them onto a flat mode
#: would strip an execute bit from a file that had one and ignore the umask on a
#: file that had none. Spelled as None so the signature stays `int | None`, and
#: named so a call site says which policy it means.
PRESERVE: Final = None

#: What a plain `open(path, "wb")` asks the kernel for on a NEW file, before the
#: umask is subtracted. Handed to `os.open` so a file written for the first time
#: under `PRESERVE` gets the mode a direct write would have given it. Asking the
#: kernel for what a plain write asks for is what makes that umask-correct
#: WITHOUT reading the umask, which `os.umask` allows only by setting it -- a
#: process-wide race inside the caller's own run.
_NEW_FILE_MODE = 0o666

#: What an unfinished write is called while it is unfinished. Leading dot so it
#: sorts out of the way and most tools ignore it; the target's own name in the
#: middle so a stranded one says what it was going to be.
#:
#: PUBLIC because a caller's suite pins it: several tools assert that the rename's
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


def staging_name(target_name: str, token: str) -> str:
    """The name a staging file for `target_name` takes, with `token` as its random part.

    PUBLIC so a tool that must recognize a staging file -- an ignore rule written
    into a directory these files land in, a suite scoping what a run may leave --
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


def _stage(path: Path) -> tuple[int, Path]:
    """Open a staging file beside `path`; its descriptor and its own path.

    Beside it because `os.replace` is atomic only within one filesystem.
    `O_EXCL` refuses any path already at the name, a symlink included, so a
    served file can never be written THROUGH a planted link, and the random name
    leaves nothing to plant one at.

    `os.open` with `_NEW_FILE_MODE` rather than `tempfile.mkstemp`, which takes
    no mode and creates its file readable by its creator alone: under `PRESERVE`
    a new file has to land where a plain write would have put it, and 0600
    renamed into place is the defect this module was built to end.
    """
    staging = path.parent / staging_name(
        path.name, secrets.token_hex(_STAGING_NAME_BYTES)
    )
    # O_BINARY exists only on Windows, where a descriptor opened without it is in
    # text mode and the C runtime writes each "\n" as "\r\n" under a "wb" handle.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    return os.open(staging, flags, _NEW_FILE_MODE), staging


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
    # Read BEFORE the staging file exists, so a target that vanishes mid-write
    # cannot be mistaken for one that was always new.
    wanted = _existing_mode(path) if mode is PRESERVE else mode
    descriptor, staging = _stage(path)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            if wanted is not None:
                # On the DESCRIPTOR, not the path: a mode set afterwards would
                # leave a window in which the finished bytes are on disk under
                # the wrong mode, and would race anything watching the
                # directory. Set BEFORE the write for the same reason.
                _set_mode(handle.fileno(), wanted)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # As late as the check can run: everything that finishes the staging
        # file is behind it, so the window a caller's re-check leaves open is
        # the rename alone -- the narrowest it can be without a lock.
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


def replace_text(
    path: Path,
    text: str,
    *,
    mode: int | None = SERVED_MODE,
    before_rename: BeforeRename | None = None,
) -> bool:
    """Replace `path` with `text`, encoded UTF-8 with LF line endings.

    Encoded here and written as BYTES rather than opened in text mode, so the
    line endings are the ones given rather than the host's. Committed outputs in
    this project are byte-compared against a fresh run, and text mode writes
    CRLF on Windows -- which every caller would otherwise have to remember to
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
