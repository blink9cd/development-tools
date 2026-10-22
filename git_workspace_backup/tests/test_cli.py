#!/usr/bin/env python3
"""The CLI's surface: options and help, the plist's arguments, the platform refusal, and run.

Every ``main()`` call gets a Host of fakes from CliCase; ``real_host()`` is only built and
inspected, never handed to ``main()``.
"""

from __future__ import annotations

import contextlib
import io
import os
import socket
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import _cli_case as cc

import backup_launchd
import backup_proc
import git_workspace_backup as wb
from backup_ledger import load_ledger

#: What a fake HOME is set to while real_host() is built.
FIXTURE_HOME = "/opt/fixture-home"


class ParserTests(unittest.TestCase):
    """The parser alone: nothing here builds a Host or runs a mode."""

    def help_text(self) -> str:
        """``--help`` with its line wrapping collapsed, so a phrase can be searched for."""
        stdout = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            self.assertRaises(SystemExit) as caught,
        ):
            wb.build_parser().parse_args(["--help"])
        self.assertEqual(caught.exception.code, 0)
        return " ".join(stdout.getvalue().split())

    def test_help_names_every_mode_option_default_and_epilog_section(self) -> None:
        text = self.help_text()
        expected = (
            *wb.MODES,
            "--mode",
            "--if-due",
            "--time",
            "--keep",
            "--destination",
            "--dry-run",
            f"(default: {backup_launchd.DEFAULT_TIME})",
            "Prerequisites:",
            "Run order:",
            "Exit codes:",
        )
        for token in expected:
            with self.subTest(token):
                self.assertIn(token, text)
        # An unset override says what unset means rather than a bare "None".
        self.assertNotIn("(default: None)", text)
        # Nor does any sentence start with one.
        self.assertNotRegex(text, r"(?:^|[.;:]\s)None\b")

    def test_prog_is_the_name_the_script_was_run_as(self) -> None:
        with mock.patch.object(sys, "argv", ["/opt/fixture-bin/renamed_backup.py"]):
            self.assertEqual(wb.build_parser().prog, "renamed_backup.py")

    def test_the_plists_baked_arguments_parse_as_a_due_run(self) -> None:
        # The installed agent's ProgramArguments and this parser are one contract: a renamed
        # flag must fail here, not make the agent exit 2 every morning.
        args = wb.build_parser().parse_args(list(backup_launchd.AGENT_ARGS))
        self.assertEqual(
            (args.mode, args.if_due, args.dry_run, args.keep, args.destination),
            ("run", True, False, None, None),
        )

    def test_mode_is_a_required_named_option(self) -> None:
        stderr = io.StringIO()
        with (
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as caught,
        ):
            wb.build_parser().parse_args([])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--mode", stderr.getvalue())

    def test_real_host_describes_this_process(self) -> None:
        with mock.patch.dict(os.environ, {"HOME": FIXTURE_HOME}):
            host = wb.real_host()
        self.assertEqual(host.home, Path(FIXTURE_HOME))
        self.assertEqual(host.script, Path(wb.__file__).resolve())
        self.assertEqual(host.start, host.script.parent)
        self.assertEqual((host.python, host.platform), (sys.executable, sys.platform))
        with tempfile.TemporaryDirectory() as tmp:
            # A file this process creates is owned by its uid: portable, and no host file read.
            self.assertEqual(host.uid, Path(tmp).stat().st_uid)
        self.assertIsNone(host.now().tzinfo, "now must be naive local time")
        # The seam itself, never its answer: no test reads this machine's host name.
        self.assertIs(host.hostname, socket.gethostname)
        self.assertIs(host.run, backup_proc.run_cmd)
        self.assertIs(host.which, shutil.which)
        self.assertEqual(host.waiter, backup_launchd.Waiter())
        self.assertIs(host.out, sys.stdout)
        self.assertIs(host.err, sys.stderr)


