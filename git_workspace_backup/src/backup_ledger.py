"""The per-destination ledger and last-run record, and where they live.

State is keyed by destination, a short hash of the destination path, so a run against a
scratch destination can never overwrite the real destination's record. The ledger is the
authority prune deletes by, so its parser refuses any entry name that is not one plain path
component: a name holding ``/`` or ``..`` could otherwise point a deletion outside the
destination. That rule is the manifest's relative-name rule plus "no ``/``", built on the
same predicate so the two cannot disagree about what escapes. It also refuses a name that
appears twice, which this tool never writes: an incoming and a promoted row of one name would
plan it as a leftover, and a leftover is deleted without a manifest proof.
"""

from __future__ import annotations

from dataclasses import asdict, fields
from pathlib import Path

from backup_fs import read_regular_file, sha256_bytes, write_private_text
from backup_json import (
    ShapeError,
    as_object,
    envelope_to_json,
    get_bool,
    get_list,
    get_optional_str,
    get_str,
    open_envelope,
    relative_posix_name,
    require_keys,
)
from backup_manifest import failed_item
from backup_model import (
    LEDGER_STATES,
    OUTCOMES,
    STATE_PROMOTED,
    Ledger,
    LedgerEntry,
    RunRecord,
)

LEDGER_NAME = "ledger.json"
LAST_RUN_NAME = "last.json"
STATE_DIR_NAME = "state"
#: The most a ledger or run record read back may hold: far more than this tool writes, and a
#: bound on what a planted or damaged file can make a run or status read.
STATE_READ_LIMIT = 1 << 22
#: Hex characters of the destination hash used as the state key.
_KEY_LENGTH = 12
#: Each record's own fields, derived from the record so the writer and reader cannot drift.
_LEDGER_KEYS = frozenset(field.name for field in fields(Ledger))
_RUN_RECORD_KEYS = frozenset(field.name for field in fields(RunRecord))


class LedgerError(Exception):
    """The ledger file exists but is not one this tool wrote."""


def state_dir_for(base: Path, destination: Path) -> Path:
    """The state directory for *destination* under the tool's *base* directory.

    *destination* must already be absolute and resolved, as the config layer supplies it:
    the key hashes the path's spelling, so a differently spelled path to the same folder
    would get a separate state directory.
    """
    key = sha256_bytes(str(destination).encode("utf-8"))[:_KEY_LENGTH]
    return base / STATE_DIR_NAME / key


def ledger_to_json(ledger: Ledger) -> str:
    """The ledger in this tool's envelope as stable JSON."""
    return envelope_to_json(asdict(ledger))


# fuzz-target
def parse_ledger(data: bytes) -> "Ledger | None":
    """The ledger in *data*, or None when it is not exactly this tool's schema."""
    try:
        obj = open_envelope(data, _LEDGER_KEYS)
        entries = get_list(obj, "entries", _ledger_entry)
        _refuse_repeated_names(entries)
        return Ledger(entries)
    except ShapeError:
        return None


def load_ledger(state: Path) -> Ledger:
    """The ledger under *state*; empty when absent, LedgerError when present but not ours.

    Read as :func:`_read_state_file` reads it, so an OSError for one that cannot be read (a
    symlink or a directory in its place, more than ``STATE_READ_LIMIT`` bytes, no
    permission) propagates to the caller.
    """
    path = state / LEDGER_NAME
    data = _read_state_file(path)
    if data is None:
        return Ledger()
    ledger = parse_ledger(data)
    if ledger is None:
        raise LedgerError(
            f"{path} is not a ledger this tool wrote; move it aside to start a fresh one"
        )
    return ledger


def save_ledger(state: Path, ledger: Ledger) -> None:
    """Write the ledger under *state* atomically."""
    write_private_text(state / LEDGER_NAME, ledger_to_json(ledger))


def newest_complete_entry(ledger: Ledger) -> "LedgerEntry | None":
    """The newest promoted, complete snapshot's entry (names sort chronologically)."""
    complete = [e for e in ledger.entries if e.state == STATE_PROMOTED and e.complete]
    return max(complete, key=lambda entry: entry.name) if complete else None


def newest_complete_name(ledger: Ledger) -> "str | None":
    """The name of :func:`newest_complete_entry`: one selection rule for both."""
    entry = newest_complete_entry(ledger)
    return entry.name if entry is not None else None


def run_record_to_json(record: RunRecord) -> str:
    """The run record in this tool's envelope as stable JSON."""
    return envelope_to_json(asdict(record))


# fuzz-target
def parse_run_record(data: bytes) -> "RunRecord | None":
    """The run record in *data*, or None when it is not exactly this tool's schema."""
    try:
        obj = open_envelope(data, _RUN_RECORD_KEYS)
        outcome = get_str(obj, "outcome")
        if outcome not in OUTCOMES:
            return None
        return RunRecord(
            finished=get_str(obj, "finished"),
            outcome=outcome,
            detail=get_str(obj, "detail"),
            snapshot=get_optional_str(obj, "snapshot"),
            failed=get_list(obj, "failed", failed_item),
        )
    except ShapeError:
        return None


def write_last_run(state: Path, record: RunRecord) -> None:
    """Write the run record under *state* atomically."""
    write_private_text(state / LAST_RUN_NAME, run_record_to_json(record))


def read_last_run(state: Path) -> "RunRecord | None":
    """The last run record under *state*; None when absent or when it is not ours.

    Read as :func:`_read_state_file` reads it, so an OSError for one that cannot be read
    propagates to the caller.
    """
    data = _read_state_file(state / LAST_RUN_NAME)
    return None if data is None else parse_run_record(data)


def _read_state_file(path: Path) -> "bytes | None":
    """*path*'s bytes, read as a regular file, never through a symlink, within a bound.

    None when it is absent, or when a path above it is not a directory (a file where the state
    directory should be), which is a state never recorded. Any other OSError propagates.
    """
    try:
        data, _ = read_regular_file(path.parent, path.name, limit=STATE_READ_LIMIT)
    except (FileNotFoundError, NotADirectoryError):
        return None
    return data


def _refuse_repeated_names(entries: tuple[LedgerEntry, ...]) -> None:
    """Raise ShapeError when a name appears twice: this tool never writes one."""
    names = [entry.name for entry in entries]
    if len(set(names)) != len(names):
        raise ShapeError("a ledger entry name appears more than once")


def _ledger_entry(value: object) -> LedgerEntry:
    obj = as_object(value)
    require_keys(obj, frozenset({"name", "state", "complete", "manifest_sha256"}))
    name = relative_posix_name(obj, "name")
    if "/" in name:
        raise ShapeError(f"ledger entry name is not one path component: {name!r}")
    state = get_str(obj, "state")
    if state not in LEDGER_STATES:
        raise ShapeError(f"unknown ledger state: {state!r}")
    return LedgerEntry(
        name, state, get_bool(obj, "complete"), get_str(obj, "manifest_sha256")
    )
