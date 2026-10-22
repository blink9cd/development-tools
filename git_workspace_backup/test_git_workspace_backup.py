#!/usr/bin/env python3
"""Aggregate git_workspace_backup's grouped suites -- bare-runnable (python this file).

The suites live in ``tests/``, one per concern. A runner that pairs a suite with its subject
by name finds this file beside ``git_workspace_backup.py``, and the suites are loaded from
here, so they are measured under this one coverage run.

An empty ``unittest.TestLoader.discover`` is a PASSING run -- ``Ran 0 tests``, exit 0 -- so
each grouped file is floored separately: a renamed suite dir, a pattern that stops matching,
or an import that quietly fails would otherwise gate nothing while reporting green.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

#: The grouped-suite dir's name, spelled here: the one library that owns it elsewhere
#: does not ship with this project, and vendoring it for one constant would not pay.
_SUITE_DIR_NAME = "tests"
_HERE = Path(__file__).resolve().parent
_SUITE_DIR = _HERE / _SUITE_DIR_NAME
# The suites import `git_workspace_backup` and `_fixtures` by plain name, so both dirs
# must be importable however this file was launched. _HERE is already sys.path[0] when
# run as a script, but not when something else imports this module.
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_SUITE_DIR))

# What each grouped file must yield, floored SEPARATELY rather than as one combined total.
# The guard exists because an empty or half-failed discovery is otherwise a PASSING run:
# "no tests ran" and "every test passed" are the same exit code, and nothing else in the
# gate distinguishes them. One combined floor decays by growth, though -- once the suite
# exceeds it by more than the smallest file holds, that file can be lost whole while the
# total still clears the floor, and the guard goes on passing while gating nothing. A
# per-file floor closes that by construction, and the key-set comparison below means a file
# added later arrives unfloored LOUDLY instead of being absorbed into a combined number.
#
# Restated, not derived: a floor computed from discovery would compare discovery against
# itself and pass for an empty suite, which is the defect this guard exists to catch.
_MIN_TESTS_BY_FILE: dict[str, int] = {
    "test_build_verify.py": 21,
    "test_bundle.py": 20,
    "test_cli.py": 14,
    "test_cli_agent.py": 9,
    "test_cli_options.py": 5,
    "test_cli_repo.py": 10,
    "test_cli_status.py": 16,
    "test_cli_uninstall.py": 9,
    "test_companion_doc.py": 2,
    "test_config_roots.py": 18,
    "test_drift.py": 13,
    "test_icloud.py": 14,
    "test_launchd.py": 18,
    "test_machine.py": 16,
    "test_notify.py": 5,
    "test_proc_fs.py": 26,
    "test_promote.py": 24,
    "test_properties.py": 13,
    "test_properties_bundle.py": 5,
    "test_properties_icloud.py": 2,
    "test_properties_machine.py": 2,
    "test_properties_manifest_fields.py": 5,
    "test_properties_notify.py": 1,
    "test_properties_patterns.py": 1,
    "test_properties_promote.py": 4,
    "test_properties_restore_doc.py": 4,
    "test_properties_verify.py": 2,
    "test_prune.py": 18,
    "test_records.py": 21,
    "test_regular_read.py": 2,
    "test_restore_containment.py": 8,
    "test_restore_doc.py": 19,
    "test_restore_doc_values.py": 5,
    "test_restore_head.py": 3,
    "test_restore_second_machine.py": 28,
    "test_restore_stash.py": 4,
    "test_roots_layouts.py": 4,
    "test_run.py": 14,
    "test_run_guards.py": 22,
    "test_run_machine.py": 11,
    "test_samples.py": 3,
    "test_state_base.py": 15,
    "test_status.py": 17,
    "test_status_gather.py": 16,
    "test_verify_guards.py": 17,
    "test_zip.py": 22,
}
# This module's OWN tests: the guards below, which are the only reason the file exists.
# Kept apart from the grouped floors rather than summed into one total. A combined number
# carries the grouped suites' growth as slack, and these guards are small enough to vanish
# inside it -- two added grouped tests would buy the silent deletion of both. Counted
# apart, no amount of grouped growth pays for a deleted guard.
#
# Restated, not counted off the class below: a count taken from that class falls by one
# when a guard is deleted, so it would pass for exactly the deletion it exists to catch.
# Exact rather than a floor, for the same reason the total was dropped -- under ``>=`` an
# added guard becomes room a later deletion can hide in.
_OWN_GUARD_TESTS = 2


def _discovered() -> unittest.TestSuite:
    """Every test case under the grouped-suite dir, loaded as top-level modules."""
    loader = unittest.TestLoader()
    return loader.discover(str(_SUITE_DIR), top_level_dir=str(_SUITE_DIR))


def _tests_by_module(suite: unittest.TestSuite) -> dict[str, int]:
    """Flatten a (possibly nested) suite into a per-file test count.

    ``discover`` returns a suite of suites -- one layer per directory or module it walked
    -- so this recurses rather than assuming a fixed depth, and keeps working if that
    internal shape ever changes. Each leaf's ``__module__`` becomes a ``<name>.py`` key,
    matching ``_MIN_TESTS_BY_FILE``.
    """
    counts: dict[str, int] = {}
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            for module, count in _tests_by_module(item).items():
                counts[module] = counts.get(module, 0) + count
        else:
            filename = f"{type(item).__module__}.py"
            counts[filename] = counts.get(filename, 0) + 1
    return counts


class AggregatorReachesTheGroupedSuitesTest(unittest.TestCase):
    """Nothing but this file runs the suites below, so prove it actually found them."""

    def test_module_load_includes_the_grouped_suites(self) -> None:
        """Guards the ``load_tests`` WIRING, which a discovery-based check cannot see.

        A guard calling ``_discovered()`` directly shares its subject's failure mode:
        deleting or renaming ``load_tests`` stops every grouped suite running in the real
        invocation, while such a guard re-derives the same count fresh off disk and stays
        green. ``loadTestsFromModule`` is what a bare run and a module-loading runner use,
        and it honors the ``load_tests`` protocol, so this depends on that wiring.

        The two populations are counted APART, never summed: a single total carries the
        grouped suites' growth as slack, and a deleted guard here disappears into it.
        ``__name__`` is what separates them, and it holds under both invocations --
        ``__main__`` when run bare, the module name under the gate -- because a class
        takes its ``__module__`` from the module that defines it.
        """
        loaded = _tests_by_module(
            unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
        )
        own_found = loaded.pop(f"{__name__}.py", 0)
        self.assertEqual(
            own_found,
            _OWN_GUARD_TESTS,
            f"this module contributed {own_found} of its own tests, expected exactly "
            f"{_OWN_GUARD_TESTS} -- a guard above may have been deleted, or a new one "
            f"added without raising _OWN_GUARD_TESTS",
        )
        # Derived from the per-file floors rather than restated as a second total: that
        # dict is a written-down literal, not a count taken from discovery, so summing it
        # still measures the suite against a number from outside it.
        grouped_found = sum(loaded.values())
        grouped_floor = sum(_MIN_TESTS_BY_FILE.values())
        self.assertGreaterEqual(
            grouped_found,
            grouped_floor,
            f"loading this module yielded {grouped_found} grouped tests, expected at "
            f"least {grouped_floor} -- load_tests may have been removed or renamed, "
            f"silently dropping a tests/ suite from the real run",
        )

    def test_grouped_suite_meets_its_own_per_file_floor(self) -> None:
        """Fail when ONE grouped file has lost tests, and when a new file arrives with no
        floor entry at all."""
        found_by_file = _tests_by_module(_discovered())
        # Compared as full key SETS, both ways: a new file with no entry here would
        # otherwise be silently unfloored, its whole-file loss caught only by the combined
        # total, which a gain elsewhere can mask. An unmapped new file -- or a mapped file
        # that no longer exists -- fails here, before any count is compared.
        self.assertEqual(
            found_by_file.keys(),
            _MIN_TESTS_BY_FILE.keys(),
            f"tests/ discovery and _MIN_TESTS_BY_FILE disagree on which files "
            f"exist: found {sorted(found_by_file)}, mapped {sorted(_MIN_TESTS_BY_FILE)}",
        )
        for filename, floor in _MIN_TESTS_BY_FILE.items():
            found = found_by_file.get(filename, 0)
            self.assertGreaterEqual(
                found,
                floor,
                f"tests/{filename} yielded {found} tests, expected at least "
                f"{floor} -- a test may have been removed from it",
            )


def load_tests(
    loader: unittest.TestLoader,
    standard_tests: unittest.TestSuite,
    pattern: str | None,
) -> unittest.TestSuite:
    """unittest protocol hook: run the grouped suites alongside the check above."""
    standard_tests.addTests(_discovered())
    return standard_tests


if __name__ == "__main__":
    unittest.main()
