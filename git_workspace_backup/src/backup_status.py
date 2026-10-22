"""Judge a destination's health for ``--mode status``.

Exit 2 means it cannot be judged: the agent is not installed, or no run record for the
destination can be read. Exit 1 lists every problem found, then any notes; exit 0 is healthy,
with notes. The verdict is a pure function of :class:`StatusFacts`, and a value it does not
recognize -- an agent state, a run outcome, an upload summary -- is a problem, never health.

The upload grace exists because the scheduled run is followed by an upload that can
legitimately lag; past it, "written but not uploaded" is a failure, which catches a signed-out
account or a paused sync, where the local folder exists and nothing leaves the machine. Both
thresholds are measured from the moment a snapshot's name records, when its run started. That
is earlier than its promotion by the length of the build, so each fires that much sooner, never
later.

``now`` must be naive local time, as snapshot names are; an aware one is refused where the
facts are gathered and where they are built, rather than escaping later as a comparison error.
A snapshot dated after ``now`` is a problem: its age is negative, so it would never go stale
and a pending upload would sit inside its grace for ever. That is a clock set back, or a
snapshot from the future, and either one must be said.

A skipped last run -- an ``--if-due`` run on a day already covered; a run that finds the lock
held records nothing -- is not itself a problem. The destination is judged by what it holds:
the newest complete snapshot's age, manifest and upload, and the ledger, so a skip masks no
staleness. Nor does it mask an incomplete snapshot:
a skip overwrites the record of the run before it, so when the newest snapshot the ledger
holds is incomplete, that is reported in the record's place.

A bare-named folder under the destination that the ledger does not hold is a note, not a
problem. Prune never touches one, which is the safe direction, but one is never reclaimed
either: a promote that could not confirm leaves one, and after a machine restore the ledger,
kept under the home directory rather than in the destination, starts empty, so every old
snapshot becomes one.

Names carry the machine that took them (backup_machine), and several machines may share one
destination. A folder named for another machine is that machine's to prune, so all of them
are one informational count. A bare name, from before machine identity, is untracked as
above. A folder named for this machine that its ledger lacks is untracked too, and also a
problem, with two causes and a fix for each. Most likely it is this machine's own snapshot
that a run could not ledger -- a promote that could not confirm, a ledger save that failed,
or a restore of this machine that started with an empty ledger -- and it is checked and kept
or deleted by hand. Relabeling this machine would only make it worse: those folders would
then count as another machine's, which no machine prunes. Only a second machine using the
same label is fixed in the identity file, on one of them.

The newest manifest is believed only when it hashes to the ledger's value, the proof prune
also requires. The snapshot's name and that hash come from one ledger entry, selected once.
A manifest that does not match is reported unreadable, and the upload check then covers the
manifest file alone rather than a file list nothing vouches for.

"Not in iCloud" means two different things. A destination deliberately chosen elsewhere -- a
local or external disk -- gets a note, and the upload check does not apply. A destination at
or under iCloud Drive's folder whose files are not in iCloud is a problem: iCloud Drive is
turned off or signed out, the folder is a plain local directory again, and every run succeeds
while nothing leaves the machine. Which one applies is decided against the injected home's
iCloud Drive folder, as backup_config names and resolves it.
"""

from __future__ import annotations

import itertools
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple, TypeVar

from backup_config import canonical_path, icloud_drive_root
from backup_fs import read_bytes_bounded, sha256_bytes
from backup_json import apfs_name_key
from backup_icloud import (
    UPLOAD_NONE,
    UPLOAD_NOT_ICLOUD,
    UPLOAD_PENDING,
    UPLOAD_UPLOADED,
    read_upload_states,
    summarize_upload,
)
from backup_ledger import (
    LAST_RUN_NAME,
    LedgerError,
    load_ledger,
    newest_complete_entry,
    read_last_run,
)
from backup_manifest import MANIFEST_NAME, parse_manifest
from backup_model import (
    OUTCOME_COMPLETE,
    OUTCOME_SKIPPED,
    STATE_PROMOTED,
    Ledger,
    LedgerEntry,
    Manifest,
    RunRecord,
)
from backup_proc import RunCmd
from backup_machine import IDENTITY_SHOWN
from backup_promote import (
    MANIFEST_READ_TIMEOUT_S,
    ReadBoundedFn,
    parse_snapshot_name,
    split_snapshot_name,
)

