# suite_select.py

Shared library (not a CLI) owning the names and helpers that let a test runner run a grouped
suite's files as separate pool tasks, while the folder's coverage is still combined as one.

## Why it exists

A grouped-suite aggregator, a `test_<folder>.py` file beside a `tests/` folder, folds every
`tests/test_*.py` beside it into one run. Run bare or under one `coverage run`, that is one
serial process, so when the grouped files mostly wait (engine launches, settle windows, refusal
timeouts) the folder's test time is the sum of every file's waiting.

The split runs each grouped file as its own task instead. Two parties must agree for that to
work: the splitting runner, which reads whether an aggregator opted in and sets each task's
selection, and the aggregator's `load_tests`, which honors it. This module alone defines the
names they share, so neither side spells them and a rename cannot leave the two disagreeing.
No such runner ships in this repository: a bare run of an aggregator loads every
grouped file, as it would with no split at all.

## The contract

- **Opt-in is a marker in the aggregator.** A module-level `SPLIT_SUITE_BY_FILE = True`, read by
  AST without importing the file, so the opt-in travels with the folder through a move or rename.
  Without it the folder runs unsplit.
- **Selection is one environment variable per task.** A grouped filename loads only that file's
  tests; the guard value loads only the aggregator's own tests. Unset (a bare run, or any caller
  but the split) loads everything, as an unsplit run does.
- **The aggregator delegates its whole `load_tests` body** to `fold_selected`, so no aggregator
  reads the variable itself.
- **Grouped files sit at the top of the suite dir.** Discovery recurses into a subdirectory
  holding an `__init__.py`, but the task list names only top-level files, so a test file in such
  a package would run in no task. `package_subdirs` lists them, and a splitting caller refuses
  when it is non-empty rather than drop those tests. A plain data directory (no `__init__.py`)
  is not entered by discovery and is not listed.
- **Every failure is loud.** A malformed selection, or a filename with no file behind it, raises
  `ValueError` rather than loading nothing: a task that runs zero tests would otherwise pass.

## Public surface

| Name | What it is |
| --- | --- |
| `SPLIT_MARKER` | `"SPLIT_SUITE_BY_FILE"`: the constant an aggregator sets to `True` to opt in. |
| `SELECT_ENV` | `"PYTHON_GATE_SUITE_SELECT"`: the per-task selection variable. |
| `GUARDS_ONLY` | `":guards:"`: the selection that loads only the aggregator's own tests. |
| `declares_split(aggregator_source) -> bool` | True only for a module-level marker, a plain or annotated assignment to that single name, whose value is the literal `True`. |
| `task_selections(suite_filenames) -> list[str]` | `GUARDS_ONLY`, then every bare `test_*.py` name in the listing, sorted and deduplicated. |
| `package_subdirs(suite_dir) -> list[str]` | The immediate subdirectories of `suite_dir` holding an `__init__.py` file, sorted: the packages discovery would recurse into. |
| `selection(environ) -> str \| None` | This task's selection: `None` when unset, else the guard value or a bare `test_*.py` name; anything else raises `ValueError`. |
| `fold_selected(standard_tests, loader, suite_dir, environ)` | What `load_tests` returns under the selection (below). |

### What `declares_split` refuses

It returns False, so the caller takes the unsplit path, for: no marker; `False`; a truthy value
that is not `True` (`1`, `"yes"`); a computed value (`bool(1)`); a bare annotation; a marker
inside a function, class or `if`; a tuple or chained assignment; an attribute of the same name;
and source that does not parse. The unsplit path is always safe, so every doubt resolves to it.

### What `fold_selected` loads

| Selection | Returns |
| --- | --- |
| unset | `standard_tests` with the whole suite dir discovered into it: the unsplit behavior. |
| `GUARDS_ONLY` | `standard_tests` alone. The suite dir is not discovered at all. |
| a filename | A new suite holding only that file's tests. The guards are left out: the guard task runs them. A name that is not exactly, case included, the name of a file in the suite dir raises `ValueError`. |

Discovery uses the suite dir as its own `top_level_dir`, so grouped modules load as top-level
modules and need no `__init__.py`.

Membership is checked against the exact names in the suite dir's listing, never by testing
whether the path exists: on a case-insensitive filesystem `test_PA.py` "exists" when only
`test_pa.py` does, and discovery's case-sensitive pattern would then load nothing.

`task_selections` drops helpers (`_live_fixtures.py`), non-Python files, anything with a path
separator, and anything holding a glob metacharacter (`*`, `?`, `[`), which the discovery
pattern would read as a glob and so match other files, so every value it returns is one `selection` accepts unchanged.

## Who consumes it

- **A splitting runner** reads `declares_split` on the aggregator, builds its task list from
  `task_selections` over the suite dir's listing, and sets `SELECT_ENV` for each task.
- **A grouped-suite aggregator's `load_tests`** returns `fold_selected`.

An aggregator reaches it by putting this folder, beside its own tool's, on `sys.path`, and the
root `pyproject.toml` names it in `mypy_path`:

```python
import os
import unittest

import suite_select  # the sys.path bootstrap that makes this resolve is omitted here

SPLIT_SUITE_BY_FILE = True


def load_tests(
    loader: unittest.TestLoader, standard_tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    """unittest's own hook: fold in the grouped suites this task selected."""
    return suite_select.fold_selected(standard_tests, loader, _SUITE_DIR, os.environ)
```

## Tests

`test_suite_select.py` pins the three names; `package_subdirs` (packages listed, plain and
nested dirs not); every accept and refuse case of `declares_split` and `selection`; and the task
list's order and filtering. It runs `fold_selected` against a temporary three-file suite dir,
asserting the exact test ids each selection loads, that a file sharing the selected file's stem
as a prefix stays out, and that a case-only variant of a file's name raises. `selection` is a
`# fuzz-target`: its property tests assert that any string either comes back unchanged or
raises `ValueError`, and that an accepted value is never a path or a glob.
