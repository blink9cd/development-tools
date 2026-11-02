"""Load and validate ``[tool.workspace_backup]`` from the nearest pyproject.toml.

The walk and the TOML parse belong to the shared ``pyproject_config`` library; this module
owns only what this tool's keys must look like. Anything wrong is a ConfigError naming the
file, so a run fails validation (exit 2) before it touches the destination.

Whitelist entries must be relative and must not climb with ``..``: they are globbed under a
root, and an entry escaping that root would archive files nobody listed. The destination is
loaded resolved, because the ledger is keyed on its spelling.

iCloud Drive's folder is named here, once, because this is where a home-relative path becomes
a real one: status asks whether the configured destination lies inside that folder, and must
resolve both paths by the same rule for the answer to mean anything.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

# pyproject_config, the shared pyproject.toml table reader, at one fixed place: its folder
# directly under the repository root, as backup_proc.py places bounded_run.
sys.path.append(str(Path(__file__).resolve().parents[2] / "pyproject_config"))

import pyproject_config  # noqa: E402 - import follows the sys.path bootstrap above

from backup_json import is_safe_relative_name  # noqa: E402 - same bootstrap block
from backup_rearm import (  # noqa: E402 - same bootstrap block
    rearm_command_problem,
    setup_doc_problem,
)

TABLE = "workspace_backup"
DEFAULT_KEEP = 7
_LIST_KEYS = ("extras", "regenerable", "claude_files", "home_files")
_MEMORY_SNAPSHOT_KEY = "memory_snapshot"
_REARM_KEY = "rearm_commands"
_SETUP_DOC_KEY = "setup_doc"
_KNOWN_KEYS = frozenset(
    {
        "destination",
        "keep",
        _MEMORY_SNAPSHOT_KEY,
        _REARM_KEY,
        _SETUP_DOC_KEY,
        *_LIST_KEYS,
    }
)
#: iCloud Drive's local folder, relative to a home directory: its one spelling in code.
ICLOUD_DRIVE = Path("Library", "Mobile Documents", "com~apple~CloudDocs")


class ConfigError(Exception):
    """The configuration is missing, malformed, or has a value of the wrong shape."""


@dataclass(frozen=True)
class BackupConfig:
    """The validated ``[tool.workspace_backup]`` table."""

    pyproject: Path
    destination: Path
    keep: int
    extras: tuple[str, ...]
    regenerable: tuple[str, ...]
    claude_files: tuple[str, ...]
    home_files: tuple[str, ...]
    #: Whether a build runs memory_snapshot first. Off when absent: outside a workspace
    #: that ships that tool, the step could only ever record a failed item.
    memory_snapshot: bool = False
    #: The restore document's re-arm commands; None when absent, so it uses its default.
    rearm_commands: "tuple[str, ...] | None" = None
    #: The workspace's setup document, relative to its root, that the re-arm step names.
    setup_doc: "str | None" = None


def expand_home(raw: str, home: Path) -> Path:
    """*raw* with a leading ``~`` expanded against the injected *home*, never ``$HOME``."""
    if raw == "~":
        return home
    if raw.startswith("~/"):
        return home / raw[2:]
    return Path(raw)


def canonical_path(path: Path) -> Path:
    """*path* resolved the way a destination is: links followed, never required to exist."""
    return Path(os.path.realpath(path))


def icloud_drive_root(home: Path) -> Path:
    """The iCloud Drive folder under the injected *home*, resolved as a destination is."""
    return canonical_path(home / ICLOUD_DRIVE)


def load_config(
    start: Path,
    *,
    home: Path,
    is_file: pyproject_config.IsFileFn = pyproject_config.default_is_file,
    read_text: pyproject_config.ReadTextFn = pyproject_config.default_read_text,
) -> BackupConfig:
    """The validated table from the nearest pyproject.toml at or above *start*."""
    try:
        found = pyproject_config.read_tool_table(
            start, table=TABLE, is_file=is_file, read_text=read_text
        )
    except pyproject_config.ConfigError as exc:
        raise ConfigError(str(exc)) from exc
    if found is None:
        raise ConfigError(f"no pyproject.toml at or above {start}")
    pyproject, table = found
    if not table:
        raise ConfigError(f"{pyproject} has no [tool.{TABLE}] table")
    return _validated(pyproject, table, home)


def _validated(pyproject: Path, table: dict[str, object], home: Path) -> BackupConfig:
    unknown = sorted(set(table) - _KNOWN_KEYS)
    if unknown:
        raise ConfigError(
            f"{pyproject}: unknown [tool.{TABLE}] keys: {', '.join(unknown)}"
        )
    destination = _destination(pyproject, table, home)
    keep = table.get("keep", DEFAULT_KEEP)
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
        raise ConfigError(f"{pyproject}: keep must be a positive integer")
    memory_snapshot = table.get(_MEMORY_SNAPSHOT_KEY, False)
    if not isinstance(memory_snapshot, bool):
        raise ConfigError(f"{pyproject}: {_MEMORY_SNAPSHOT_KEY} must be true or false")
    lists = {key: _entries(pyproject, table, key) for key in _LIST_KEYS}
    return BackupConfig(
        pyproject=pyproject,
        destination=destination,
        keep=keep,
        extras=lists["extras"],
        regenerable=lists["regenerable"],
        claude_files=lists["claude_files"],
        home_files=lists["home_files"],
        memory_snapshot=memory_snapshot,
        rearm_commands=_rearm_commands(pyproject, table),
        setup_doc=_setup_doc(pyproject, table),
    )


def _destination(pyproject: Path, table: dict[str, object], home: Path) -> Path:
    """The destination, absolute and resolved, so one directory has exactly one spelling.

    backup_ledger keys the state directory on this path's string, so a second spelling of
    the same directory, through a symlink or with a ``..``, would get a separate, empty
    ledger. ``os.path.realpath`` is non-strict: it only lstats components and reads links, so
    the destination need not exist yet and no file is opened. ``Path.resolve`` is not used
    because on 3.11 and 3.12 it also stats the result and raises RuntimeError on a link loop.

    The absolute check comes first, because realpath would join a relative path to the cwd.
    A NUL is refused before either: realpath raises ValueError on one, not a ConfigError.

    Limit: case is not canonicalized. realpath keeps the case it is given, so on a
    case-insensitive volume such as default APFS, ``~/Backup`` and ``~/backup`` name one
    directory yet key two ledgers. Spell the destination one way.
    """
    raw = table.get("destination")
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ConfigError(
            f"{pyproject}: destination must be a non-empty string without NUL"
        )
    destination = expand_home(raw, home)
    if not destination.is_absolute():
        raise ConfigError(f"{pyproject}: destination must be absolute or start with ~/")
    return canonical_path(destination)


def _entries(pyproject: Path, table: dict[str, object], key: str) -> tuple[str, ...]:
    """The strings at *key*, each one a name the manifest parser would accept.

    Entries become zip member names, and the manifest parser refuses any member that fails
    ``is_safe_relative_name``, so config applies that same predicate rather than a second
    rule, which could admit a name (``./a``, ``a//b``) whose snapshot would then read as
    another tool's. Glob characters are ordinary characters to the predicate.
    """
    raw = table.get(key, [])
    if not isinstance(raw, list):
        raise ConfigError(f"{pyproject}: {key} must be a list of strings")
    entries: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not is_safe_relative_name(item):
            raise ConfigError(
                f"{pyproject}: {key} entry is not a relative name that stays under its root: "
                f"{item!r}"
            )
        entries.append(item)
    return tuple(entries)


def _rearm_commands(
    pyproject: Path, table: dict[str, object]
) -> "tuple[str, ...] | None":
    """The listed re-arm commands, each one safe inside RESTORE.md's fence; None when absent.

    An empty list is refused rather than read as "no step": a workspace that wants only the
    default omits the key.
    """
    raw = table.get(_REARM_KEY)
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        raise ConfigError(
            f"{pyproject}: {_REARM_KEY} must be a non-empty list of strings"
        )
    commands: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ConfigError(
                f"{pyproject}: {_REARM_KEY} entry is not a string: {item!r}"
            )
        problem = rearm_command_problem(item)
        if problem is not None:
            raise ConfigError(f"{pyproject}: {_REARM_KEY} entry {problem}: {item!r}")
        commands.append(item)
    return tuple(commands)


def _setup_doc(pyproject: Path, table: dict[str, object]) -> "str | None":
    """The setup document the re-arm step names, safe in its prose; None when absent."""
    raw = table.get(_SETUP_DOC_KEY)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ConfigError(f"{pyproject}: {_SETUP_DOC_KEY} must be a string")
    problem = setup_doc_problem(raw)
    if problem is not None:
        raise ConfigError(f"{pyproject}: {_SETUP_DOC_KEY} {problem}: {raw!r}")
    return raw
