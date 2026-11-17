#!/usr/bin/env python3
"""Tests for suite_select: the opt-in marker read, the task list, the selection, the fold."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

import suite_select  # noqa: E402 - import follows the sys.path bootstrap above

# The determinism convention: this property test also runs in every coverage run, so a
# randomized profile would make that run flaky. Only a fuzzing run applying its own settings
# overrides this with randomization and a time budget.
# deadline=None: a per-example wall-clock deadline measures machine load, not
# correctness. too_slow is suppressed as well: on a cold .hypothesis cache the first
# draw also pays Hypothesis's one-time setup, which under load outlasts the limit.
settings.register_profile(
    "deterministic",
    derandomize=True,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("deterministic")

#: Fixture module names, unique to this suite so a discovery here can never collide in
#: ``sys.modules`` with a real module of the same name.
_ALPHA = "test_suite_select_fixture_alpha"
_BETA = "test_suite_select_fixture_beta"
#: Shares alpha's stem as a prefix, so selecting alpha proves an exact-name match, not a prefix.
_ALPHA_MORE = f"{_ALPHA}_more"

#: Three tiny grouped files: alpha holds two tests, beta and alpha_more one each, so every
#: selection's test-id set is distinct from every other's.
_FIXTURES = {
    f"{_ALPHA}.py": (
        "import unittest\n\n\n"
        "class Alpha(unittest.TestCase):\n"
        "    def test_one(self) -> None:\n"
        "        pass\n\n"
        "    def test_two(self) -> None:\n"
        "        pass\n"
    ),
    f"{_BETA}.py": (
        "import unittest\n\n\n"
        "class Beta(unittest.TestCase):\n"
        "    def test_only(self) -> None:\n"
        "        pass\n"
    ),
    f"{_ALPHA_MORE}.py": (
        "import unittest\n\n\n"
        "class AlphaMore(unittest.TestCase):\n"
        "    def test_more(self) -> None:\n"
        "        pass\n"
    ),
    # A helper in the suite dir: never a selection, never a task.
    "_helpers.py": "VALUE = 1\n",
}


class TestNames(unittest.TestCase):
    """The three names other tools code against are the contract's spelling."""

    def test_names_are_the_contract(self) -> None:
        """A rename here breaks a splitting runner and every aggregator at once."""
        self.assertEqual(suite_select.SPLIT_MARKER, "SPLIT_SUITE_BY_FILE")
        self.assertEqual(suite_select.SELECT_ENV, "PYTHON_GATE_SUITE_SELECT")
        self.assertEqual(suite_select.GUARDS_ONLY, ":guards:")


