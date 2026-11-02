"""One backup run: lock -> validate -> snapshot memory -> build -> verify -> promote -> prune -> record.

A validation failure exits 2 without touching the destination; a snapshot promoted but
incomplete exits 1; only a verified, complete snapshot exits 0. Every non-dry run that takes
the lock records its outcome under the destination's state directory, validation failures
and ``--if-due`` skips included, so status can tell "never ran" from "fails every day". The
record is written first, then any non-zero exit is posted, then the outcome is printed. A run
that finds the lock held prints its skip and exits 1, but neither records nor posts it: a
manual run racing the agent is not a fault, and the holder's record (perhaps a failure it
is posting at that moment) must survive it. A record that cannot be written is said in what
is printed and posted, and makes the run need attention whatever it came to.

What goes wrong while the run holds its lock becomes one of three things:

- a failed item on a snapshot that is still promoted: a build or verification failure, or
  one backup_seal records while sealing the staged snapshot;
- a failed run, exit 1: staging that cannot be made fresh, a promote refused, a manifest that
  does not read back, any other filesystem error, and, as the last resort, any other
  exception, recorded and posted as an internal error, its traceback printed to stderr after;
- an unrunnable run, exit 2: validation, the ledger, or the lock itself.

Only ``Exception`` is caught, so an interrupt still stops the run. Once the outcome is recorded
and posted, printing it is best effort: an output stream that cannot be written loses the line,
never the exit code. Releasing the lock afterward is not guarded, and a lock that cannot be
released still raises.

The ledger records a promoted snapshot only once its manifest reads back from the destination,
within a bound, as the bytes written. Until then the folder is one prune never touches, the
safe direction if the run dies in between, and a run that cannot confirm prunes nothing.
"""

from __future__ import annotations

import os
import stat
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TextIO

from backup_build import BuildRequest, build_snapshot, memory_repos
from backup_config import BackupConfig
from backup_fs import RunLock, read_bytes_bounded, sha256_bytes
from backup_machine import MachineError, ensure_identity, identity_path, read_identity
from backup_ledger import (
    LedgerError,
    load_ledger,
    save_ledger,
    state_dir_for,
    write_last_run,
)
from backup_manifest import MANIFEST_NAME
from backup_model import (
    OUTCOME_COMPLETE,
    OUTCOME_FAILED,
    OUTCOME_INCOMPLETE,
    OUTCOME_SKIPPED,
    STATE_PROMOTED,
    TOOL_ID,
    FailedItem,
    Ledger,
    LedgerEntry,
    Manifest,
    RunRecord,
)
from backup_proc import RunCmd
from backup_rearm import rearm_for
from backup_promote import (
    MANIFEST_READ_TIMEOUT_S,
    PromoteError,
    PruneResult,
    parse_snapshot_name,
    promote,
    prune,
    snapshot_name,
)
from backup_roots import Roots
from backup_seal import manifest_bytes, verify_in_scratch, write_snapshot_records
from backup_staging import StagingError, fresh_staging

LOCK_NAME = "run.lock"
STAGING_DIR = "staging"
#: What a run that finds the lock held prints, after the skipped outcome.
HELD_LOCK_DETAIL = "another run holds the lock"
EXIT_OK = 0
EXIT_ATTENTION = 1
EXIT_UNRUNNABLE = 2
DARWIN = "darwin"
NOTIFY_TITLE = TOOL_ID
GIT = "git"


@dataclass(frozen=True)
class RunRequest:
    """What to back up, where the tool keeps its state, and how this run was asked for.

    *base* is the tool's own directory (lock, state, staging); the run resolves it once, so
    staging and the lock are always derived from one spelling of it.
    """

    roots: Roots
    config: BackupConfig
    base: Path
    tools_dir: Path
    python: str
    if_due: bool
    dry_run: bool
    platform: str


@dataclass(frozen=True)
class RunDeps:
    """The run's seams: commands, clock, PATH lookup, notification, host name, streams."""

    run: RunCmd
    now: Callable[[], datetime]
    which: Callable[[str], "str | None"]
    notify: Callable[[str, str], bool]
    #: Read only on the first run, to derive this machine's identity (backup_machine).
    hostname: Callable[[], str]
    out: TextIO
    err: TextIO


@dataclass(frozen=True)
class _Outcome:
    """What one run came to: the record to write and the exit code."""

    outcome: str
    detail: str
    code: int
    snapshot: "str | None" = None
    failed: tuple[FailedItem, ...] = ()
    #: A defect's traceback, printed after the outcome is recorded and posted; "" for none.
    trace: str = ""


