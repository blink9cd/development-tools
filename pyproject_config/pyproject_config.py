#!/usr/bin/env python3
"""Own the one way a gate reads its own ``[tool.<name>]`` config out of a pyproject.

Why this exists
---------------
A gate, a tool that checks a tree, usually carries standing config -- exclude lists,
allow lists, exempt lists -- in a ``[tool.<name>]`` table in the repository's
``pyproject.toml``. Reading it correctly is four separate decisions, and all four
are easy to get subtly wrong in a way no test of the *gate* would notice:

1. **Where to look.** The nearest ``pyproject.toml`` at or above the TARGET path,
   never the process working directory, because the same files otherwise pass from
   one cwd and fail from another.
2. **What absence means.** No pyproject, no table, or no key are all legitimately
   "no config" and must yield an empty list. Absence is not an error.
3. **What malformed means.** A file that will not parse, or a key holding
   something other than a list of strings, must STOP the gate. This is the half
   that matters: a reader that treated unparseable config as empty would silently
   shrink the set of things being checked and still print PASS -- the exact false
   clean a gate exists to prevent.
4. **What the paths are relative to.** The directory of the pyproject that
   supplied them, so an entry means the same thing regardless of who is asking.

That is a policy, not a snippet, so it is decided once, here.

What it owns, and what it deliberately does not
-----------------------------------------------
It owns **path-list keys**: given a table name and some key names, it returns each
key's value as resolved :class:`~pathlib.Path` objects. The table name and the key
names are both **parameters** -- there is nothing about any one consumer baked in,
which is the property :func:`read_path_lists`'s tests pin by reading two different
tables out of one document.

It also owns what such an entry MEANS once resolved, which is the half that drifts
most quietly because every tool re-decides it in passing:

* :func:`is_under` -- an entry names a **subtree**, not one path. A tool that
  compared for equality would silently walk into everything below a directory its
  config excluded, and report PASS on files nobody meant to check.
* :func:`dead_paths` -- an entry that no longer resolves is **stale config that
  gates**, unless it was declared prospective. Without that, an allow/exempt list
  keeps excusing folders that no longer exist, so the exclusion outlives the
  reason for it and nothing ever says so.

It does **not** own:

* **Scalar config.** A boolean marker key is read by whoever cares;
  generalizing this module to "any value of any type" would trade a precise
  contract for a dict of ``object`` the caller must re-validate anyway.
* **The consumer's resolved shape.** A gate's own ``Config`` dataclass names ITS
  keys (``allow`` / ``exempt`` / ``paths``) and stays with that gate. This module
  hands back a plain mapping and takes no view on what the keys mean.

An honestly narrow library beats one that claims to be generic and hardcodes a
table name, so the boundary above is the contract, not an apology for it.

Entry point assumptions
-----------------------
Both filesystem touches are **injected** (``is_file`` / ``read_text``), so a
consumer's tests can drive every branch -- including the unreadable-file one --
without a real tree, and this module performs no I/O of its own. It imports
``tomllib``, so it requires Python 3.11+. It has no other dependency, holds no
state, and cannot fail at import time.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Callable, Container, Iterable, Sequence
from pathlib import Path

# The config file this library walks up to find. One name, one place.
_PYPROJECT_NAME = "pyproject.toml"
# The top-level table every per-tool table nests under: [tool.<name>].
_TOOL_KEY = "tool"

# Filesystem seams. Injected rather than called directly so a consumer's tests can
# reach the malformed/unreadable branches without writing a real file: inject the
# seam, never let a black-box test touch the host.
IsFileFn = Callable[[Path], bool]  # is this path an existing regular file?
ReadTextFn = Callable[[Path], str]  # read a file's text, or raise
# Does a configured path still resolve? The CALLER decides what that means -- is_dir
# for a list of folders, exists for a mixed list -- because only it knows what kind
# of thing its own config names.
ExistsFn = Callable[[Path], bool]


def default_is_file(path: Path) -> bool:
    """The real :data:`IsFileFn` -- true when *path* is an existing regular file."""
    return path.is_file()


def default_is_dir(path: Path) -> bool:
    """The real :data:`ExistsFn` for folder config -- true when *path* is a directory."""
    return path.is_dir()


def default_read_text(path: Path) -> str:
    """The real :data:`ReadTextFn`: read *path* as UTF-8.

    The encoding is explicit and lives here once. Falling back to the platform
    default would make a pyproject holding a non-ASCII string readable on one
    machine and a ``UnicodeDecodeError`` on another -- and since that error is
    caught and re-raised as :class:`ConfigError`, the difference would surface as
    a gate that mysteriously exits 2 on one developer's box.
    """
    return path.read_text(encoding="utf-8")


class ConfigError(Exception):
    """Exit-2-class failure: a tool's pyproject config could not be trusted.

    Raised for a pyproject that cannot be read or parsed, and for a key whose
    value is not a list of strings. Never raised for *absent* config, which is a
    legitimate empty result -- the distinction between "no config" and "config I
    cannot interpret" is the whole point of the type.
    """


def _require_string_list(
    raw: object, *, key: str, pyproject: Path, table: str
) -> list[str]:
    """Return *raw* as a list of strings, or raise :class:`ConfigError`.

    A present-but-wrong-shaped value is config we cannot interpret; treating it as
    empty would be a false clean, so it stops the gate instead. The message names
    the table, the key and the file, so the reader knows exactly what to edit;
    *pyproject* is the file as a diagnostic shows it, relative to the target.
    """
    if not isinstance(raw, list) or any(not isinstance(entry, str) for entry in raw):
        raise ConfigError(
            f"[tool.{table}] {key} in {pyproject} must be a list of strings"
        )
    return raw


def _tool_table(data: object, table: str) -> dict[str, object]:
    """The ``[tool.<table>]`` sub-table of parsed *data*, or ``{}`` when absent.

    Every wrong shape on the way down (non-dict document, non-dict ``tool``,
    non-dict table) degrades to an empty table rather than raising: a pyproject
    that simply does not configure this tool is the common case, not a defect.
    A malformed *value* inside a table that does exist is a different matter and
    is caught by :func:`_require_string_list`.
    """
    tool = data.get(_TOOL_KEY) if isinstance(data, dict) else None
    found = tool.get(table) if isinstance(tool, dict) else None
    return found if isinstance(found, dict) else {}


def _resolved_list(
    values: dict[str, object], key: str, *, base: Path, pyproject: Path, table: str
) -> list[Path]:
    """Resolve ``values[key]`` (a list of path strings) against *base*; ``[]`` when absent.

    *base* is the directory holding *pyproject*, so an entry means the same thing
    no matter which directory the tool was launched from.
    """
    raw = values.get(key)
    if raw is None:
        return []
    entries = _require_string_list(raw, key=key, pyproject=pyproject, table=table)
    return [(base / entry).resolve() for entry in entries]


def _shown(pyproject: Path, start: Path, *, is_file: IsFileFn) -> Path:
    """*pyproject* as a diagnostic names it: relative to the directory the walk began in.

    Every consumer prints a :class:`ConfigError` on stderr, so an absolute path there
    would carry the home directory into each gate's output. The walk only ever finds a
    file at or above that directory, so the relative form is always a run of ``..``
    segments and the file name -- still the one file to edit, read against the very
    target the caller passed. A FILE target's walk effectively begins in its parent, so
    measuring from the file itself would name a sibling pyproject ``../pyproject.toml``,
    pointing one directory too high.
    """
    here = start.resolve()
    origin = here.parent if is_file(here) else here
    return Path(os.path.relpath(pyproject, origin))


def _parse(pyproject: Path, read_text: ReadTextFn, *, shown: Path) -> object:
    """Parse *pyproject*, converting every read/decode failure into a ConfigError.

    ``UnicodeDecodeError`` is caught alongside the TOML and OS errors because a
    non-UTF-8 pyproject is unreadable config, not absent config, and must land on
    the same stop-the-gate path rather than escaping as a bare exception. The
    message names the file as *shown* (see :func:`_shown`), never its absolute path.
    """
    try:
        return tomllib.loads(read_text(pyproject))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError) as exc:
        raise ConfigError(f"cannot read or parse {shown}: {_reason(exc)}") from exc


def _reason(exc: Exception) -> str:
    """*exc* as a diagnostic quotes it, without the absolute path an OSError carries.

    ``str(OSError)`` appends the error's ``filename`` -- the absolute pyproject path --
    so an OSError contributes only its ``strerror``, or its type name when it has none.
    """
    if isinstance(exc, OSError):
        return exc.strerror or type(exc).__name__
    return str(exc)


def nearest(start: Path, *, is_file: IsFileFn) -> Path | None:
    """The nearest ``pyproject.toml`` at or above *start*, or ``None`` when there is none.

    The walk with the parse taken off it, for a consumer that needs to know WHICH file
    would answer before -- or without -- reading it. :func:`chain` needs exactly that:
    telling one source from another by identity is what keeps a declaration from being
    read, and judged stale, once per subject that happens to sit under it.
    """
    here = start.resolve()
    for candidate in (here, *here.parents):
        pyproject = candidate / _PYPROJECT_NAME
        if is_file(pyproject):
            return pyproject
    return None


def chain(start: Path, *, stop: Path, is_file: IsFileFn) -> tuple[Path, ...]:
    """Every ``pyproject.toml`` from *start* up to and including *stop*, nearest first.

    For config that ACCUMULATES down a tree rather than being answered by one file --
    a nested project's declarations adding to what the checkout root declares instead
    of replacing them. Nearest first, because a nearer file is the more specific
    statement and a consumer merging these will want it to win.

    Bounded by *stop* deliberately. An unbounded walk would let whatever happens to sit
    above the checkout change a verdict, which is the host-independence rule governing
    every other path this library resolves. A *start* outside *stop* yields ``()``: what
    to do about a subject reached from outside the bound is the caller's policy, not a
    default this library should pick on its behalf.
    """
    here = start.resolve()
    ceiling = stop.resolve()
    if here != ceiling and ceiling not in here.parents:
        return ()
    # Cut the walk to the bound BEFORE testing anything, rather than breaking out of it
    # partway: the guard above already proves the ceiling is on this list, so a loop that
    # could also end by running out would carry a branch nothing can reach.
    upward = [here, *here.parents]
    bounded = upward[: upward.index(ceiling) + 1]
    return tuple(
        candidate / _PYPROJECT_NAME
        for candidate in bounded
        if is_file(candidate / _PYPROJECT_NAME)
    )


def read_tool_table(
    start: Path, *, table: str, is_file: IsFileFn, read_text: ReadTextFn
) -> tuple[Path, dict[str, object]] | None:
    """The nearest pyproject above *start* and its raw ``[tool.<table>]`` table.

    The walk paired with the parse, for consumers whose keys are NOT path lists.
    :func:`read_path_lists` answers "what paths does my table declare?"; a table
    holding sub-tables (a scope-keyed mapping) cannot be asked that and still needs
    this identical policy -- nearest pyproject above the TARGET rather than the cwd,
    absence is empty, malformed STOPS the gate. Handing the walk over is what keeps
    that decision in one place instead of being re-made, and re-broken, per tool.

    Returns ``None`` when the walk finds no ``pyproject.toml`` at all, and
    ``(pyproject, {})`` when it finds one that declares no such table. Those two are
    deliberately distinct: the caller needs the FILE in the second case, both as the
    directory its entries resolve against and to name in a diagnostic.

    Values come back exactly as ``tomllib`` parsed them. Validating their shape is
    the caller's job, because only it knows what shape its own keys should have --
    which is the same boundary :func:`read_path_lists` draws by owning path lists
    and nothing else. A malformed or unreadable file raises :class:`ConfigError`.
    """
    pyproject = nearest(start, is_file=is_file)
    if pyproject is None:
        return None
    shown = _shown(pyproject, start, is_file=is_file)
    data = _parse(pyproject, read_text, shown=shown)
    return pyproject, _tool_table(data, table)


def read_path_lists(
    start: Path,
    *,
    table: str,
    keys: Sequence[str],
    is_file: IsFileFn,
    read_text: ReadTextFn,
) -> dict[str, list[Path]]:
    """Each of *keys* from ``[tool.<table>]`` in the nearest pyproject above *start*.

    Walks *start* and its ancestors (bounded by the finite parent chain) for a
    ``pyproject.toml``; the FIRST one found supplies every key, in one walk and one
    parse. Each value is an optional list of path strings, resolved relative to
    that file's own directory.

    Every requested key is present in the result, mapped to ``[]`` when the
    pyproject, the table, or the key is absent -- so a caller can index the mapping
    directly instead of guarding each lookup. A malformed/unreadable file, or a
    value that is not a list of strings, raises :class:`ConfigError`.

    Built on :func:`read_tool_table` rather than repeating its walk, so the two
    entry points cannot drift over where config comes from -- the property that
    would otherwise fail silently, since a walk that stopped somewhere else would
    still return a perfectly well-formed answer.
    """
    found = read_tool_table(start, table=table, is_file=is_file, read_text=read_text)
    if found is None:
        return {key: [] for key in keys}
    pyproject, values = found
    # The file as a diagnostic names it: the key validator's message must not carry
    # the absolute path either (see _shown).
    shown = _shown(pyproject, start, is_file=is_file)
    return {
        key: _resolved_list(
            values, key, base=pyproject.parent, pyproject=shown, table=table
        )
        for key in keys
    }


# ---------------------------------------------------------------------------
# What a resolved entry means: a subtree, and one that can rot.
# ---------------------------------------------------------------------------
def is_under(path: Path, roots: Sequence[Path]) -> bool:
    """True when *path* resolves to one of *roots* or sits beneath one.

    The subtree rule behind every exclude/prune list: naming a directory excludes
    everything below it, not just the directory itself. *roots* are expected to be
    already resolved (as :func:`read_path_lists` returns them); *path* is resolved
    here so a caller can pass the un-normalized path it is holding mid-walk.
    """
    resolved = path.resolve()
    return any(resolved == root or root in resolved.parents for root in roots)


def dead_paths(
    configured: Iterable[Path], prospective: Container[Path], *, exists: ExistsFn
) -> tuple[list[Path], list[Path]]:
    """Split *configured* entries that no longer resolve into ``(gating, absent)``.

    A configured path that has gone away is stale config and belongs in the first
    (gating) list, so an allow/exempt/exclude list cannot rot silently -- the
    exclusion would otherwise outlive whatever justified it, with nothing to say so.

    An entry also listed in *prospective* goes to the second (non-gating) list
    instead: that is the curated hatch for a path that is legitimately absent right
    now -- gitignored, created on demand, or present in the primary checkout and not
    in a linked worktree. It is still REPORTED, because an unexplained silence is
    how the first list stops being trusted. Both lists are sorted for a stable
    report.
    """
    dead = sorted(path for path in configured if not exists(path))
    gating = [path for path in dead if path not in prospective]
    absent = [path for path in dead if path in prospective]
    return gating, absent