class TestDeclaresSplit(unittest.TestCase):
    """Only a module-level ``SPLIT_SUITE_BY_FILE = True`` opts an aggregator in."""

    def test_plain_assign_true(self) -> None:
        """The canonical spelling opts in."""
        self.assertTrue(suite_select.declares_split("SPLIT_SUITE_BY_FILE = True\n"))

    def test_annotated_assign_true(self) -> None:
        """An annotated assignment with a True value opts in too."""
        source = "from typing import Final\nSPLIT_SUITE_BY_FILE: Final = True\n"
        self.assertTrue(suite_select.declares_split(source))

    def test_marker_among_other_statements(self) -> None:
        """The marker is found wherever it sits at module level."""
        source = '"""Doc."""\nimport sys\nX = 1\nSPLIT_SUITE_BY_FILE = True\ndef f():\n    pass\n'
        self.assertTrue(suite_select.declares_split(source))

    def test_absent(self) -> None:
        """No marker: the unsplit path."""
        self.assertFalse(suite_select.declares_split("X = True\n"))

    def test_false_value(self) -> None:
        """An explicit False opts out."""
        self.assertFalse(suite_select.declares_split("SPLIT_SUITE_BY_FILE = False\n"))

    def test_truthy_but_not_true(self) -> None:
        """Only the constant True counts: 1 and "yes" are not True."""
        self.assertFalse(suite_select.declares_split("SPLIT_SUITE_BY_FILE = 1\n"))
        self.assertFalse(suite_select.declares_split("SPLIT_SUITE_BY_FILE = 'yes'\n"))

    def test_non_constant_value(self) -> None:
        """A computed value cannot be read statically, so it does not opt in."""
        self.assertFalse(suite_select.declares_split("SPLIT_SUITE_BY_FILE = bool(1)\n"))

    def test_annotation_without_value(self) -> None:
        """A bare annotation declares nothing."""
        self.assertFalse(suite_select.declares_split("SPLIT_SUITE_BY_FILE: bool\n"))

    def test_nested_in_function(self) -> None:
        """A marker inside a function is not module level."""
        source = "def f():\n    SPLIT_SUITE_BY_FILE = True\n"
        self.assertFalse(suite_select.declares_split(source))

    def test_nested_in_class(self) -> None:
        """A class attribute is not module level."""
        source = "class C:\n    SPLIT_SUITE_BY_FILE = True\n"
        self.assertFalse(suite_select.declares_split(source))

    def test_nested_in_if(self) -> None:
        """A conditional marker is not a static declaration."""
        source = "if True:\n    SPLIT_SUITE_BY_FILE = True\n"
        self.assertFalse(suite_select.declares_split(source))

    def test_tuple_target(self) -> None:
        """An unpacking assignment is not the single-name declaration."""
        source = "SPLIT_SUITE_BY_FILE, OTHER = True, True\n"
        self.assertFalse(suite_select.declares_split(source))

    def test_chained_assignment(self) -> None:
        """Two targets on one statement is not the single-name declaration."""
        source = "SPLIT_SUITE_BY_FILE = OTHER = True\n"
        self.assertFalse(suite_select.declares_split(source))

    def test_attribute_target(self) -> None:
        """An attribute of the same name is not the module constant."""
        self.assertFalse(suite_select.declares_split("x.SPLIT_SUITE_BY_FILE = True\n"))

    def test_annotated_attribute_target(self) -> None:
        """An annotated attribute of the same name is not the module constant either."""
        source = "x.SPLIT_SUITE_BY_FILE: bool = True\n"
        self.assertFalse(suite_select.declares_split(source))

    def test_other_name_annotated(self) -> None:
        """An annotated assignment to another name does not opt in."""
        self.assertFalse(suite_select.declares_split("OTHER: bool = True\n"))

    def test_syntax_error(self) -> None:
        """Unparseable source falls back to the unsplit path, never raises."""
        self.assertFalse(suite_select.declares_split("SPLIT_SUITE_BY_FILE = (\n"))

    def test_null_byte(self) -> None:
        """Source holding a NUL byte is unparseable too, and also falls back."""
        self.assertFalse(suite_select.declares_split("SPLIT_SUITE_BY_FILE = True\0\n"))


class TestTaskSelections(unittest.TestCase):
    """The task list: the guards first, then every grouped file once, sorted."""

    def test_guards_first_then_sorted_unique_files(self) -> None:
        """Order of input never matters; duplicates collapse; helpers drop."""
        names = [
            "test_b.py",
            "_live_fixtures.py",
            "test_a.py",
            "test_b.py",
            "test_runner.mjs",
            "conftest.py",
            "test_c.pyc",
        ]
        self.assertEqual(
            suite_select.task_selections(names),
            [suite_select.GUARDS_ONLY, "test_a.py", "test_b.py"],
        )

    def test_empty_input(self) -> None:
        """No grouped files still yields the guard task."""
        self.assertEqual(suite_select.task_selections([]), [suite_select.GUARDS_ONLY])

    def test_paths_are_not_names(self) -> None:
        """A name with a separator is a path, not a suite-dir listing entry, and drops."""
        self.assertEqual(
            suite_select.task_selections(["sub/test_a.py", "sub\\test_b.py"]),
            [suite_select.GUARDS_ONLY],
        )

    def test_glob_metacharacters_drop(self) -> None:
        """A name a discovery pattern would read as a glob is never a task."""
        names = ["test_*.py", "test_?.py", "test_[a].py", "test_a.py"]
        self.assertEqual(
            suite_select.task_selections(names), [suite_select.GUARDS_ONLY, "test_a.py"]
        )

    def test_accepts_any_iterable(self) -> None:
        """A generator, as from ``Path.iterdir``, works as well as a list."""
        names = (name for name in ("test_z.py", "test_y.py"))
        self.assertEqual(
            suite_select.task_selections(names),
            [suite_select.GUARDS_ONLY, "test_y.py", "test_z.py"],
        )

    def test_every_task_round_trips_through_selection(self) -> None:
        """Each value it hands a splitting runner is one ``selection`` accepts unchanged."""
        for value in suite_select.task_selections(["test_a.py", "x.py", "test_b.py"]):
            env = {suite_select.SELECT_ENV: value}
            self.assertEqual(suite_select.selection(env), value)


