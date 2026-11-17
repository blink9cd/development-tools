"""Own the names and helpers that let a runner run one grouped suite's files as separate tasks.

Why this exists
---------------
A grouped-suite aggregator (a ``test_<folder>.py`` file beside a ``tests/`` folder) runs every
``tests/test_*.py`` beside it in ONE process. When those files mostly wait (engine launches,
settle windows), that one serial run is the folder's whole test time. A splitting runner can
instead run each grouped file as its own pool task, under one shared coverage data dir, and
combine.

Two parties must agree on three names for that to work: the runner (which reads the opt-in
marker and sets the per-task selection) and the aggregator's ``load_tests`` (which honors it).
This module alone defines those names, so neither side spells them.

The contract
------------
- An aggregator opts in with a module-level ``SPLIT_SUITE_BY_FILE = True``, read here by AST
  without importing the file. Absent, the folder runs unsplit.
- The runner sets ``PYTHON_GATE_SUITE_SELECT`` per task, to a grouped filename (that file's
  tests only) or to ``:guards:`` (the aggregator's own tests only). Unset means everything.
- The aggregator's ``load_tests`` returns :func:`fold_selected`, and nothing more.
- Grouped files sit at the top of the suite dir: :func:`package_subdirs` lists the packages
  discovery would recurse into, which no task selects, so a splitting caller refuses them.

Entry point assumptions
-----------------------
Import-only: no CLI and no output. Standard library only. Paths and the environment come from
the caller, so the module reads no global state.
"""

from __future__ import annotations

import ast
import fnmatch
import unittest
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Final

#: The module-level constant an aggregator sets to True to opt in to the per-file split.
SPLIT_MARKER: Final = "SPLIT_SUITE_BY_FILE"

#: The environment variable a splitting runner sets for each task; unset means "load everything".
SELECT_ENV: Final = "PYTHON_GATE_SUITE_SELECT"

#: The selection value that loads only the aggregator's own (guard) tests.
GUARDS_ONLY: Final = ":guards:"

#: The grouped files a task may select: the same names unittest's default discovery loads.
_SUITE_FILE_GLOB: Final = "test_*.py"

#: Path separators of either platform: a selection is a bare name inside the suite dir.
_SEPARATORS: Final = ("/", "\\")

#: Characters unittest's discovery pattern reads as a glob. A name holding one would select
#: more (or other) files than itself, so it is never a selection.
_GLOB_METACHARACTERS: Final = ("*", "?", "[")

#: The file whose presence makes a subdirectory a package that discovery recurses into.
_PACKAGE_INIT: Final = "__init__.py"


def _is_marker_target(target: ast.expr) -> bool:
    """True when an assignment target is the bare marker name."""
    return isinstance(target, ast.Name) and target.id == SPLIT_MARKER


def _is_true_constant(value: ast.expr | None) -> bool:
    """True only for the literal ``True``: 1, "yes" and computed values do not count."""
    return isinstance(value, ast.Constant) and value.value is True


def _statement_declares(statement: ast.stmt) -> bool:
    """True when one module-level statement is the marker set to True."""
    if isinstance(statement, ast.Assign):
        # Exactly one target: a chained ``A = B = True`` or a tuple unpack is not the plain
        # declaration the contract names, so it does not opt in.
        return (
            len(statement.targets) == 1
            and _is_marker_target(statement.targets[0])
            and _is_true_constant(statement.value)
        )
    if isinstance(statement, ast.AnnAssign):
        return _is_marker_target(statement.target) and _is_true_constant(
            statement.value
        )
    return False


def declares_split(aggregator_source: str) -> bool:
    """Report whether an aggregator's source opts in, by a module-level marker set to True.

    Read by AST, never by import, so a gate can classify a folder without running its code.
    Only top-level statements are read: a marker inside a function, class or ``if`` is not a
    static declaration. Source that does not parse returns False, and the caller then runs the
    unsplit path, which is always safe.
    """
    try:
        tree = ast.parse(aggregator_source)
    except (SyntaxError, ValueError):
        # ValueError: a NUL byte in the source, which ast.parse rejects before tokenizing on
        # some interpreters rather than as a SyntaxError.
        return False
    return any(_statement_declares(statement) for statement in tree.body)