AGENT_LOADED = "loaded"
AGENT_ABSENT = "absent"
AGENT_UNKNOWN = "unknown"
STALE_AFTER = timedelta(hours=36)
UPLOAD_GRACE = timedelta(hours=24)
EXIT_HEALTHY = 0
EXIT_ATTENTION = 1
EXIT_UNJUDGED = 2
#: Prune keeps ``keep`` complete and at most ``keep`` incomplete snapshots, so a ledger
#: holding more than this many times ``keep`` means pruning is not keeping up.
MAX_SNAPSHOTS_PER_KEEP = 2
#: How many destination entries are examined for untracked snapshot folders: a destination
#: grown huge costs a partial note, never an unbounded listing.
UNTRACKED_SCAN_LIMIT = 10_000
#: How many untracked snapshot folders the note names before it counts the rest.
UNTRACKED_NAMED = 5
_HOUR = timedelta(hours=1)
_NO_TIME = timedelta(0)
#: Last-run outcomes that are not a problem by themselves.
_UNREMARKABLE_OUTCOMES = frozenset({OUTCOME_COMPLETE, OUTCOME_SKIPPED})
#: Upload summaries a verdict can act on; any other value is an unreadable upload state.
_KNOWN_UPLOADS = frozenset({UPLOAD_UPLOADED, UPLOAD_PENDING, UPLOAD_NOT_ICLOUD})
_ICLOUD_DRIVE_OFF = (
    "the destination is in iCloud Drive, yet its files are not in iCloud: check that iCloud "
    "Drive is signed in and turned on"
)
_NO_RUN_RECORD = (
    "no run record for this destination can be read: it has never run, or its "
    f"{LAST_RUN_NAME} is not this tool's"
)

_Read = TypeVar("_Read")


class StateReadError(LedgerError):
    """A recorded state file exists but cannot be read: like a foreign ledger, nothing to judge."""


def _require_naive(now: datetime) -> None:
    """Refuse an aware *now*: snapshot names record naive local time."""
    if now.tzinfo is not None:
        raise ValueError(
            f"now must be naive local time, as snapshot names are; got tzinfo {now.tzinfo!r}"
        )


