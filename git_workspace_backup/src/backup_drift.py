"""Classify the primary checkout's gitignored paths: backed up, regenerable, or unclassified.

An unclassified path is a gitignored file nobody has decided about: the shape a new
irreplaceable folder takes on the day it appears. Status fails on any, and the remedy is
always one line in ``extras`` or ``regenerable``.

Git lists ignored paths with ``--ignored=matching``, collapsing to one ``dir/`` entry only a
directory that itself matches an ignore pattern (a directory whose files are each ignored
by their own patterns is listed file by file), and ``-z`` keeps names with spaces or quotes
unmangled. Three things about that invocation are load-bearing:

- ``--untracked-files=normal`` is explicit because git refuses ``--ignored=matching`` next to
  a ``status.showUntrackedFiles=no`` config, which would fail every drift check;
- ``--no-optional-locks`` stops status refreshing a stale index under ``index.lock``, the
  lock a commit the user is making at that moment needs;
- under ``-z`` a rename or copy record's original path follows as a bare record of its own,
  so it is consumed rather than read as a status line (a file named ``!! x`` would otherwise
  be listed as ignored).

Every listed path is display-only: it is reported, never joined onto a filesystem path.

The pattern rules here are the tool's only ones. backup_zip enumerates the same ``extras``
entries with :func:`pattern_matches` and prunes its walk with
:func:`pattern_reaches_beneath`, so a path drift calls backed up is one the archive holds.
"""

from __future__ import annotations

from collections.abc import Sequence
from fnmatch import fnmatchcase
from pathlib import Path

from backup_proc import GIT_TIMEOUT_S, RunCmd

_STATUS_ARGV = (
    "git",
    "--no-optional-locks",
    "status",
    "--porcelain",
    "--ignored=matching",
    "--untracked-files=normal",
    "-z",
)
#: A porcelain v1 record is ``XY PATH``: two status columns, one space, then the path.
_STATUS_WIDTH = 2
_IGNORED_STATUS = "!!"
#: Either status column holding one of these means an original path follows the record.
_RENAME_OR_COPY = frozenset("RC")
_DOUBLE_STAR = "**"


class DriftError(Exception):
    """git could not list the checkout's ignored paths."""


def list_ignored(workspace: Path, *, run: RunCmd) -> tuple[str, ...]:
    """The checkout's ignored paths, sorted, repo-relative, without a trailing slash.

    Raises :class:`DriftError` on any non-zero exit, a timeout or a missing git included:
    an empty listing would read as "nothing unclassified" when nothing was checked.
    """
    code, out, err = run(_STATUS_ARGV, workspace, GIT_TIMEOUT_S)
    if code != 0:
        raise DriftError(f"git status --ignored failed (exit {code}): {err.strip()}")
    return parse_ignored(out)


# fuzz-target
def parse_ignored(out: str) -> tuple[str, ...]:
    """The ignored paths in ``git status --porcelain -z`` output *out*, sorted.

    A record that is not ``XY PATH`` is skipped, as is one whose path is empty once its
    trailing slashes are removed. Never raises.
    """
    ignored: list[str] = []
    records = iter(out.split("\0"))
    for record in records:
        if len(record) <= _STATUS_WIDTH + 1 or record[_STATUS_WIDTH] != " ":
            continue
        status = record[:_STATUS_WIDTH]
        path = record[_STATUS_WIDTH + 1 :].rstrip("/")
        if _RENAME_OR_COPY.intersection(status):
            next(records, None)
        elif status == _IGNORED_STATUS and path:
            ignored.append(path)
    return tuple(sorted(ignored))


# fuzz-target
def pattern_matches(pattern: str, path: str) -> bool:
    """Whether *pattern* matches *path* or one of its ancestor directories.

    Component-wise ``fnmatchcase``: ``*`` and ``?`` never cross ``/``, and a ``**``
    component matches any number of components, none included. A pattern with no components,
    empty or slashes alone, names no path, so it matches nothing; configuration refuses
    one already, and this keeps it from covering every path if one ever gets through.
    """
    components = _components(pattern)
    return bool(components) and _matches_a_prefix(components, tuple(path.split("/")))


# fuzz-target
def pattern_reaches_beneath(pattern: str, directory: str) -> bool:
    """Whether *pattern* matches *directory* or an ancestor, or could match a path beneath it.

    False means no path at or below *directory* can match, so a walk may skip it without
    missing anything :func:`pattern_matches` would accept. True when the whole pattern is
    consumed within the directory, or some prefix of it consumes exactly the directory's
    components with the rest left to match deeper. False for a pattern with no components,
    which :func:`pattern_matches` never accepts.
    """
    components = _components(pattern)
    if not components:
        return False
    parts = tuple(directory.split("/"))
    counts = _consumed_counts(components, parts)
    fully_consumed = len(counts) == len(components) + 1
    return fully_consumed or any(len(parts) in consumed for consumed in counts)


def unclassified(
    ignored: Sequence[str], *, extras: Sequence[str], regenerable: Sequence[str]
) -> tuple[str, ...]:
    """The ignored paths that neither list covers."""
    patterns = (*extras, *regenerable)
    return tuple(
        path for path in ignored if not any(pattern_matches(p, path) for p in patterns)
    )


def _components(pattern: str) -> tuple[str, ...]:
    """*pattern*'s non-empty ``/``-separated components."""
    return tuple(part for part in pattern.split("/") if part)


def _matches_a_prefix(pattern: tuple[str, ...], parts: tuple[str, ...]) -> bool:
    """Whether *pattern* consumes some leading run of *parts*: the path or an ancestor."""
    return len(_consumed_counts(pattern, parts)) == len(pattern) + 1


def _consumed_counts(
    pattern: tuple[str, ...], parts: tuple[str, ...]
) -> list[set[int]]:
    """Every count of *parts* the pattern could have consumed, after each of its components.

    Element 0 is ``{0}``, for the empty prefix, and element *i* follows pattern component
    *i*. The list stops at the first component that leaves no count, so it is shorter than
    ``len(pattern) + 1`` exactly when no prefix of *parts* is consumed by the whole pattern.
    Tracking every reachable count, rather than backtracking over each way to split the path
    among ``**`` components, bounds the cost at one component match per pattern component
    per path component.
    """
    consumed = {0}
    counts = [consumed]
    for component in pattern:
        if component == _DOUBLE_STAR:
            consumed = set(range(min(consumed), len(parts) + 1))
        else:
            consumed = {
                count + 1
                for count in consumed
                if count < len(parts) and fnmatchcase(parts[count], component)
            }
        if not consumed:
            break
        counts.append(consumed)
    return counts
