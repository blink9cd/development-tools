#!/usr/bin/env python3
"""``--repo``: backing up a repository other than the checkout the script sits in.

Every Host here starts somewhere ``--repo`` must override: a folder outside any checkout, so
a run that ignored the option would refuse rather than back up the fixture tree by accident.
launchctl never runs for real; FakeLaunchctl answers every verb.
"""

from __future__ import annotations

import plistlib
import shlex
import unittest
from dataclasses import replace
from pathlib import Path

import _cli_case as cc

import backup_launchd
import schedule_artifacts
import git_workspace_backup as wb

#: What install and its dry run say about which repository the one agent backs up.
BACKS_UP = "The agent backs up"
ONE_AGENT = "one agent per account"
#: What marks the status command install prints.
STATUS = "--mode status"


class RepoOptionTests(cc.CliCase):
    def setUp(self) -> None:
        super().setUp()
        self.elsewhere = self.tmp / "elsewhere"
        self.elsewhere.mkdir()

    def outside(self) -> wb.Host:
        """A Host whose own start is outside any checkout, as a separate clone's may be."""
        return replace(self.host(), start=self.elsewhere)

    def installed_args(self) -> "list[str]":
        """The ProgramArguments of the plist install wrote."""
        plist = backup_launchd.plist_path(self.tree.home).read_bytes()
        return list(plistlib.loads(plist)["ProgramArguments"])

    def test_run_and_status_back_up_the_named_repository(self) -> None:
        repo = ["--repo", str(self.tree.workspace)]
        self.assertEqual(self.main("--mode", "run", *repo, host=self.outside()), 0)
        self.assertEqual([p.name for p in self.destination.iterdir()], [cc.FIRST_NAME])
        self.write_plist()
        code = self.main("--mode", "status", *repo, host=self.outside())
        self.assertEqual(code, 0, self.err.getvalue())
        self.assertIn("healthy", self.out.getvalue())

    def test_a_linked_worktree_or_a_home_relative_path_means_its_primary_checkout(
        self,
    ) -> None:
        home_relative = Path("~", self.tree.workspace.relative_to(self.tmp))
        home = replace(self.outside(), home=self.tmp)
        for label, raw, host in (
            ("linked worktree", str(self.tmp / "wt"), self.outside()),
            ("home-relative", str(home_relative), home),
        ):
            with self.subTest(label):
                self.launchctl.prints = [backup_launchd.PRINT_ABSENT_RC, 0]
                argv = ["--mode", "install", "--repo", raw]
                self.assertEqual(self.main(*argv, host=host), 0, self.err.getvalue())
                plist = backup_launchd.plist_path(host.home).read_bytes()
                args = plistlib.loads(plist)["ProgramArguments"]
                self.assertEqual(args[-4:-2], ["--repo", str(self.tree.workspace)])

    def test_install_bakes_the_repository_and_the_agent_runs_with_it(self) -> None:
        self.launchctl.prints = [backup_launchd.PRINT_ABSENT_RC, 0]
        argv = ["--mode", "install", "--repo", str(self.tree.workspace)]
        self.assertEqual(self.main(*argv, host=self.outside()), 0, self.err.getvalue())
        baked = self.installed_args()
        self.assertEqual(
            baked,
            [
                cc.FIXTURE_PYTHON,
                str(self.script),
                *backup_launchd.AGENT_ARGS,
                "--repo",
                str(self.tree.workspace),
                "--destination",
                str(self.destination),
            ],
        )
        self.assertIn(f"{BACKS_UP} {self.tree.workspace}", self.out.getvalue())
        self.assertIn(ONE_AGENT, self.out.getvalue())
        # What the agent runs every morning, from a start that is no checkout at all.
        self.assertEqual(self.main(*baked[2:], host=self.outside()), 0)
        self.assertEqual([p.name for p in self.destination.iterdir()], [cc.FIRST_NAME])

    def test_install_without_repo_writes_the_plist_it_always_wrote(self) -> None:
        # A reinstall of the live agent without --repo changes it by the pinned destination
        # alone: one option and its value, and not one byte more.
        self.launchctl.prints = [backup_launchd.PRINT_ABSENT_RC, 0]
        self.assertEqual(self.main("--mode", "install"), 0)
        before_the_option = schedule_artifacts.build_launchd_plist(
            schedule_artifacts.DailySchedule(hour=6, minute=0),
            cc.FIXTURE_PYTHON,
            str(self.script),
            self.tree.home / wb.STATE_BASE_NAME,
            backup_launchd.baked_path(cc.FIXTURE_GIT),
            label=backup_launchd.LABEL,
            args=("--mode", "run", "--if-due", "--destination", str(self.destination)),
            run_at_load=True,
        )
        plist = backup_launchd.plist_path(self.tree.home).read_bytes()
        self.assertEqual(plist, before_the_option)
        self.assertIn(f"{BACKS_UP} {self.tree.workspace}", self.out.getvalue())

    def test_the_printed_status_command_judges_what_the_agent_backs_up(self) -> None:
        # Copied from a start outside every checkout, a status line without --repo would
        # judge the script's own folder, and exit 2 there.
        self.launchctl.prints = [backup_launchd.PRINT_ABSENT_RC, 0]
        argv = ["--mode", "install", "--repo", str(self.tree.workspace)]
        self.assertEqual(self.main(*argv, host=self.outside()), 0, self.err.getvalue())
        [line] = [line for line in self.out.getvalue().splitlines() if STATUS in line]
        status = shlex.split(line)
        self.assertEqual(
            status,
            [
                cc.FIXTURE_PYTHON,
                str(self.script),
                "--mode",
                "status",
                "--repo",
                str(self.tree.workspace),
                "--destination",
                str(self.destination),
            ],
        )
        self.assertEqual(self.main(*self.installed_args()[2:], host=self.outside()), 0)
        code = self.main(*status[2:], host=self.outside())
        self.assertEqual(code, 0, self.err.getvalue())

    def test_the_dry_run_shows_the_repository_and_writes_nothing(self) -> None:
        argv = ["--mode", "install", "--dry-run", "--repo", str(self.tree.workspace)]
        self.assertEqual(self.main(*argv, host=self.outside()), 0, self.err.getvalue())
        shown = self.out.getvalue()
        self.assertIn(f"<string>{self.tree.workspace}</string>", shown)
        self.assertIn(f"{BACKS_UP} {self.tree.workspace}", shown)
        self.assertFalse(backup_launchd.plist_path(self.tree.home).exists())
        self.assertEqual(self.launchctl.argvs, [])

    def test_an_install_that_fails_names_no_repository(self) -> None:
        # Launchd never confirms the agent loaded, so no agent backs anything up.
        self.launchctl.prints = [backup_launchd.PRINT_ABSENT_RC]
        self.launchctl.settled = backup_launchd.PRINT_ABSENT_RC
        argv = ["--mode", "install", "--repo", str(self.tree.workspace)]
        self.assertEqual(self.main(*argv, host=self.outside()), 2)
        self.assertIn("did not load", self.err.getvalue())
        self.assertNotIn(BACKS_UP, self.out.getvalue())

    def test_a_script_in_a_linked_worktree_is_still_refused(self) -> None:
        # The agent runs the script, so a worktree holding it is deleted from under the agent.
        in_worktree = replace(self.host(), start=self.tmp / "wt")
        argv = ["--mode", "install", "--repo", str(self.tree.workspace)]
        self.assertEqual(self.main(*argv, host=in_worktree), 2)
        self.assert_one_error_line("install from a script outside a linked worktree")
        self.assertIn(f"script's folder {self.tmp / 'wt'}", self.err.getvalue())
        self.assertFalse(backup_launchd.plist_path(self.tree.home).exists())

    def test_an_unusable_repo_is_refused_in_every_mode_that_reads_it(self) -> None:
        cases = {
            "relative": ("fixture-relative/repo", "absolute or start with ~/"),
            "missing": (str(self.tmp / "missing"), "existing directory"),
            "not a checkout": (str(self.elsewhere), "git"),
        }
        for mode in ("run", "status", "install"):
            for label, (raw, fragment) in cases.items():
                with self.subTest(mode=mode, repo=label):
                    argv = ["--mode", mode, "--repo", raw]
                    self.assertEqual(self.main(*argv, host=self.outside()), 2)
                    self.assert_one_error_line(fragment)
                    self.assertIn("--repo", self.err.getvalue())
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.launchctl.argvs, [])

    def test_uninstall_takes_repo_and_removes_the_one_agent(self) -> None:
        plist = self.write_plist()
        self.launchctl.settled = backup_launchd.PRINT_ABSENT_RC
        argv = ["--mode", "uninstall", "--repo", str(self.elsewhere / "any")]
        self.assertEqual(self.main(*argv, host=self.outside()), 0)
        self.assertFalse(plist.exists())


if __name__ == "__main__":
    unittest.main()