@dataclass(frozen=True)
class StatusFacts:
    """Everything the verdict is a function of."""

    agent: str
    last_run: "RunRecord | None"
    ledger: Ledger
    keep: int
    newest_complete: "str | None"
    newest_manifest: "Manifest | None"
    upload: str
    now: datetime
    #: Whether the destination is iCloud Drive's folder or lies beneath it.
    in_icloud_drive: bool
    #: Snapshot-named folders directly under the destination that the ledger does not hold:
    #: bare ones, and ones named for this machine.
    untracked: tuple[str, ...] = ()
    #: This machine's identity, which the names it writes end in; None when not known.
    machine: "str | None" = None
    #: How many snapshot folders directly under the destination are named for other machines.
    other_machines: int = 0
    #: Problems the installed agent itself raises: a pinned destination not the one judged.
    agent_problems: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Refuse an aware ``now``, so no judgment ever compares it with a snapshot time."""
        _require_naive(self.now)


class _Listing(NamedTuple):
    """What the destination's listing says, in StatusFacts' field order after in_icloud_drive."""

    untracked: tuple[str, ...]
    machine: str
    other_machines: int


@dataclass(frozen=True)
class Verdict:
    """The exit code and the lines to print."""

    code: int
    lines: tuple[str, ...]


def gather_facts(
    *,
    destination: Path,
    state: Path,
    keep: int,
    agent: str,
    now: datetime,
    home: Path,
    run: RunCmd,
    machine: str,
    read_bounded: ReadBoundedFn = read_bytes_bounded,
) -> StatusFacts:
    """Read the ledger, the last run, and the newest complete snapshot's manifest and uploads.

    Raises ValueError, before reading anything, for an aware *now*. Raises LedgerError for a
    ledger this tool did not write, and StateReadError (a LedgerError) for a ledger or run
    record that exists but cannot be read. A manifest that is not read within
    MANIFEST_READ_TIMEOUT_S, does not hash to the ledger, or does not parse is None; it never
    raises. *home* is the home directory the configuration expands ``~`` against: iCloud
    Drive's folder is found under it, never under the process's own home. *machine* is this
    machine's identity, which tells its snapshot folders from other machines'.
    """
    _require_naive(now)
    in_drive = _in_icloud_drive(destination, home)
    ledger = _read_state(state, load_ledger)
    last_run = _read_state(state, read_last_run)
    untracked, others = _untracked_snapshots(destination, ledger, machine)
    listing = _Listing(untracked, machine, others)
    newest = newest_complete_entry(ledger)
    if newest is None:
        return StatusFacts(
            agent,
            last_run,
            ledger,
            keep,
            None,
            None,
            UPLOAD_NONE,
            now,
            in_drive,
            *listing,
        )
    folder = destination / newest.name
    manifest = _trusted_manifest(
        folder / MANIFEST_NAME, newest.manifest_sha256, read_bounded
    )
    listed = manifest.files if manifest is not None else ()
    paths = [folder / MANIFEST_NAME, *(folder / item.name for item in listed)]
    upload = summarize_upload(read_upload_states(paths, run=run))
    return StatusFacts(
        agent,
        last_run,
        ledger,
        keep,
        newest.name,
        manifest,
        upload,
        now,
        in_drive,
        *listing,
    )


def judge(facts: StatusFacts) -> Verdict:
    """The verdict for *facts*: unjudged, every problem then every note, or healthy."""
    if facts.agent == AGENT_ABSENT:
        # No plist, so no pin to compare: agent_problems is always empty here.
        return Verdict(EXIT_UNJUDGED, ("the launchd agent is not installed",))
    if facts.last_run is None:
        # The agent writing elsewhere is the likeliest reason this one has no record.
        return Verdict(EXIT_UNJUDGED, (_NO_RUN_RECORD, *facts.agent_problems))
    problems = (
        *facts.agent_problems,
        *_run_problems(facts, facts.last_run),
        *_ledger_problems(facts),
        *_snapshot_problems(facts),
        *_identity_problems(facts),
    )
    notes = tuple(_notes(facts))
    if problems:
        return Verdict(EXIT_ATTENTION, problems + notes)
    healthy = f"healthy: newest complete snapshot {facts.newest_complete}"
    return Verdict(EXIT_HEALTHY, (healthy, *notes))


def _read_state(state: Path, reader: Callable[[Path], _Read]) -> _Read:
    """*reader* applied to *state*, with an OSError raised as StateReadError."""
    try:
        return reader(state)
    except OSError as exc:
        detail = f"could not read the recorded state under {state}: {exc}"
        raise StateReadError(detail) from exc


def _trusted_manifest(
    path: Path, expected_sha256: str, read_bounded: ReadBoundedFn
) -> "Manifest | None":
    """The manifest at *path* when it is read in time and hashes to *expected_sha256*."""
    data = read_bounded(path, MANIFEST_READ_TIMEOUT_S)
    if data is None or sha256_bytes(data) != expected_sha256:
        return None
    return parse_manifest(data)


def _in_icloud_drive(destination: Path, home: Path) -> bool:
    """Whether *destination* is iCloud Drive's folder under *home*, or lies beneath it.

    Component by component, so a sibling whose name merely starts with the folder's is outside
    it. Both sides are resolved by backup_config's one rule, then compared as APFS compares
    names, ignoring case and Unicode normalization form: a resolved path keeps the spelling it
    was given, and an NFD spelling of an NFC home names the same folder. A wrong "inside" can
    only turn a note into a problem, which is the safe direction. Not covered, in the unsafe
    direction: a destination spelled through a firmlink, such as ``/System/Volumes/Data/...``,
    which resolving does not map back, reads as outside -- a note where a problem was due. A
    destination written as ``~/...`` never takes that form.
    """
    root = [apfs_name_key(part) for part in icloud_drive_root(home).parts]
    parts = [apfs_name_key(part) for part in canonical_path(destination).parts]
    return parts[: len(root)] == root


def _untracked_snapshots(
    destination: Path, ledger: Ledger, machine: str
) -> "tuple[tuple[str, ...], int]":
    """The snapshot folders under *destination* that *ledger* lacks, by whose they are.

    The untracked ones, sorted: bare names and names carrying *machine*. Then how many carry
    another machine's label, which are that machine's to track. Judged by ``lstat`` alone,
    so a symlink is never a folder here and nothing is followed, and at most
    UNTRACKED_SCAN_LIMIT entries are examined. A destination that cannot be listed gives
    none: that is already a problem of its own, since the manifest and upload reads beneath
    it fail too, and a note would only say it twice.
    """
    tracked = {entry.name for entry in ledger.entries}
    found: list[str] = []
    others = 0
    try:
        with os.scandir(destination) as listing:
            for item in itertools.islice(listing, UNTRACKED_SCAN_LIMIT):
                parts = split_snapshot_name(item.name)
                if (
                    item.name in tracked
                    or parts is None
                    or not item.is_dir(follow_symlinks=False)
                ):
                    continue
                if parts.machine in (None, machine):
                    found.append(item.name)
                else:
                    others += 1
    except OSError:
        return (), 0
    return tuple(sorted(found)), others


def _promoted(ledger: Ledger) -> list[LedgerEntry]:
    """The ledger's snapshots: promoted entries, never an incoming copy's leftover."""
    return [entry for entry in ledger.entries if entry.state == STATE_PROMOTED]


def _age(name: "str | None", now: datetime) -> "timedelta | None":
    """How long before *now* snapshot *name* was taken; None when it names no moment."""
    taken = parse_snapshot_name(name) if name is not None else None
    return now - taken if taken is not None else None


def _whole_hours(span: timedelta) -> int:
    """*span* in whole hours, rounded down."""
    return span // _HOUR


def _run_problems(facts: StatusFacts, last: RunRecord) -> Iterator[str]:
    """What the agent's state and the last run record say is wrong."""
    if facts.agent != AGENT_LOADED:
        yield f"the launchd agent's state could not be read ({facts.agent})"
    if last.outcome not in _UNREMARKABLE_OUTCOMES:
        yield f"last run {last.outcome}: {last.detail}"
    elif last.outcome == OUTCOME_SKIPPED:
        promoted = _promoted(facts.ledger)
        newest = max(promoted, key=lambda entry: entry.name) if promoted else None
        if newest is not None and not newest.complete:
            yield (
                f"the newest snapshot, {newest.name}, is incomplete "
                "(the last run was skipped, so its record is gone)"
            )