def run_backup(request: RunRequest, deps: RunDeps) -> int:
    """Run once and return the exit code; what the host does never raises out of here."""
    if request.dry_run:
        return _print_plan(request, deps)
    base = Path(os.path.realpath(request.base))
    state = state_dir_for(base, request.config.destination)
    lock = RunLock(lock_path(request.base))
    try:
        held = lock.acquire()
    except OSError as exc:
        detail = f"could not take the run lock: {exc}"
        return _finish(state, deps, _Outcome(OUTCOME_FAILED, detail, EXIT_UNRUNNABLE))
    if not held:
        # Printed, never recorded: the holder's record (a failure it is posting right now,
        # say) must outlive a skip that races it, and only the holder writes under the lock.
        print(f"{OUTCOME_SKIPPED}: {HELD_LOCK_DETAIL}", file=deps.err)
        return EXIT_ATTENTION
    try:
        return _finish(state, deps, _run_recording_defects(request, deps, base, state))
    finally:
        lock.release()


def _run_recording_defects(
    request: RunRequest, deps: RunDeps, base: Path, state: Path
) -> _Outcome:
    """:func:`_locked_run`, with any exception it lets escape recorded as a failed run.

    The last resort behind the named catches, so a raise site nobody listed still leaves a
    record, a notification and exit 1 rather than a traceback in the agent's log alone. The
    traceback is kept, not printed here: it is printed once the outcome is recorded and
    posted, because it is a defect to diagnose while stderr is only best effort, and a stream
    that cannot be written must not cost the record. Exception only, never BaseException, so
    an interrupt or an exit still stops the run.
    """
    try:
        return _locked_run(request, deps, base, state)
    except Exception as exc:
        # A note is what a raise site added on the way out, such as scratch it could not
        # remove: recorded with the defect, or only the traceback would say it.
        notes = "".join(f"; {note}" for note in getattr(exc, "__notes__", ()))
        detail = f"internal error: {type(exc).__name__}: {exc}{notes}"
        trace = traceback.format_exc()
        return _Outcome(OUTCOME_FAILED, detail, EXIT_ATTENTION, trace=trace)


def lock_path(base: Path) -> Path:
    """The run lock's file for the tool directory *base*: the one spelling every reader uses.

    *base* is resolved first, as a run resolves it, so install and uninstall probe the very
    file a run locks.
    """
    return Path(os.path.realpath(base)) / LOCK_NAME


def summarize_run(name: str, failed: Sequence[FailedItem], pruned: PruneResult) -> str:
    """One line describing a promoted snapshot's run.

    Deferred prunes are always listed: a folder that defers on every run is space that is
    never reclaimed, and this line is where that shows.
    """
    parts = [f"snapshot {name}"]
    if failed:
        reasons = "; ".join(f"{item.item}: {item.reason}" for item in failed)
        parts.append(f"{len(failed)} failed item(s): {reasons}")
    if pruned.deleted:
        parts.append(f"pruned {len(pruned.deleted)}")
    if pruned.deferred:
        parts.append("prune deferred: " + ", ".join(pruned.deferred))
    return "; ".join(parts)


def _locked_run(
    request: RunRequest, deps: RunDeps, base: Path, state: Path
) -> _Outcome:
    """Validate, read the ledger, honor --if-due, then snapshot; what the run came to."""
    problem = _validation_problem(request, deps)
    if problem is not None:
        return _Outcome(OUTCOME_FAILED, problem, EXIT_UNRUNNABLE)
    try:
        ledger = load_ledger(state)
        machine = ensure_identity(base, deps.hostname)
    except (LedgerError, MachineError) as exc:
        return _Outcome(OUTCOME_FAILED, str(exc), EXIT_UNRUNNABLE)
    except OSError as exc:
        detail = f"could not read the ledger: {exc}"
        return _Outcome(OUTCOME_FAILED, detail, EXIT_UNRUNNABLE)
    now = deps.now()
    if request.if_due and _has_complete_on(ledger, now):
        detail = "a complete snapshot already exists today"
        return _Outcome(OUTCOME_SKIPPED, detail, EXIT_OK)
    try:
        return _snapshot_run(request, deps, base, state, ledger, (now, machine))
    except StagingError as exc:
        return _Outcome(OUTCOME_FAILED, str(exc), EXIT_ATTENTION)
    except OSError as exc:
        # Recorded and posted, not raised: an unattended run that dies with a traceback
        # leaves status reading a stale record, and nobody is told.
        detail = f"unexpected filesystem error: {exc}"
        return _Outcome(OUTCOME_FAILED, detail, EXIT_ATTENTION)