class TestSelection(unittest.TestCase):
    """Reading the per-task variable: absent, guards, a filename, or a loud error."""

    def test_absent_is_none(self) -> None:
        """No variable: a bare run, which loads everything."""
        self.assertIsNone(suite_select.selection({}))

    def test_guards_only(self) -> None:
        """The guard value is returned as is."""
        env = {suite_select.SELECT_ENV: suite_select.GUARDS_ONLY}
        self.assertEqual(suite_select.selection(env), suite_select.GUARDS_ONLY)

    def test_filename(self) -> None:
        """A bare grouped filename is returned as is."""
        env = {suite_select.SELECT_ENV: "test_pass_cli.py"}
        self.assertEqual(suite_select.selection(env), "test_pass_cli.py")

    def test_rejects_malformed_values(self) -> None:
        """Every malformed value raises, naming the variable and the value."""
        for value in (
            "",
            "tests/test_a.py",
            "tests\\test_a.py",
            "test_a",
            "test_a.pyc",
            "a_test.py",
            "guards",
            "test_*.py",
            "test_?.py",
            "test_[ab].py",
        ):
            with self.subTest(value=value):
                env = {suite_select.SELECT_ENV: value}
                with self.assertRaises(ValueError) as raised:
                    suite_select.selection(env)
                self.assertIn(suite_select.SELECT_ENV, str(raised.exception))
                self.assertIn(repr(value), str(raised.exception))

    # fuzz-explore: selection
    @given(st.text())
    def test_selection_answers_or_raises_value_error(self, value: str) -> None:
        """Any string either comes back unchanged or raises ValueError, nothing else."""
        env = {suite_select.SELECT_ENV: value}
        try:
            result = suite_select.selection(env)
        except ValueError:
            return
        self.assertEqual(result, value)

    @given(st.text())
    def test_fuzz_selection(self, value: str) -> None:
        """An accepted value is the guard value or a bare ``test_*.py`` name, never a path."""
        env = {suite_select.SELECT_ENV: value}
        try:
            result = suite_select.selection(env)
        except ValueError:
            return
        if result != suite_select.GUARDS_ONLY:
            self.assertIsNotNone(result)
            assert result is not None  # narrows for the checks below
            self.assertTrue(result.startswith("test_"))
            self.assertTrue(result.endswith(".py"))
            self.assertNotIn("/", result)
            self.assertNotIn("\\", result)
            for metacharacter in "*?[":
                self.assertNotIn(metacharacter, result)


def _ids(suite: unittest.TestSuite) -> set[str]:
    """Collect every test id in a (possibly nested) suite into a flat set."""
    found: set[str] = set()
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            found |= _ids(item)
        else:
            found.add(item.id())
    return found


class _StandardGuard(unittest.TestCase):
    """Stands in for an aggregator's own guard tests."""

    def test_guard(self) -> None:
        """Never run: only its id is compared."""