def _is_suite_filename(name: str) -> bool:
    """True for a bare ``test_*.py`` name: no directory part and no glob metacharacter."""
    forbidden = (*_SEPARATORS, *_GLOB_METACHARACTERS)
    return not any(char in name for char in forbidden) and fnmatch.fnmatchcase(
        name, _SUITE_FILE_GLOB
    )


def task_selections(suite_filenames: Iterable[str]) -> list[str]:
    """List the selections one split run needs: the guards, then each grouped file once.

    ``suite_filenames`` is the suite dir's listing; helpers (``_live_fixtures.py``), non-Python
    files and anything else that is not a bare ``test_*.py`` name are dropped. Every value
    returned is one :func:`selection` accepts unchanged.
    """
    files = sorted({name for name in suite_filenames if _is_suite_filename(name)})
    return [GUARDS_ONLY, *files]


# fuzz-target
def selection(environ: Mapping[str, str]) -> str | None:
    """Read this task's selection from ``environ``.

    Returns None when the variable is absent (a bare run: load everything), and the guard value
    or a bare ``test_*.py`` basename as is. Anything else raises ValueError naming the variable
    and the value, because a malformed selection that loaded nothing would be a green run of
    zero tests.
    """
    value = environ.get(SELECT_ENV)
    if value is None or value == GUARDS_ONLY or _is_suite_filename(value):
        return value
    msg = (
        f"{SELECT_ENV}={value!r} is neither {GUARDS_ONLY!r} nor a bare "
        f"{_SUITE_FILE_GLOB!r} filename in the suite directory"
    )
    raise ValueError(msg)


def package_subdirs(suite_dir: Path) -> list[str]:
    """List, sorted, the immediate subdirectories of ``suite_dir`` that are packages.

    Discovery recurses into a subdirectory holding an ``__init__.py``, but
    :func:`task_selections` names only top-level files, so a test file inside one would run in
    no split task. A caller splitting the suite refuses when this is non-empty. A plain data
    directory (no ``__init__.py``) is not listed: discovery does not enter it.
    """
    return sorted(
        entry.name
        for entry in suite_dir.iterdir()
        if entry.is_dir() and (entry / _PACKAGE_INIT).is_file()
    )


def fold_selected(
    standard_tests: unittest.TestSuite,
    loader: unittest.TestLoader,
    suite_dir: Path,
    environ: Mapping[str, str],
) -> unittest.TestSuite:
    """Build what an aggregator's ``load_tests`` returns, honoring this task's selection.

    - No selection: ``standard_tests`` with the whole suite dir discovered into it, exactly the
      unsplit behavior.
    - The guard value: ``standard_tests`` alone; the suite dir is not discovered at all.
    - A filename: a NEW suite holding only that file's tests; the guards are left out, since
      the guard task runs them. A name that is not a file in ``suite_dir`` raises ValueError:
      a typo or a lost file must be loud, never an empty green run.

    ``top_level_dir`` is the suite dir itself, so grouped modules load as top-level modules and
    need no ``__init__.py``.
    """
    chosen = selection(environ)
    if chosen == GUARDS_ONLY:
        return standard_tests
    if chosen is None:
        standard_tests.addTests(
            loader.discover(str(suite_dir), top_level_dir=str(suite_dir))
        )
        return standard_tests
    # Exact names from the listing, never ``(suite_dir / chosen).is_file()``: on a
    # case-insensitive filesystem that finds ``test_PA.py`` for a ``test_pa.py``, and
    # discovery's case-sensitive pattern then loads nothing: a green run of zero tests.
    if chosen not in {entry.name for entry in suite_dir.iterdir() if entry.is_file()}:
        msg = f"{SELECT_ENV}={chosen!r} names no file in {suite_dir}"
        raise ValueError(msg)
    return unittest.TestSuite(
        [loader.discover(str(suite_dir), pattern=chosen, top_level_dir=str(suite_dir))]
    )