def _snapshot_run(
    request: RunRequest,
    deps: RunDeps,
    base: Path,
    state: Path,
    ledger: Ledger,
    taken: "tuple[datetime, str]",
) -> _Outcome:
    """Build, verify and seal a snapshot taken at a moment by a machine, then promote it.

    The build never archives the identity file: a machine set up from this snapshot must
    derive an identity of its own.
    """
    now, machine = taken
    name = snapshot_name(now, machine)
    staging = fresh_staging(base / STAGING_DIR, name)
    build_request = BuildRequest(
        request.roots,
        request.config,
        staging,
        request.tools_dir,
        request.python,
        identity_file=identity_path(base),
    )
    build = build_snapshot(build_request, run=deps.run)
    failed = build.failed + verify_in_scratch(staging, build, run=deps.run)
    config = request.config
    rearm = rearm_for(config.rearm_commands, request.roots.workspace, config.setup_doc)
    manifest = write_snapshot_records(
        staging, now, build, failed, rearm=rearm, machine=machine
    )
    return _promote_and_prune(request.config, state, staging, name, ledger, manifest)


def _promote_and_prune(
    config: BackupConfig,
    state: Path,
    staging: Path,
    name: str,
    ledger: Ledger,
    manifest: Manifest,
) -> _Outcome:
    """Promote *staging*, ledger it once its manifest reads back, then prune."""
    try:
        target, ledger = promote(
            staging,
            config.destination,
            name,
            ledger,
            save_ledger=lambda updated: save_ledger(state, updated),
        )
    except PromoteError as exc:
        detail = f"promote failed: {exc}"
        return _Outcome(OUTCOME_FAILED, detail, EXIT_ATTENTION, None, manifest.failed)
    unconfirmed = _confirmation_problem(target, manifest)
    if unconfirmed is not None:
        # promote may have dropped an incoming entry from what it hands back; keep that,
        # but never the promoted entry, so prune can never delete on an unproven hash.
        unsaved = _save_problem(state, ledger)
        detail = f"promoted but not confirmed: {unconfirmed}"
        if unsaved is not None:
            detail = f"{detail}; {unsaved}"
        return _Outcome(OUTCOME_FAILED, detail, EXIT_ATTENTION, name, manifest.failed)
    manifest_sha256 = sha256_bytes(manifest_bytes(manifest))
    ledger = ledger.with_entry(
        LedgerEntry(name, STATE_PROMOTED, manifest.complete, manifest_sha256)
    )
    # Saved before prune runs: prune is the step with no undo, and must never run while
    # the newest snapshot is not yet durably in the ledger. A good snapshot the ledger does
    # not hold is never pruned either, so it is named rather than left for nobody to find.
    unsaved = _save_problem(state, ledger)
    if unsaved is not None:
        detail = f"snapshot {name} is promoted and confirmed, but nothing was pruned: {unsaved}"
        return _Outcome(OUTCOME_FAILED, detail, EXIT_ATTENTION, name, manifest.failed)
    # The run's own snapshot is protected: a clock set back far enough names it before keep
    # snapshots already held, which would otherwise put it past the newest keep.
    pruned = prune(config.destination, ledger, config.keep, protected=(name,))
    save_ledger(state, pruned.ledger)
    outcome = OUTCOME_COMPLETE if manifest.complete else OUTCOME_INCOMPLETE
    code = EXIT_OK if manifest.complete else EXIT_ATTENTION
    detail = summarize_run(name, manifest.failed, pruned)
    return _Outcome(outcome, detail, code, name, manifest.failed)


def _save_problem(state: Path, ledger: Ledger) -> "str | None":
    """Save *ledger* under *state*; why that failed, or None."""
    try:
        save_ledger(state, ledger)
    except OSError as exc:
        return f"the ledger could not be saved: {exc}"
    return None


def _confirmation_problem(target: Path, manifest: Manifest) -> "str | None":
    """Why the promoted manifest does not read back as the bytes written, or None."""
    path = target / MANIFEST_NAME
    data = read_bytes_bounded(path, MANIFEST_READ_TIMEOUT_S)
    if data is None:
        return f"{path} could not be read within {MANIFEST_READ_TIMEOUT_S:g}s"
    if sha256_bytes(data) != sha256_bytes(manifest_bytes(manifest)):
        return f"{path} does not hold the manifest written"
    return None