class TestFoldSelected(unittest.TestCase):
    """The whole ``load_tests`` body: which test ids load under each selection."""

    _tmp: tempfile.TemporaryDirectory[str]
    suite_dir: Path

    @classmethod
    def setUpClass(cls) -> None:
        """Write the two-file fixture suite dir once; discovery imports each module once."""
        cls._tmp = tempfile.TemporaryDirectory()
        cls.suite_dir = Path(cls._tmp.name)
        for name, text in _FIXTURES.items():
            (cls.suite_dir / name).write_text(text, encoding="utf-8", newline="\n")

    @classmethod
    def tearDownClass(cls) -> None:
        """Drop the fixture modules and the path discovery inserted, then the dir."""
        for name in (_ALPHA, _BETA, _ALPHA_MORE):
            sys.modules.pop(name, None)
        while str(cls.suite_dir) in sys.path:
            sys.path.remove(str(cls.suite_dir))
        cls._tmp.cleanup()

    def _fold(self, environ: dict[str, str]) -> set[str]:
        """Fold with a fresh one-guard standard suite, returning the loaded ids."""
        standard = unittest.TestSuite([_StandardGuard("test_guard")])
        folded = suite_select.fold_selected(
            standard, unittest.TestLoader(), self.suite_dir, environ
        )
        return _ids(folded)

    def test_unset_loads_guards_and_every_file(self) -> None:
        """No selection: today's behavior, the guards plus the whole suite dir."""
        self.assertEqual(
            self._fold({}),
            {
                _StandardGuard("test_guard").id(),
                f"{_ALPHA}.Alpha.test_one",
                f"{_ALPHA}.Alpha.test_two",
                f"{_BETA}.Beta.test_only",
                f"{_ALPHA_MORE}.AlphaMore.test_more",
            },
        )

    def test_unset_returns_the_standard_suite_itself(self) -> None:
        """The bare path extends the suite it was handed, exactly as the aggregator did."""
        standard = unittest.TestSuite()
        folded = suite_select.fold_selected(
            standard, unittest.TestLoader(), self.suite_dir, {}
        )
        self.assertIs(folded, standard)

    def test_guards_only_loads_only_the_guards(self) -> None:
        """The guard task never discovers the suite dir."""
        env = {suite_select.SELECT_ENV: suite_select.GUARDS_ONLY}
        self.assertEqual(self._fold(env), {_StandardGuard("test_guard").id()})

    def test_filename_loads_only_that_file(self) -> None:
        """A file task loads that file's tests and not the guards."""
        env = {suite_select.SELECT_ENV: f"{_BETA}.py"}
        self.assertEqual(self._fold(env), {f"{_BETA}.Beta.test_only"})

    def test_other_filename_loads_only_that_file(self) -> None:
        """The other file, likewise; and a file sharing its stem as a prefix stays out."""
        env = {suite_select.SELECT_ENV: f"{_ALPHA}.py"}
        self.assertEqual(
            self._fold(env), {f"{_ALPHA}.Alpha.test_one", f"{_ALPHA}.Alpha.test_two"}
        )

    def test_missing_file_is_loud(self) -> None:
        """A typo or a lost file raises rather than running zero tests green."""
        env = {suite_select.SELECT_ENV: "test_no_such_file.py"}
        with self.assertRaises(ValueError) as raised:
            self._fold(env)
        self.assertIn("test_no_such_file.py", str(raised.exception))

    def test_directory_named_like_a_file_is_loud(self) -> None:
        """A directory is not a grouped file."""
        directory = self.suite_dir / "test_suite_select_fixture_dir.py"
        directory.mkdir()
        try:
            env = {suite_select.SELECT_ENV: directory.name}
            with self.assertRaises(ValueError):
                self._fold(env)
        finally:
            os.rmdir(directory)

    def test_case_variant_of_a_file_is_loud(self) -> None:
        """A name matching a file only case-insensitively raises, on any filesystem.

        On a case-insensitive filesystem a path check finds ``test_PA.py`` when only
        ``test_pa.py`` exists, while discovery's case-sensitive pattern then loads nothing.
        """
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "test_pa.py").write_text("", encoding="utf-8", newline="\n")
            env = {suite_select.SELECT_ENV: "test_PA.py"}
            with self.assertRaises(ValueError):
                suite_select.fold_selected(
                    unittest.TestSuite(), unittest.TestLoader(), Path(tmp), env
                )

    def test_malformed_selection_is_loud(self) -> None:
        """The fold reads through ``selection``, so a path value raises too."""
        env = {suite_select.SELECT_ENV: f"../{_ALPHA}.py"}
        with self.assertRaises(ValueError):
            self._fold(env)


class TestPackageSubdirs(unittest.TestCase):
    """Package subdirectories, which discovery recurses into but no task selects."""

    def test_lists_only_immediate_packages_sorted(self) -> None:
        """A subdir with ``__init__.py`` is listed; a plain dir, a file and a nested one are not."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for package in ("zeta", "alpha", "alpha/inner"):
                (root / package).mkdir()
                (root / package / "__init__.py").write_text(
                    "", encoding="utf-8", newline="\n"
                )
            (root / "fixture_extension").mkdir()
            (root / "test_a.py").write_text("", encoding="utf-8", newline="\n")
            (root / "__init__.py").write_text("", encoding="utf-8", newline="\n")
            self.assertEqual(suite_select.package_subdirs(root), ["alpha", "zeta"])

    def test_dir_without_init_is_not_a_package(self) -> None:
        """A data dir such as browser_pass's ``tests/fixture_extension`` is not a package."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "fixture_extension").mkdir()
            (root / "fixture_extension" / "manifest.json").write_text(
                "{}", encoding="utf-8", newline="\n"
            )
            self.assertEqual(suite_select.package_subdirs(root), [])

    def test_init_that_is_a_directory_does_not_count(self) -> None:
        """Only an ``__init__.py`` FILE makes a package."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "odd" / "__init__.py").mkdir(parents=True)
            self.assertEqual(suite_select.package_subdirs(root), [])


if __name__ == "__main__":
    unittest.main()