def _ledger_problems(facts: StatusFacts) -> Iterator[str]:
    """Whether the ledger holds more snapshots than pruning should ever leave."""
    held = len(_promoted(facts.ledger))
    if held > MAX_SNAPSHOTS_PER_KEEP * facts.keep:
        yield (
            f"the ledger holds {held} snapshots, more than {MAX_SNAPSHOTS_PER_KEEP} "
            f"times keep ({facts.keep})"
        )


def _snapshot_problems(facts: StatusFacts) -> Iterator[str]:
    """What is wrong with the newest complete snapshot: absent, misdated, stale or unread."""
    name = facts.newest_complete
    if name is None:
        yield "no complete snapshot exists"
        return
    age = _age(name, facts.now)
    if age is None:
        yield f"the newest complete snapshot's name records no time: {name}"
        return
    if age < _NO_TIME:
        yield (
            f"the newest complete snapshot, {name}, is dated after now "
            f"({facts.now.isoformat(timespec='minutes')}): the clock has been set back, "
            "or the snapshot is from the future"
        )
    if age >= STALE_AFTER:
        yield f"the newest complete snapshot, {name}, is {_whole_hours(age)} h old"
    manifest = facts.newest_manifest
    if manifest is None:
        yield (
            "the newest snapshot's manifest could not be read, or does not hash to "
            "the ledger"
        )
    elif manifest.unclassified_ignored:
        paths = ", ".join(manifest.unclassified_ignored)
        yield f"unclassified ignored paths (add each to extras or regenerable): {paths}"
    if facts.upload not in _KNOWN_UPLOADS:
        yield f"the newest snapshot's upload state could not be read ({facts.upload})"
    elif facts.upload == UPLOAD_NOT_ICLOUD and facts.in_icloud_drive:
        yield _ICLOUD_DRIVE_OFF
    elif facts.upload == UPLOAD_PENDING and age >= UPLOAD_GRACE:
        hours = _whole_hours(age)
        yield f"the newest snapshot is still not uploaded {hours} h after it was taken"


