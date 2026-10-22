#!/usr/bin/env python3
"""``--mode status`` through the CLI: which home, state, clock and agent answer it reads.

launchctl and osascript are fake routes; the destination, home and state are temp dirs.
"""

from __future__ import annotations

import plistlib
import unittest
from dataclasses import replace
from pathlib import Path

import _cli_case as cc

import backup_launchd
import git_workspace_backup as wb
from backup_agent_args import installed_agent_from
from backup_config import icloud_drive_root
from backup_ledger import LEDGER_NAME, load_ledger, save_ledger
from backup_machine import IDENTITY_NAME
from backup_model import STATE_PROMOTED, Ledger, LedgerEntry

#: A launchctl print exit that is neither loaded nor absent.
UNKNOWN_PRINT = 5


class StatusCliTests(cc.CliCase):
    def test_status_is_unjudged_until_an_agent_is_installed_and_has_run(self) -> None:
        self.assertEqual(self.main("--mode", "status"), 2)
        self.assertIn("not installed", self.err.getvalue())
        # No plist, so launchd is not asked.
        self.assertEqual(self.launchctl.argvs, [])
        self.write_plist()
        self.assertEqual(self.main("--mode", "status"), 2)
        self.assertIn("no run record", self.err.getvalue())
        self.assertEqual(self.main("--mode", "run"), 0)
        self.assertEqual(self.main("--mode", "status"), 0)
        self.assertIn("healthy", self.out.getvalue())
        self.assertIn("not in iCloud Drive", self.out.getvalue())
        self.assertEqual(self.err.getvalue(), "")

    def test_launchds_answer_is_the_agent_state_judged(self) -> None:
        self.write_plist()
        self.assertEqual(self.main("--mode", "run"), 0)
        service = f"gui/{cc.FIXTURE_UID}/{backup_launchd.LABEL}"
        cases = (
            (backup_launchd.PRINT_ABSENT_RC, 2, "not installed"),
            (UNKNOWN_PRINT, 1, "could not be read"),
        )
        for code, expected, fragment in cases:
            with self.subTest(code):
                self.launchctl.settled = code
                self.assertEqual(self.main("--mode", "status"), expected)
                self.assertIn(fragment, self.err.getvalue())
                self.assertEqual(self.launchctl.argvs[-1][1:], ["print", service])

    def test_a_destination_in_icloud_drive_whose_files_are_not_in_icloud_exits_1(
        self,
    ) -> None:
        drive = icloud_drive_root(self.tree.home)
        drive.mkdir(parents=True)
        self.destination = drive / "backups"
        self.write_config(self.destination)
        self.write_plist()
        self.assertEqual(self.main("--mode", "run"), 0)
        self.assertEqual(self.main("--mode", "status"), 1)
        self.assertIn("yet its files are not in iCloud", self.err.getvalue())
        self.osascript.state = (True, True)
        self.assertEqual(self.main("--mode", "status"), 0)
        self.assertIn("healthy", self.out.getvalue())

    def test_problems_go_to_stderr_and_a_foreign_ledger_exits_2(self) -> None:
        self.write_plist()
        failing = self.host(self.fake_runner(memory_fails=True))
        self.assertEqual(self.main("--mode", "run", host=failing), 1)
        self.assertEqual(self.main("--mode", "status"), 1)
        self.assertIn("incomplete", self.err.getvalue())
        self.assertEqual(self.out.getvalue(), "")
        (self.state() / LEDGER_NAME).write_text("{", encoding="utf-8", newline="\n")
        self.assertEqual(self.main("--mode", "status"), 2)
        self.assert_one_error_line("not a ledger this tool wrote")

    def test_status_judges_the_destination_and_keep_it_is_given(self) -> None:
        self.write_plist()
        other = self.tmp / "other"
        self.assertEqual(self.main("--mode", "run", "--destination", str(other)), 0)
        # The configured destination has never run; the overriding one has.
        self.assertEqual(self.main("--mode", "status"), 2)
        self.assertIn("no run record", self.err.getvalue())
        given = ("--mode", "status", "--destination", str(other))
        self.assertEqual(self.main(*given), 0)
        state = self.state(other)
        older = [
            LedgerEntry(name, STATE_PROMOTED, True, "0" * 64)
            for name in ("2026-09-12T060000", "2026-09-13T060000")
        ]
        save_ledger(state, Ledger((*older, *load_ledger(state).entries)))
        # Three snapshots are within the configured keep, and more than twice a keep of 1.
        self.assertEqual(self.main(*given), 0)
        self.assertEqual(self.main(*given, "--keep", "1"), 1)
        self.assertIn("times keep (1)", self.err.getvalue())

    def test_status_tells_this_machines_folders_from_another_machines(self) -> None:
        self.write_plist()
        self.assertEqual(self.main("--mode", "run"), 0)
        for day in (12, 13):
            (self.destination / f"2026-09-{day}T060000-fixture-studio").mkdir()
        self.assertEqual(self.main("--mode", "status"), 0)
        self.assertIn("2 snapshots from other machines", self.out.getvalue())
        (self.destination / f"2026-09-11T060000-{cc.MACHINE}").mkdir()
        self.assertEqual(self.main("--mode", "status"), 1)
        self.assertIn("a run could not ledger", self.err.getvalue())

    def test_status_reads_the_kept_identity_and_refuses_an_invalid_one(self) -> None:
        self.write_plist()
        self.assertEqual(self.main("--mode", "run"), 0)
        identity = self.tree.home / wb.STATE_BASE_NAME / IDENTITY_NAME
        (self.destination / f"2026-09-11T060000-{cc.MACHINE}").mkdir()
        identity.write_text("fixture-studio\n", encoding="utf-8", newline="\n")
        # The kept file is the authority, so that folder is now another machine's.
        self.assertEqual(self.main("--mode", "status"), 0)
        self.assertIn("1 snapshot from another machine", self.out.getvalue())
        identity.write_text("Not A Label\n", encoding="utf-8", newline="\n")
        self.assertEqual(self.main("--mode", "status"), 2)
        self.assert_one_error_line(str(identity))

    def test_a_pre_identity_state_is_healthy_and_status_keeps_no_identity(
        self,
    ) -> None:
        # State from before machine identity: a bare-named snapshot, ledgered, and no
        # identity file. Status derives the label to read names with, and writes nothing.
        self.write_plist()
        self.assertEqual(self.main("--mode", "run"), 0)
        identity = self.tree.home / wb.STATE_BASE_NAME / IDENTITY_NAME
        identity.unlink()
        bare = cc.FIRST_NAME.removesuffix(f"-{cc.MACHINE}")
        (self.destination / cc.FIRST_NAME).rename(self.destination / bare)
        [kept] = load_ledger(self.state()).entries
        save_ledger(self.state(), Ledger((replace(kept, name=bare),)))
        self.assertEqual(self.main("--mode", "status"), 0)
        self.assertIn(f"healthy: newest complete snapshot {bare}", self.out.getvalue())
        self.assertNotIn("never prunes", self.out.getvalue())
        self.assertFalse(identity.exists())

    def test_an_unreadable_state_file_exits_2_with_one_line(self) -> None:
        self.write_plist()
        self.assertEqual(self.main("--mode", "run"), 0)
        ledger = self.state() / LEDGER_NAME
        ledger.unlink()
        ledger.mkdir()
        self.assertEqual(self.main("--mode", "status"), 2)
        self.assert_one_error_line("could not read the recorded state")


