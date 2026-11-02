"""Move a verified snapshot into the destination, and prune what the ledger owns.

Promote is one rename, since staging and iCloud Drive share an APFS volume, with a copy
fallback on EXDEV that records its intent in the ledger first, so a crash mid-copy leaves a known
leftover rather than an orphan. A folder is never replaced.

Prune is the only step with no undo, so the run calls it last, and it is the only code in this
tool that deletes snapshot data. It touches only ``destination/<name>`` for a ledger name of
the shape the tool writes (a snapshot name for a promoted entry, ``.incoming-`` and a
snapshot name for an incoming one), never follows a symlink, and deletes a promoted folder
only when its manifest, read as a regular file with no symlink beneath the destination, still
hashes to the ledger's value. A manifest it cannot read that way in time (iCloud may have
offloaded it) defers the deletion instead of forcing it. A removal that fails is recorded as
deferred, never raised.

A snapshot name is the local time it was taken, then ``-`` and the label of the machine that
took it (backup_machine), so two machines sharing one destination never name the same folder.
The time comes first, so names still sort chronologically whatever the machine. A bare name,
with no label, is one written before machine identity: it still parses, as naming no machine,
so the snapshots taken before it stay ours and roll off through ``keep``.

Existence is judged by ``lstat`` and only "no such file" counts as absent. ``Path.exists``
answers differently across interpreters for any other error: 3.11 and 3.12 raise for one
outside a short ignore list (ENAMETOOLONG among them); later interpreters (3.14 measured)
return False for every error. Either answer would be wrong here: a raise escapes prune, and a
False drops a folder the tool still owns from the ledger.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from backup_fs import read_bytes_bounded, read_regular_file, sha256_bytes
from backup_machine import is_valid_label
from backup_manifest import MANIFEST_NAME
from backup_model import STATE_INCOMING, STATE_PROMOTED, Ledger, LedgerEntry

SNAPSHOT_NAME_FORMAT = "%Y-%m-%dT%H%M%S"
#: What joins a snapshot's timestamp to the machine label after it.
MACHINE_SEPARATOR = "-"
#: The timestamp's time mark and the time after it, ``THHMMSS``: where a timestamp ends.
_TIME_MARK = "T"
_TIME_LENGTH = len("THHMMSS")
INCOMING_PREFIX = ".incoming-"
MANIFEST_READ_TIMEOUT_S = 60.0

RenameFn = Callable[[Path, Path], None]
CopyTreeFn = Callable[[Path, Path], object]
RemoveTreeFn = Callable[[Path], None]
ReadBoundedFn = Callable[[Path, float], "bytes | None"]

# What pruning one planned name came to. Vanished means the folder is already gone, so the
# ledger lets go of it without a deletion being reported.
_DELETED = "deleted"
_DEFERRED = "deferred"
_VANISHED = "vanished"


class PromoteError(Exception):
    """The snapshot could not be moved into the destination."""


@dataclass(frozen=True)
class PruneResult:
    """The ledger after pruning, and what was deleted or deferred."""

    ledger: Ledger
    deleted: tuple[str, ...]
    deferred: tuple[str, ...]


class SnapshotName(NamedTuple):
    """What a snapshot folder name records: when it was taken, and by which machine.

    *machine* is None for a bare name, one written before machine identity.
    """

    moment: datetime
    machine: "str | None"


# fuzz-target
def snapshot_name(now: datetime, machine: "str | None" = None) -> str:
    """The folder name for a snapshot taken at *now* (local time) by *machine*.

    Sorts chronologically. None writes the bare name, as before machine identity; a
    *machine* that is not a valid label (backup_machine) is a ValueError, since it would
    write a name no parser here accepts.
    """
    stamp = now.strftime(SNAPSHOT_NAME_FORMAT)
    if machine is None:
        return stamp
    if not is_valid_label(machine):
        raise ValueError(f"not a machine label: {machine!r}")
    return f"{stamp}{MACHINE_SEPARATOR}{machine}"


# fuzz-target
def split_snapshot_name(name: str) -> "SnapshotName | None":
    """The moment and machine a snapshot folder name records, or None for any other name.

    A name counts only when :func:`snapshot_name` would write it back exactly. ``strptime``
    alone accepts unpadded fields and non-ASCII digits (``2026-9-14T60005``), which would let a
    folder the tool never wrote pass as a snapshot. The timestamp ends where its time does,
    seven characters from its first time mark, since no date field holds one.
    """
    mark = name.find(_TIME_MARK)
    if mark < 0:
        return None
    end = mark + _TIME_LENGTH
    stamp, tail = name[:end], name[end:]
    try:
        moment = datetime.strptime(stamp, SNAPSHOT_NAME_FORMAT)
    except ValueError:
        return None
    if snapshot_name(moment) != stamp:
        return None
    if not tail:
        return SnapshotName(moment, None)
    machine = tail[len(MACHINE_SEPARATOR) :]
    if not tail.startswith(MACHINE_SEPARATOR) or not is_valid_label(machine):
        return None
    return SnapshotName(moment, machine)


# fuzz-target
def parse_snapshot_name(name: str) -> "datetime | None":
    """The moment a snapshot folder name records, bare or carrying a machine; else None."""
    parts = split_snapshot_name(name)
    return None if parts is None else parts.moment


def _copy_tree(src: Path, dst: Path) -> object:
    """Copy a snapshot tree, keeping any symlink inside it a symlink."""
    return shutil.copytree(src, dst, symlinks=True)


def _remove_tree(path: Path) -> None:
    """Remove a tree. ``rmtree`` refuses a symlinked *path* itself, and raises on failure."""
    shutil.rmtree(path)


def promote(
    staging: Path,
    destination: Path,
    name: str,
    ledger: Ledger,
    *,
    save_ledger: Callable[[Ledger], None],
    rename: RenameFn = os.rename,
    copy_tree: CopyTreeFn = _copy_tree,
    remove_tree: RemoveTreeFn = _remove_tree,
) -> "tuple[Path, Ledger]":
    """Move *staging* to ``destination/name``; returns the target and the ledger to carry on.

    *name* must be a snapshot name, so the target is always one folder directly under the
    destination, of a shape prune can later prove it owns. Anything already at the target (a
    folder, a file, a symlink dangling or not) is refused, as is a target whose state cannot
    be read. The check and the rename are two steps, and on POSIX ``rename`` silently replaces
    an empty directory created between them; the standard library has no exclusive rename. The
    caller holds the run lock, so nothing else in this tool can create that directory in the
    window; another program writing snapshot-named folders into the destination is outside
    what this guards. The cross-volume path checks the target a second time, immediately
    before its final rename, because the copy ahead of that rename is slow.

    The destination itself is created, never its parents: a missing parent means the iCloud
    Drive folder is absent, and creating it would put the snapshot on local disk under a path
    that only looks synced.
    """
    if parse_snapshot_name(name) is None:
        raise PromoteError(f"not a snapshot name: {name!r}")
    target = destination / name
    _refuse_occupied(target)
    try:
        destination.mkdir(exist_ok=True)
    except OSError as exc:
        raise PromoteError(f"could not create {destination}: {exc}") from exc
    try:
        rename(staging, target)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise PromoteError(
                f"could not move snapshot into {destination}: {exc}"
            ) from exc
        return _copy_across_volumes(
            staging, target, ledger, save_ledger, rename, copy_tree, remove_tree
        )
    return target, ledger


def _refuse_occupied(target: Path) -> None:
    """Raise PromoteError unless *target* is provably free (a symlink occupies it)."""
    try:
        target.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise PromoteError(f"could not check that {target} is free: {exc}") from exc
    raise PromoteError(f"{target} already exists")


def _copy_across_volumes(
    staging: Path,
    target: Path,
    ledger: Ledger,
    save_ledger: Callable[[Ledger], None],
    rename: RenameFn,
    copy_tree: CopyTreeFn,
    remove_tree: RemoveTreeFn,
) -> "tuple[Path, Ledger]":
    """Record the intent, copy beside the target, check it again, then rename the copy in."""
    incoming = target.with_name(f"{INCOMING_PREFIX}{target.name}")
    ledger = ledger.with_entry(LedgerEntry(incoming.name, STATE_INCOMING, False, ""))
    try:
        save_ledger(ledger)
    except OSError as exc:
        # Nothing is copied without the record: an unrecorded copy is an orphan prune can
        # never prove it owns.
        raise PromoteError(f"could not record the cross-volume copy: {exc}") from exc
    try:
        copy_tree(staging, incoming)
        _refuse_occupied(target)
        rename(incoming, target)
    except OSError as exc:
        raise PromoteError(
            f"cross-volume copy into {target.parent} failed: {exc}"
        ) from exc
    try:
        remove_tree(staging)
    except OSError:
        # The snapshot is already in place. Failing now would report a failed run over a good
        # snapshot and leave it out of the ledger for good; the staging copy left behind is
        # removed by sweep_staging at the start of the next run, under the same lock.
        pass
    return target, ledger.without(incoming.name)


def plan_prune(ledger: Ledger, keep: int) -> tuple[str, ...]:
    """Names to delete: all but the newest *keep* complete and *keep* incomplete, and every leftover.

    Retention orders by snapshot name, which is local wall-clock time. Across a daylight-saving
    fall-back or a clock correction an older snapshot can therefore outrank a newer one; either
    way, every planned deletion is still proven by :func:`prune` before anything is removed.

    A negative *keep* is refused: as a slice bound it would silently mean "delete the oldest
    few" rather than anything a caller asked for.
    """
    if keep < 0:
        raise ValueError(f"keep must not be negative, got {keep}")
    promoted = sorted(
        (entry for entry in ledger.entries if entry.state == STATE_PROMOTED),
        key=lambda entry: entry.name,
        reverse=True,
    )
    complete = [entry.name for entry in promoted if entry.complete]
    incomplete = [entry.name for entry in promoted if not entry.complete]
    leftovers = sorted(
        entry.name for entry in ledger.entries if entry.state == STATE_INCOMING
    )
    return tuple(complete[keep:] + incomplete[keep:] + leftovers)


def _read_beneath_destination(path: Path) -> bytes:
    """The bytes of the regular file ``destination/<name>/<file>`` at *path*.

    Opened relative to a descriptor on the destination by ``backup_fs.read_regular_file``: where
    ``O_NOFOLLOW_ANY`` exists neither the snapshot folder nor the file may be a symlink, the open
    never waits on a FIFO, and anything but a regular file raises OSError. The destination
    itself is configuration, so it alone is opened following a link.
    """
    folder = path.parent
    return read_regular_file(folder.parent, f"{folder.name}/{path.name}")[0]


def _read_manifest_bounded(path: Path, timeout: float) -> "bytes | None":
    """:func:`_read_beneath_destination` on the watchdog; None when it fails or times out.

    The default reader would follow a symlinked manifest to matching bytes outside the snapshot
    and call that proof, and would block opening a FIFO, pinning its thread for the life of the
    process.
    """
    return read_bytes_bounded(path, timeout, reader=_read_beneath_destination)


def prune(
    destination: Path,
    ledger: Ledger,
    keep: int,
    *,
    protected: Collection[str] = (),
    read_bounded: ReadBoundedFn = _read_manifest_bounded,
    remove_tree: RemoveTreeFn = _remove_tree,
) -> PruneResult:
    """Delete what :func:`plan_prune` names, when each deletion can be proven safe.

    A name in *protected* is never deleted, planned or not: the run protects the snapshot it
    has just promoted, which a clock set back far enough would otherwise plan past the newest
    ``keep`` and delete in the same run. A destination that is not a readable directory defers
    everything: every folder would otherwise look vanished, and the ledger would let go of
    snapshots that may yet reappear.
    """
    planned = tuple(name for name in plan_prune(ledger, keep) if name not in protected)
    if not _is_directory(destination, follow=True):
        return PruneResult(ledger, (), planned)
    by_name = {entry.name: entry for entry in ledger.entries}
    deleted: list[str] = []
    deferred: list[str] = []
    for name in planned:
        outcome = _prune_one(destination, by_name[name], read_bounded, remove_tree)
        if outcome == _DEFERRED:
            deferred.append(name)
            continue
        ledger = ledger.without(name)
        if outcome == _DELETED:
            deleted.append(name)
    return PruneResult(ledger, tuple(deleted), tuple(deferred))


def _prune_one(
    destination: Path,
    entry: LedgerEntry,
    read_bounded: ReadBoundedFn,
    remove_tree: RemoveTreeFn,
) -> str:
    """Delete one planned folder if it is provably ours; which outcome that came to."""
    if not _is_owned_name(entry):
        return _DEFERRED
    path = destination / entry.name
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return _VANISHED
    except OSError:
        return _DEFERRED
    # lstat, so a symlink is never a directory here and is never handed to remove_tree.
    if not stat.S_ISDIR(mode):
        return _DEFERRED
    if entry.state == STATE_PROMOTED and not _manifest_matches(
        path, entry, read_bounded
    ):
        return _DEFERRED
    try:
        remove_tree(path)
    except OSError:
        return _DEFERRED
    return _DELETED


def _is_owned_name(entry: LedgerEntry) -> bool:
    """True when *entry*'s name has the shape this tool gives a folder in that state.

    Promoted: a snapshot name. Incoming: :data:`INCOMING_PREFIX` and a snapshot name. Incoming
    entries skip the manifest proof, so this is all that keeps one from naming a real snapshot
    (or, in a hand-built ledger, ``""``, ``"."`` or ``".."``) and deleting it unproven. A
    snapshot name is one plain path component, so neither shape can leave the destination.
    """
    if entry.state == STATE_INCOMING:
        tail = entry.name[len(INCOMING_PREFIX) :]
        return (
            entry.name.startswith(INCOMING_PREFIX)
            and parse_snapshot_name(tail) is not None
        )
    return parse_snapshot_name(entry.name) is not None


def sweep_staging(
    staging_root: Path, *, remove_tree: RemoveTreeFn = _remove_tree
) -> tuple[str, ...]:
    """Remove everything left in *staging_root*; safe only while the run lock is held.

    Only direct children go, and the root itself stays. A directory goes through
    *remove_tree*; a file or a symlink, to a directory or not, is unlinked, never followed.
    A root that lstat does not show as a directory (absent, unreadable, or a symlink even to
    a directory) sweeps nothing: the children of a link's target are not staging's to remove.
    """
    if not _is_directory(staging_root, follow=False):
        return ()
    removed: list[str] = []
    for child in sorted(staging_root.iterdir()):
        if child.is_dir() and not child.is_symlink():
            remove_tree(child)
        else:
            child.unlink()
        removed.append(child.name)
    return tuple(removed)


def _is_directory(path: Path, *, follow: bool) -> bool:
    """True when *path* is a directory (through a symlink only when *follow*); False on error."""
    try:
        return stat.S_ISDIR(os.stat(path, follow_symlinks=follow).st_mode)
    except OSError:
        return False


def _manifest_matches(
    folder: Path, entry: LedgerEntry, read_bounded: ReadBoundedFn
) -> bool:
    """True only when the manifest was read in time and hashes to the ledger's value."""
    data = read_bounded(folder / MANIFEST_NAME, MANIFEST_READ_TIMEOUT_S)
    return data is not None and sha256_bytes(data) == entry.manifest_sha256