def _identity_problems(facts: StatusFacts) -> Iterator[str]:
    """Untracked folders named for this machine: its own unledgered, or a shared label."""
    named = [name for name in facts.untracked if _named_for(name, facts.machine)]
    if named:
        yield (
            f"snapshot folders named for this machine ({facts.machine}) are not in its "
            f"ledger: {_listed(named)}. Each is most likely this machine's own snapshot "
            "that a run could not ledger (a failed promote confirm or ledger save, or a "
            "restore that started with an empty ledger): check it, and keep or delete it "
            "by hand. Only if a second machine uses the same label, edit "
            f"{IDENTITY_SHOWN} on one of them"
        )


def _named_for(name: str, machine: "str | None") -> bool:
    """Whether snapshot folder *name* carries *machine*'s label; never for None."""
    parts = split_snapshot_name(name)
    return machine is not None and parts is not None and parts.machine == machine


def _listed(names: "list[str] | tuple[str, ...]") -> str:
    """The first UNTRACKED_NAMED of *names*, then a count of the rest."""
    rest = len(names) - UNTRACKED_NAMED
    more = f" and {rest} more" if rest > 0 else ""
    return ", ".join(names[:UNTRACKED_NAMED]) + more


def _notes(facts: StatusFacts) -> Iterator[str]:
    """What is worth saying that is not a problem."""
    age = _age(facts.newest_complete, facts.now)
    pending = facts.upload == UPLOAD_PENDING
    # A snapshot dated after now is already a problem, never a pending upload inside its grace.
    if pending and age is not None and _NO_TIME <= age < UPLOAD_GRACE:
        yield f"upload pending, inside its {_whole_hours(UPLOAD_GRACE)} h grace"
    if facts.upload == UPLOAD_NOT_ICLOUD and not facts.in_icloud_drive:
        yield "the destination is not in iCloud Drive; the upload check does not apply"
    if facts.untracked:
        yield (
            "the ledger does not track these snapshot folders, so this tool never prunes "
            f"them: {_listed(facts.untracked)}"
        )
    if facts.other_machines:
        count = facts.other_machines
        whose = (
            "snapshot from another machine"
            if count == 1
            else "snapshots from other machines"
        )
        yield (
            f"{count} {whose} share this destination; each is pruned by the machine "
            "that took it, never by this one"
        )
