#!/usr/bin/env python3
"""``--mode uninstall``: which repository the removed agent was backing up, and what it runs.

There is one agent per account, so uninstall removes it whichever repository it backs up.
What it reads back is the agent's own ProgramArguments, so the line it prints names the
repository that agent really ran against, and ``--repo`` naming another is said, not obeyed.
launchctl never runs for real: FakeLaunchctl answers every verb.
"""

from __future__ import annotations

import plistlib
import unittest

import _cli_case as cc

import backup_agent_args
import backup_launchd
import backup_notify
import backup_proc

#: What uninstall says about the repository the removed agent backed up.
WAS_BACKING_UP = "The removed agent was backing up"
#: What it says when --repo names another repository than that one.
NOT_THAT_ONE = "not the repository the removed agent was backing up"
#: What it says when the plist cannot tell it.
CANNOT_TELL = "cannot tell which repository the removed agent was backing up"


class UninstallReportTests(cc.CliCase):
    def uninstall(self, *extra: str) -> int:
        """Uninstall, with launchd confirming the agent gone."""
        self.launchctl.settled = backup_launchd.PRINT_ABSENT_RC
        return self.main("--mode", "uninstall", *extra)

    def named_agent(self, repo: str) -> None:
        """Write the plist of an agent installed with --repo *repo*."""
        self.write_plist(
            [*backup_launchd.AGENT_ARGS, "--repo", repo, "--destination", "/opt/d"]
        )

    def test_uninstall_names_the_repository_the_removed_agent_backed_up(self) -> None:
        repo = str(self.tmp / "fixture-repo")
        self.named_agent(repo)
        self.assertEqual(self.uninstall(), 0, self.err.getvalue())
        self.assertIn(f"{WAS_BACKING_UP} {repo}.", self.out.getvalue())
        self.assertEqual(self.err.getvalue(), "")
        self.assertFalse(backup_launchd.plist_path(self.tree.home).exists())

    def test_an_agent_without_repo_backed_up_the_checkout_holding_its_script(
        self,
    ) -> None:
        self.write_plist()
        self.assertEqual(self.uninstall(), 0, self.err.getvalue())
        self.assertIn(
            f"{WAS_BACKING_UP} the checkout holding {self.script}.", self.out.getvalue()
        )

    def test_a_different_repo_is_said_and_the_one_agent_still_removed(self) -> None:
        self.named_agent(str(self.tmp / "fixture-repo"))
        other = str(self.tmp / "fixture-other")
        self.assertEqual(self.uninstall("--repo", other), 0, self.err.getvalue())
        self.assertIn(f"--repo names {other}, {NOT_THAT_ONE}", self.out.getvalue())
        self.assertFalse(backup_launchd.plist_path(self.tree.home).exists())
        with self.subTest("an agent that named no repository"):
            self.write_plist()
            self.assertEqual(self.uninstall("--repo", other), 0)
            self.assertIn(NOT_THAT_ONE, self.out.getvalue())

    def test_the_same_repo_spelled_from_home_is_the_same_repository(self) -> None:
        self.named_agent(str(self.tree.home / "fixture-repo"))
        self.assertEqual(self.uninstall("--repo", "~/fixture-repo"), 0)
        self.assertNotIn(NOT_THAT_ONE, self.out.getvalue())

    def test_a_plist_that_cannot_tell_warns_and_the_agent_is_still_removed(
        self,
    ) -> None:
        plist = backup_launchd.plist_path(self.tree.home)
        cases = {
            "not a plist": b"fixture garbage",
            "no arguments": plistlib.dumps({"Label": backup_launchd.LABEL}),
            "arguments not strings": plistlib.dumps({"ProgramArguments": [1, 2]}),
            "no script": plistlib.dumps({"ProgramArguments": ["/opt/fixture-python"]}),
            "too large": b" " * (backup_agent_args.PLIST_READ_LIMIT + 1),
        }
        for label, data in cases.items():
            with self.subTest(label):
                plist.parent.mkdir(parents=True, exist_ok=True)
                plist.write_bytes(data)
                self.assertEqual(self.uninstall(), 0)
                [warning] = self.err.getvalue().splitlines()
                self.assertTrue(warning.startswith("WARNING: "), warning)
                self.assertIn(CANNOT_TELL, warning)
                self.assertFalse(plist.exists())
        with self.subTest("a symlink in the plist's place is not followed"):
            target = self.tmp / "fixture-target.plist"
            target.write_bytes(plistlib.dumps({"ProgramArguments": ["/a", "/b"]}))
            plist.symlink_to(target)
            self.assertEqual(self.uninstall(), 0)
            self.assertIn(CANNOT_TELL, self.err.getvalue())
            self.assertTrue(target.exists())

    def test_no_plist_says_no_agent_was_installed(self) -> None:
        self.assertEqual(self.uninstall(), 0, self.err.getvalue())
        self.assertIn("no agent plist", self.out.getvalue())
        self.assertNotIn(WAS_BACKING_UP, self.out.getvalue())

    def test_a_relative_repo_is_refused_before_anything_runs(self) -> None:
        self.write_plist()
        self.assertEqual(self.uninstall("--repo", "fixture-relative"), 2)
        self.assert_one_error_line("absolute or start with ~/")
        self.assertEqual(self.launchctl.argvs, [])

    def test_a_failed_uninstall_names_no_repository(self) -> None:
        self.named_agent(str(self.tmp / "fixture-repo"))
        self.launchctl.settled = 0
        self.assertEqual(self.main("--mode", "uninstall"), 2)
        self.assertNotIn(WAS_BACKING_UP, self.out.getvalue())


class SystemToolPathTests(cc.CliCase):
    def test_launchctl_and_osascript_run_by_absolute_path(self) -> None:
        # The agent's PATH puts git's directory first, which Homebrew makes user-writable.
        self.assertEqual(backup_proc.LAUNCHCTL, "/bin/launchctl")
        self.assertEqual(backup_proc.OSASCRIPT, "/usr/bin/osascript")
        self.launchctl.prints = [backup_launchd.PRINT_ABSENT_RC, 0]
        self.assertEqual(self.main("--mode", "install"), 0, self.err.getvalue())
        self.assertEqual({argv[0] for argv in self.launchctl.argvs}, {"/bin/launchctl"})
        self.assertTrue(backup_notify.notify("t", "m", run=self.runner))
        notified = [call for call in self.runner.calls if call[-2:] == ["t", "m"]]
        self.assertEqual([call[0] for call in notified], ["/usr/bin/osascript"])


if __name__ == "__main__":
    unittest.main()