def _validation_problem(request: RunRequest, deps: RunDeps) -> "str | None":
    """Why this run cannot start, or None."""
    if request.platform != DARWIN:
        return f"macOS only; this platform is {request.platform}"
    if deps.which(GIT) is None:
        return "git is not on PATH"
    return _parent_problem(request.config.destination.parent)


def _parent_problem(parent: Path) -> "str | None":
    """Why the destination's *parent* is not a directory to promote under, or None.

    A missing parent means the iCloud Drive folder is absent, and promote never creates it.
    An explicit ``stat``, because for errors outside a short list ``Path.is_dir`` on 3.11 and
    3.12 raises; later interpreters (3.14 measured) return False for every error.
    """
    try:
        mode = parent.stat().st_mode
    except FileNotFoundError:
        return f"the destination's parent does not exist: {parent}"
    except OSError as exc:
        return f"the destination's parent cannot be examined: {exc}"
    if not stat.S_ISDIR(mode):
        return f"the destination's parent is not a directory: {parent}"
    return None


def _has_complete_on(ledger: Ledger, now: datetime) -> bool:
    """Whether the ledger holds a promoted, complete snapshot taken on *now*'s local date."""
    return any(
        entry.state == STATE_PROMOTED and entry.complete and _taken_on(entry.name, now)
        for entry in ledger.entries
    )


def _taken_on(name: str, now: datetime) -> bool:
    """Whether snapshot *name* was taken on *now*'s date; False for any other name."""
    taken = parse_snapshot_name(name)
    return taken is not None and taken.date() == now.date()


def _finish(state: Path, deps: RunDeps, result: _Outcome) -> int:
    """Record *result*, post it unless it exits 0, then print it; the exit code.

    The record and the notification are the signals that must survive, so both come before
    anything is printed, and every print is best effort (:func:`_say`).
    """
    problem = _record_problem(state, deps, result)
    if problem is not None:
        # Status would go on reading an older record, so the run needs attention whatever
        # it came to, and is posted even when its outcome alone would exit 0.
        result = replace(
            result,
            detail=f"{result.detail}; {problem}",
            code=max(result.code, EXIT_ATTENTION),
        )
    line = f"{result.outcome}: {result.detail}"
    posted = result.code == EXIT_OK or deps.notify(NOTIFY_TITLE, line)
    _say(deps.out if result.code == EXIT_OK else deps.err, line)
    if not posted:
        _say(deps.err, "notification could not be posted")
    if result.trace:
        _say(deps.err, result.trace.rstrip("\n"))
    return result.code


def _say(stream: TextIO, text: str) -> None:
    """Print *text* to *stream*, best effort: a stream that cannot be written loses the line.

    Only ever called once the outcome is recorded and posted. A broken pipe or a closed log
    raises OSError or ValueError from print; letting either escape would turn a recorded,
    posted exit code into a traceback, which is worse than the one line it loses.
    """
    try:
        print(text, file=stream)
    except (OSError, ValueError):
        return


def _record_problem(state: Path, deps: RunDeps, result: _Outcome) -> "str | None":
    """Write *result* as the last run, atomically; why that failed, or None."""
    finished = deps.now().astimezone().isoformat(timespec="seconds")
    record = RunRecord(
        finished, result.outcome, result.detail, result.snapshot, result.failed
    )
    try:
        write_last_run(state, record)
    except OSError as exc:
        return f"last run not recorded: {exc}"
    return None


def _print_plan(request: RunRequest, deps: RunDeps) -> int:
    """Print what a run would do, having written nothing; exit as the run would start."""
    problem = _validation_problem(request, deps)
    if problem is not None:
        print(f"cannot run: {problem}", file=deps.err)
        return EXIT_UNRUNNABLE
    try:
        machine = read_identity(Path(os.path.realpath(request.base)), deps.hostname)
    except MachineError as exc:
        print(f"cannot run: {exc}", file=deps.err)
        return EXIT_UNRUNNABLE
    config, roots = request.config, request.roots
    code = EXIT_OK
    try:
        memory = str(len(memory_repos(roots.claude_home)))
    except OSError as exc:
        memory, code = f"cannot list: {exc}", EXIT_ATTENTION
    lines = [
        f"would write: {config.destination / snapshot_name(deps.now(), machine)}",
        f"workspace: {roots.workspace}",
        f"memory repositories: {memory}",
        f"extras: {', '.join(config.extras) or '(none)'}",
        f"claude files: {', '.join(config.claude_files) or '(none)'}",
        f"home files: {', '.join(config.home_files) or '(none)'}",
        f"keep: {config.keep} complete snapshots",
    ]
    print("\n".join(lines), file=deps.out)
    return code
