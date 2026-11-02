"""The records workspace_backup writes and reads back: manifest, ledger and run state.

Frozen dataclasses and constants only: no I/O and no parsing. Serialization lives beside
each record's reader (backup_manifest, backup_ledger), so these shapes are the one
definition the writer and the reader agree on.
"""

from __future__ import annotations

from dataclasses import dataclass

TOOL_ID = "workspace_backup"
SCHEMA_VERSION = 1
#: The tool's own directory under home: the run lock, the machine identity, per-destination
#: state, staging and logs.
STATE_BASE_NAME = ".workspace_backup"

OUTCOME_COMPLETE = "complete"
OUTCOME_INCOMPLETE = "incomplete"
OUTCOME_FAILED = "failed"
OUTCOME_SKIPPED = "skipped"
OUTCOMES = frozenset(
    {OUTCOME_COMPLETE, OUTCOME_INCOMPLETE, OUTCOME_FAILED, OUTCOME_SKIPPED}
)

#: A snapshot folder the tool renamed into place.
STATE_PROMOTED = "promoted"
#: A cross-volume copy in flight; recorded before the copy so a crash leaves a known leftover.
STATE_INCOMING = "incoming"
LEDGER_STATES = frozenset({STATE_PROMOTED, STATE_INCOMING})


@dataclass(frozen=True)
class FailedItem:
    """One unit of work that did not succeed, and why."""

    item: str
    reason: str


@dataclass(frozen=True)
class FileEntry:
    """A file's POSIX name (snapshot- or archive-relative), sha256 and size in bytes."""

    name: str
    sha256: str
    size: int


@dataclass(frozen=True)
class WorktreeHead:
    """A linked worktree's HEAD as a bundle records it: ``worktrees/<name>/HEAD``."""

    name: str
    sha: str
    reachable: bool  # whether a branch or tag already contains it


@dataclass(frozen=True)
class StashEntry:
    """One stash entry: its commit and the reflog message ``git stash list`` shows."""

    sha: str
    message: str


@dataclass(frozen=True)
class BundleEntry:
    """One bundle in a snapshot: its file, HEAD commit, and the refs a restore must hold.

    *stashes* runs oldest first, the order a restore stores them in. ``refs/stash`` names
    only the newest entry and a bundle carries no reflog, so the bundle holds each entry's
    commit unnamed and this list is the only record of which commits they are.
    """

    name: str
    head: str
    refs: tuple[str, ...]
    worktree_heads: tuple[WorktreeHead, ...]
    stashes: tuple[StashEntry, ...] = ()


@dataclass(frozen=True)
class DirtyWorktree:
    """A worktree with uncommitted changes: reported, never captured. -1 means unreadable."""

    path_name: str
    changed_paths: int


@dataclass(frozen=True)
class Manifest:
    """Everything a snapshot claims about itself; written last, read by prune and status."""

    created: str
    complete: bool
    bundles: tuple[BundleEntry, ...]
    files: tuple[FileEntry, ...]
    archive_members: tuple[FileEntry, ...]
    dirty_worktrees: tuple[DirtyWorktree, ...]
    failed: tuple[FailedItem, ...]
    skipped_symlinks: tuple[str, ...]
    unclassified_ignored: tuple[str, ...]
    #: The label of the machine that took it (backup_machine); None for a snapshot taken
    #: before machine identity, whose manifest has no such field.
    machine: "str | None" = None


@dataclass(frozen=True)
class LedgerEntry:
    """A destination folder this tool created, with the manifest hash that proves it."""

    name: str
    state: str
    complete: bool
    manifest_sha256: str


@dataclass(frozen=True)
class Ledger:
    """The per-destination record of every folder this tool created and still owns."""

    entries: tuple[LedgerEntry, ...] = ()

    def __post_init__(self) -> None:
        """Refuse a name held twice: prune plans by name, so one name would stand for two rows.

        An incoming row and a promoted row of one name would plan that name as a leftover, and
        a leftover is deleted without the manifest proof a promoted snapshot needs.
        """
        names = [entry.name for entry in self.entries]
        repeated = sorted({name for name in names if names.count(name) > 1})
        if repeated:
            raise ValueError(f"a ledger names {repeated!r} more than once")

    def with_entry(self, entry: LedgerEntry) -> Ledger:
        """A copy with *entry* added, replacing any entry of the same name."""
        return Ledger(self.without(entry.name).entries + (entry,))

    def without(self, name: str) -> Ledger:
        """A copy without the entry named *name* (unchanged when there is none)."""
        return Ledger(tuple(entry for entry in self.entries if entry.name != name))


@dataclass(frozen=True)
class RunRecord:
    """The outcome of the most recent run against one destination."""

    finished: str
    outcome: str
    detail: str
    snapshot: "str | None"
    failed: tuple[FailedItem, ...]
