#!/usr/bin/env python3
"""Which options each mode consumes: an option a mode would silently ignore is refused.

The expected map is written out here rather than read from the CLI's own table, so a wrong
table fails instead of agreeing with itself. Every refusal happens before the platform check,
which is how an accepted option is told apart cheaply: on a fake Linux host it gets past the
refusal and stops at "macOS only", with nothing run.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest import mock

import _cli_case as cc

import backup_launchd
import git_workspace_backup as wb

#: One non-default value per option dest, as it is spelled on the command line.
SAMPLES: "dict[str, list[str]]" = {
    "if_due": ["--if-due"],
    "time": ["--time", "07:30"],
    "keep": ["--keep", "2"],
    "destination": ["--destination", "/opt/fixture-dest"],
    "dry_run": ["--dry-run"],
    "repo": ["--repo", "/opt/fixture-repo"],
}
#: Each row's command line, and the options it refuses. ``--dry-run`` is not refusable for
#: ``run``: giving it selects the dry-run row instead.
REFUSED: "dict[tuple[str, ...], frozenset[str]]" = {
    ("run",): frozenset({"time"}),
    ("run", "--dry-run"): frozenset({"if_due", "time"}),
    ("status",): frozenset({"if_due", "time", "dry_run"}),
    ("install",): frozenset({"if_due", "keep"}),
    ("uninstall",): frozenset({"if_due", "time", "keep", "destination", "dry_run"}),
}
#: Options whose presence changes the row rather than being accepted within it.
ROW_SELECTORS: "dict[tuple[str, ...], frozenset[str]]" = {
    ("run",): frozenset({"dry_run"})
}


def _parser_dests() -> "set[str]":
    """Every option dest the real parser defines, --mode aside, through its public API."""
    return set(vars(wb.build_parser().parse_args(["--mode", "run"]))) - {"mode"}


class OptionTableTests(unittest.TestCase):
    def test_the_table_covers_every_option_and_every_row_and_nothing_else(self) -> None:
        dests = _parser_dests()
        self.assertEqual(set(SAMPLES), dests, "a new option needs a sample here")
        named = set().union(*wb.CONSUMED_OPTIONS.values())
        self.assertEqual(named, dests)
        rows = {" ".join(row) for row in REFUSED}
        self.assertEqual(set(wb.CONSUMED_OPTIONS), rows)
        self.assertEqual({*wb.MODES, wb.RUN_PLAN_ROW}, rows)


class OptionRefusalTests(cc.CliCase):
    def test_an_option_a_mode_does_not_consume_is_refused(self) -> None:
        for row, refused in REFUSED.items():
            for dest in sorted(refused):
                option = SAMPLES[dest][0]
                with self.subTest(row=row, option=option):
                    code = self.main("--mode", *row, *SAMPLES[dest])
                    self.assertEqual(code, 2)
                    self.assert_one_error_line(option)
                    self.assertIn(f"--mode {' '.join(row)}", self.err.getvalue())
                    self.assertEqual(self.runner.calls, [])
        # Every case above runs on a macOS host, so swapping the refusal and the platform
        # check would pass them all; on Linux the refusal must still come first.
        with self.subTest("refused before the platform check, on a Linux host"):
            linux = replace(self.host(), platform="linux")
            code = self.main("--mode", "uninstall", *SAMPLES["dry_run"], host=linux)
            self.assertEqual(code, 2)
            self.assert_one_error_line("--dry-run")
            self.assertNotIn("macOS only", self.err.getvalue())

    def test_every_refused_option_is_named_on_one_line(self) -> None:
        argv = ["--mode", "install", *SAMPLES["keep"], *SAMPLES["if_due"]]
        self.assertEqual(self.main(*argv), 2)
        self.assert_one_error_line("--keep")
        self.assertIn("--if-due", self.err.getvalue())
        self.assertEqual(self.runner.calls, [])

    def test_an_option_a_mode_consumes_is_accepted(self) -> None:
        cases = [
            (row, SAMPLES[dest])
            for row, refused in REFUSED.items()
            for dest in sorted(
                _parser_dests() - refused - ROW_SELECTORS.get(row, frozenset())
            )
        ]
        # A value equal to the default changes nothing, so it is not refused.
        cases.append((("run",), ["--time", backup_launchd.DEFAULT_TIME]))
        for row, extra in cases:
            with self.subTest(row=row, option=extra):
                linux = replace(self.host(), platform="linux")
                self.assertEqual(self.main("--mode", *row, *extra, host=linux), 2)
                self.assert_one_error_line("macOS only")
                self.assertEqual(self.runner.calls, [])

    def test_the_installed_agents_own_arguments_run(self) -> None:
        # The parser accepting AGENT_ARGS is not enough: a table refusing --if-due for run
        # would make the installed agent exit 2 every morning.
        self.assertEqual(self.main(*backup_launchd.AGENT_ARGS), 0)
        self.assertEqual([p.name for p in self.destination.iterdir()], [cc.FIRST_NAME])


class UnsetFlagTests(cc.CliCase):
    """The two on/off flags default to None, so --help prints no default for them.

    What each mode is handed is still a bool: an unset flag must arrive as False, never as
    the None argparse stores, because the request and install take a typed bool.
    """

    def test_the_flags_default_to_none(self) -> None:
        args = wb.build_parser().parse_args(["--mode", "run"])
        self.assertIsNone(args.if_due)
        self.assertIsNone(args.dry_run)

    def test_a_run_is_handed_false_for_each_unset_flag(self) -> None:
        requests: "list[wb.RunRequest]" = []

        def record(request: wb.RunRequest, deps: object) -> int:
            requests.append(request)
            return 0

        with mock.patch.object(wb, "run_backup", record):
            self.assertEqual(self.main("--mode", "run"), 0)
        self.assertIs(requests[0].if_due, False)
        self.assertIs(requests[0].dry_run, False)

    def test_a_run_without_dry_run_creates_the_state_base(self) -> None:
        # The unset flag is None, so a check written as `args.dry_run is False` would read
        # a real run as a dry one and never create the state base.
        base = self.tree.home / wb.STATE_BASE_NAME
        self.assertFalse(base.exists())
        with mock.patch.object(wb, "run_backup", lambda request, deps: 0):
            self.assertEqual(self.main("--mode", "run"), 0)
        self.assertTrue(base.is_dir())

    def test_an_install_is_handed_false_for_an_unset_dry_run(self) -> None:
        calls: "list[object]" = []

        def record(*args: object, dry_run: object, **kwargs: object) -> int:
            calls.append(dry_run)
            return 0

        with mock.patch.object(backup_launchd, "install", record):
            self.assertEqual(self.main("--mode", "install"), 0)
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0], False)


if __name__ == "__main__":
    unittest.main()
