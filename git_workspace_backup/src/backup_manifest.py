"""Serialize and parse MANIFEST.json.

The parser is total. A snapshot folder in iCloud can hold a truncated sync, a stray file, or
a manifest from a future schema, and prune decides what to delete by reading it. Any input
that is not exactly this tool's current schema parses as None ("not ours"), which is the
safe answer for a deletion decision.

File, archive-member and bundle names are later joined onto the snapshot directory to hash
and verify what they name, so each must be a relative path that cannot leave it. Without
that, an edited ``../<sibling>/workspace.bundle`` would let a corrupt snapshot verify by
hashing a neighboring snapshot's intact file.

Two exceptions to "exactly the current schema", each a field added after snapshots without
it were written. A bundle entry without ``stashes`` was written before stash entries were
recorded, and reads as recording none. A manifest without ``machine`` was written before
machine identity, and reads as naming no machine; the writer leaves the field out for such a
manifest, so it round-trips, and a ``machine`` that is present must be a valid label
(backup_machine). Read as "not ours", every snapshot taken before either change would be
kept past its retention forever.
"""

from __future__ import annotations

from dataclasses import asdict, fields

from backup_json import (
    ShapeError,
    as_object,
    envelope_to_json,
    get_bool,
    get_int,
    get_list,
    get_str,
    open_envelope,
    relative_posix_name,
    require_keys,
    str_item,
)
from backup_machine import is_valid_label
from backup_model import (
    BundleEntry,
    DirtyWorktree,
    FailedItem,
    FileEntry,
    Manifest,
    StashEntry,
    WorktreeHead,
)

MANIFEST_NAME = "MANIFEST.json"
#: The manifest's own fields, derived from the record so the writer and reader cannot drift.
_MANIFEST_KEYS = frozenset(field.name for field in fields(Manifest))
_BUNDLE_KEYS = frozenset(field.name for field in fields(BundleEntry))
_STASHES_KEY = "stashes"
_MACHINE_KEY = "machine"


def manifest_to_json(manifest: Manifest) -> str:
    """The manifest in this tool's envelope as stable JSON; no ``machine`` when it names none."""
    body = asdict(manifest)
    if manifest.machine is None:
        del body[_MACHINE_KEY]
    return envelope_to_json(body)


# fuzz-target
def parse_manifest(data: bytes) -> "Manifest | None":
    """The manifest in *data*, or None when it is not exactly this tool's current schema."""
    try:
        obj = open_envelope(
            data, _MANIFEST_KEYS - {_MACHINE_KEY}, optional=frozenset({_MACHINE_KEY})
        )
        recorded = _MACHINE_KEY in obj
        return Manifest(
            created=get_str(obj, "created"),
            complete=get_bool(obj, "complete"),
            bundles=get_list(obj, "bundles", _bundle_entry),
            files=get_list(obj, "files", file_entry),
            archive_members=get_list(obj, "archive_members", file_entry),
            dirty_worktrees=get_list(obj, "dirty_worktrees", _dirty_worktree),
            failed=get_list(obj, "failed", failed_item),
            skipped_symlinks=get_list(obj, "skipped_symlinks", str_item),
            unclassified_ignored=get_list(obj, "unclassified_ignored", str_item),
            machine=_machine(obj) if recorded else None,
        )
    except ShapeError:
        return None


def file_entry(value: object) -> FileEntry:
    """A ``{name, sha256, size}`` object whose name is a safe relative path."""
    obj = as_object(value)
    require_keys(obj, frozenset({"name", "sha256", "size"}))
    return FileEntry(
        relative_posix_name(obj, "name"), get_str(obj, "sha256"), get_int(obj, "size")
    )


def failed_item(value: object) -> FailedItem:
    """A ``{item, reason}`` object."""
    obj = as_object(value)
    require_keys(obj, frozenset({"item", "reason"}))
    return FailedItem(get_str(obj, "item"), get_str(obj, "reason"))


def _bundle_entry(value: object) -> BundleEntry:
    obj = as_object(value)
    recorded = _STASHES_KEY in obj
    require_keys(obj, _BUNDLE_KEYS if recorded else _BUNDLE_KEYS - {_STASHES_KEY})
    return BundleEntry(
        name=relative_posix_name(obj, "name"),
        head=get_str(obj, "head"),
        refs=get_list(obj, "refs", str_item),
        worktree_heads=get_list(obj, "worktree_heads", _worktree_head),
        stashes=get_list(obj, _STASHES_KEY, _stash_entry) if recorded else (),
    )


def _machine(obj: dict[str, object]) -> str:
    """The machine label at ``machine``, which must be a valid one."""
    machine = get_str(obj, _MACHINE_KEY)
    if not is_valid_label(machine):
        raise ShapeError(f"machine is not a machine label: {machine!r}")
    return machine


def _stash_entry(value: object) -> StashEntry:
    obj = as_object(value)
    require_keys(obj, frozenset({"sha", "message"}))
    return StashEntry(get_str(obj, "sha"), get_str(obj, "message"))


def _worktree_head(value: object) -> WorktreeHead:
    obj = as_object(value)
    require_keys(obj, frozenset({"name", "sha", "reachable"}))
    return WorktreeHead(
        get_str(obj, "name"), get_str(obj, "sha"), get_bool(obj, "reachable")
    )


def _dirty_worktree(value: object) -> DirtyWorktree:
    obj = as_object(value)
    require_keys(obj, frozenset({"path_name", "changed_paths"}))
    return DirtyWorktree(
        get_str(obj, "path_name"), get_int(obj, "changed_paths", minimum=-1)
    )