#: What status says when the installed agent pins another destination than the one judged.
WRITES_ELSEWHERE = "the installed agent writes to"
#: What it warns when the plist cannot say which destination the agent writes to.
CANNOT_TELL_DESTINATION = "cannot tell which destination the installed agent writes to"


class PinnedDestinationTests(cc.CliCase):
    """Status compares the destination it judges with the one install pinned in the agent."""

    def pinned_agent(self, destination: Path) -> None:
        """Write the plist of an agent install pinned to *destination*."""
        self.write_plist(
            [*backup_launchd.AGENT_ARGS, "--destination", str(destination)]
        )

    def test_an_agent_pinned_elsewhere_names_both_folders_and_exits_1(self) -> None:
        other = self.tmp / "other"
        self.pinned_agent(other)
        self.assertEqual(self.main("--mode", "run"), 0)
        self.assertEqual(self.main("--mode", "status"), 1)
        self.assertIn(
            f"{WRITES_ELSEWHERE} {other}; this status judges {self.destination}",
            self.err.getvalue(),
        )
        self.assertEqual(self.out.getvalue(), "")
        with self.subTest("judging the agent's own destination"):
            self.assertEqual(self.main("--mode", "run", "--destination", str(other)), 0)
            given = ("--mode", "status", "--destination", str(other))
            self.assertEqual(self.main(*given), 0, self.err.getvalue())
        with self.subTest("an unjudged destination still names both"):
            unrun = self.tmp / "unrun"
            self.assertEqual(
                self.main("--mode", "status", "--destination", str(unrun)), 2
            )
            self.assertIn("no run record", self.err.getvalue())
            self.assertIn(f"{WRITES_ELSEWHERE} {other}", self.err.getvalue())

    def test_a_hidden_character_in_the_pin_is_shown_escaped(self) -> None:
        # A hand-edited plist is the user's own, but what a terminal shows must still be
        # what the plist holds: DEL and a bidi override print escaped, never raw.
        self.assertEqual(self.main("--mode", "run"), 0)
        for hidden in ("\x7f", "‮"):
            with self.subTest(hidden=repr(hidden)):
                self.err.seek(0)
                self.err.truncate()
                pinned = self.tmp / f"other{hidden}fixture"
                self.pinned_agent(pinned)
                self.assertEqual(self.main("--mode", "status"), 1)
                line = self.err.getvalue()
                self.assertNotIn(hidden, line)
                self.assertIn(f"{WRITES_ELSEWHERE} {str(pinned)!r}", line)

    def test_an_agent_pinned_to_the_judged_destination_is_healthy(self) -> None:
        self.pinned_agent(self.destination)
        self.assertEqual(self.main("--mode", "run"), 0)
        self.assertEqual(self.main("--mode", "status"), 0, self.err.getvalue())
        self.assertIn("healthy", self.out.getvalue())
        with self.subTest("the same folder through a link"):
            link = self.tmp / "fixture-link"
            link.symlink_to(self.destination)
            self.pinned_agent(link)
            self.assertEqual(self.main("--mode", "status"), 0, self.err.getvalue())
        with self.subTest("the same folder in another case"):
            self.pinned_agent(self.tmp / "DEST")
            self.assertEqual(self.main("--mode", "status"), 0, self.err.getvalue())

    def test_an_agent_installed_before_the_pin_adds_no_line(self) -> None:
        self.write_plist()
        self.assertEqual(self.main("--mode", "run"), 0)
        given = ("--mode", "status", "--destination", str(self.tmp / "other"))
        self.assertEqual(self.main(*given), 2)
        self.assertNotIn(WRITES_ELSEWHERE, self.err.getvalue())
        self.assertEqual(self.main("--mode", "status"), 0, self.err.getvalue())

    def test_no_agent_adds_no_line(self) -> None:
        self.assertEqual(self.main("--mode", "status"), 2)
        self.assertNotIn(WRITES_ELSEWHERE, self.err.getvalue())
        self.assertEqual(self.launchctl.argvs, [])

    def test_a_plist_that_cannot_tell_warns_and_status_still_judges(self) -> None:
        plist = self.write_plist()
        self.assertEqual(self.main("--mode", "run"), 0)
        plist.write_bytes(b"fixture garbage")
        self.assertEqual(self.main("--mode", "status"), 0)
        [warning] = self.err.getvalue().splitlines()
        self.assertTrue(warning.startswith("WARNING: "), warning)
        self.assertIn(CANNOT_TELL_DESTINATION, warning)
        self.assertIn("healthy", self.out.getvalue())

    def test_the_agents_destination_is_read_beside_its_repository(self) -> None:
        data = plistlib.dumps(
            {
                "ProgramArguments": [
                    "/opt/fixture-python",
                    "/opt/fixture-kit/tool.py",
                    "--repo",
                    "/opt/fixture-repo",
                    "--destination",
                    "/opt/fixture-dest",
                ]
            }
        )
        agent = installed_agent_from(data)
        self.assertEqual(agent.repo, "/opt/fixture-repo")
        self.assertEqual(agent.destination, "/opt/fixture-dest")
        trailing = plistlib.dumps(
            {"ProgramArguments": ["/opt/p", "/opt/s", "--destination"]}
        )
        self.assertIsNone(installed_agent_from(trailing).destination)


if __name__ == "__main__":
    unittest.main()