class CliTests(cc.CliCase):
    def test_streams_are_forced_to_utf8_before_the_options_are_parsed(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch.object(wb.stdio_utf8, "force_utf8_streams") as forced,
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit),
        ):
            wb.main(["--no-such-option"], self.host())
        forced.assert_called_once_with(sys.stdout, stderr)

    def test_every_mode_refuses_a_platform_other_than_macos(self) -> None:
        argvs = (
            ["--mode", "run"],
            ["--mode", "run", "--dry-run"],
            ["--mode", "install"],
            ["--mode", "uninstall"],
            ["--mode", "status"],
        )
        for argv in argvs:
            with self.subTest(argv):
                linux = replace(self.host(), platform="linux")
                self.assertEqual(self.main(*argv, host=linux), 2)
                self.assert_one_error_line("macOS only")
                self.assertEqual(self.runner.calls, [])
        self.assertFalse((self.tree.home / wb.STATE_BASE_NAME).exists())
        self.assertFalse(self.destination.exists())
        self.assertFalse(backup_launchd.plist_path(self.tree.home).exists())

    def test_run_takes_a_snapshot_through_this_hosts_seams(self) -> None:
        self.assertEqual(self.main("--mode", "run"), 0)
        self.assertEqual([p.name for p in self.destination.iterdir()], [cc.FIRST_NAME])
        entries = load_ledger(self.state()).entries
        self.assertEqual([entry.name for entry in entries], [cc.FIRST_NAME])
        self.assertIn(cc.FIRST_NAME, self.out.getvalue())
        memory = [argv for argv in self.runner.calls if "--mode" in argv[2:3]]
        self.assertEqual(len(memory), 1, self.runner.calls)
        self.assertEqual(memory[0][0], cc.FIXTURE_PYTHON)
        self.assertEqual(Path(memory[0][1]).parent.parent, self.script.parent.parent)
        self.assertEqual(self.osascript.notifications, [])

    def test_destination_and_keep_override_the_configuration(self) -> None:
        link = self.tmp / "link"
        link.symlink_to(self.tmp, target_is_directory=True)
        other = self.tmp / "other"
        argv = ["--mode", "run", "--destination", str(link / "other"), "--keep", "2"]
        self.assertEqual(self.main(*argv), 0)
        self.assertEqual([p.name for p in other.iterdir()], [cc.FIRST_NAME])
        self.assertFalse(self.destination.exists())
        # Keyed on the resolved spelling, as a configured destination is.
        self.assertEqual(len(load_ledger(self.state(other)).entries), 1)
        plan = ["--mode", "run", "--dry-run", "--keep", "2", "--destination", "~/away"]
        self.assertEqual(self.main(*plan), 0)
        self.assertIn(
            f"would write: {self.tree.home / 'away' / cc.FIRST_NAME}",
            self.out.getvalue(),
        )
        self.assertIn("keep: 2 complete snapshots", self.out.getvalue())

    def test_keep_below_one_is_refused_before_anything_runs(self) -> None:
        self.assertEqual(self.main("--mode", "run", "--keep", "0"), 2)
        self.assert_one_error_line("--keep")
        self.assertEqual(self.runner.calls, [])

    def test_a_relative_destination_is_refused_before_anything_runs(self) -> None:
        # A relative path would name, and key the ledger of, a different directory from each
        # working directory the CLI is started in -- the agent's included.
        argv = ["--mode", "run", "--dry-run", "--destination", "fixture-relative/dest"]
        self.assertEqual(self.main(*argv), 2)
        self.assert_one_error_line("--destination")
        self.assertEqual(self.runner.calls, [])

    def test_dry_run_passes_its_exit_code_through(self) -> None:
        self.assertEqual(self.main("--mode", "run", "--dry-run"), 0)
        self.assertIn("would write:", self.out.getvalue())
        self.assertFalse(self.destination.exists())
        self.assertFalse((self.tree.home / wb.STATE_BASE_NAME).exists())
        no_git = replace(self.host(), which=lambda name: None)
        self.assertEqual(self.main("--mode", "run", "--dry-run", host=no_git), 2)
        self.assertIn("git is not on PATH", self.err.getvalue())

    def test_an_incomplete_run_exits_1_and_posts_a_notification(self) -> None:
        failing = self.host(self.fake_runner(memory_fails=True))
        self.assertEqual(self.main("--mode", "run", host=failing), 1)
        self.assertEqual(len(self.osascript.notifications), 1)
        self.assertIn(cc.MEMORY_FAILURE, " ".join(self.osascript.notifications[0]))

    def test_a_missing_checkout_or_configuration_exits_2(self) -> None:
        for mode in ("run", "status", "install"):
            with self.subTest(mode, problem="no checkout"):
                outside = replace(self.host(), start=self.tmp)
                self.assertEqual(self.main("--mode", mode, host=outside), 2)
                self.assert_one_error_line("git")
        (self.tree.workspace / "pyproject.toml").write_text(
            "[tool.other]\nx = 1\n", encoding="utf-8", newline="\n"
        )
        for mode in ("run", "status", "install"):
            with self.subTest(mode, problem="no table"):
                self.assertEqual(self.main("--mode", mode), 2)
                self.assert_one_error_line("[tool.workspace_backup]")
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.launchctl.argvs, [])


if __name__ == "__main__":
    unittest.main()
