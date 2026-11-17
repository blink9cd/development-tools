#!/usr/bin/env python3
"""The daily agent's plist, and the install, uninstall and state read around launchd_agent.

launchctl never runs for real. Install, uninstall and the state read are handed the shared
``launchd_agent``'s own scripted ``FakeLaunchctl``, which checks each argv against a queue and
keeps fake time, so no test waits; the one adapter test routes through a FakeRunner, which
refuses an unrouted launchctl outright. ``home`` is always a temporary directory.
"""

from __future__ import annotations

import io
import plistlib
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import _fixtures as fx

import backup_launchd as bl
import launchd_agent
import os_schedule_gen
from backup_proc import NOT_STARTED_RC, TIMEOUT_RC
from backup_status import AGENT_ABSENT, AGENT_LOADED, AGENT_UNKNOWN

# launchd_agent's own scripted launchctl, from the tests/ folder beside the module, so these
# tests fake launchd exactly as the library's own suites do rather than through a copy.
sys.path.append(str(Path(launchd_agent.__file__).resolve().parent / "tests"))

from _launchd_fixtures import (  # noqa: E402 - on sys.path just above
    ABSENT,
    LOADED,
    OK,
    UID,
    Answer,
    FakeLaunchctl,
)

SERVICE = f"gui/{UID}/{bl.LABEL}"
PRINT = ["/bin/launchctl", "print", SERVICE]
BOOTOUT = ["/bin/launchctl", "bootout", SERVICE]
#: A print that is neither loaded nor absent.
UNKNOWN = (5, "", "fixture print failure\n")
#: Enough reads of one unchanging answer to outlast the library's wait deadline.
READS_PAST_THE_DEADLINE = (
    int(launchd_agent.DEFAULT_DEADLINE_S / launchd_agent.POLL_S) + 1
)


class LaunchdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.fake = FakeLaunchctl(self)
        self.spec = bl.AgentSpec(
            home=self.home,
            state_base=self.home / ".workspace_backup",
            python="/opt/fixture-python",
            script=Path(
                "/opt/fixture-kit/projects/development_tools/git_workspace_backup/git_workspace_backup.py"
            ),
            schedule=os_schedule_gen.DailySchedule(hour=6, minute=0),
            git_path="/opt/fixture-git/bin/git",
            destination=Path("/opt/fixture-dest"),
        )
        self.out, self.err = io.StringIO(), io.StringIO()

    def bootstrap(self, home: "Path | None" = None) -> list[str]:
        """The bootstrap install runs: the GUI domain and the plist under *home*."""
        target = bl.plist_path(self.home if home is None else home)
        return ["/bin/launchctl", "bootstrap", f"gui/{UID}", str(target)]

    def expect_reads(self, answer: Answer, count: int = 1) -> None:
        """Queue *count* prints of the agent, each answered *answer*."""
        for _ in range(count):
            self.fake.expect(PRINT, answer)

    def expect_loading(self) -> None:
        """Queue a clean install: bootout, absent, bootstrap, loaded."""
        self.fake.expect(BOOTOUT, OK)
        self.expect_reads(ABSENT)
        self.fake.expect(self.bootstrap(), OK)
        self.expect_reads(LOADED)

    def _install(
        self, *, dry_run: bool = False, spec: "bl.AgentSpec | None" = None
    ) -> int:
        self.out, self.err = io.StringIO(), io.StringIO()
        return bl.install(
            self.spec if spec is None else spec,
            self.fake.ctl(),
            dry_run=dry_run,
            out=self.out,
            err=self.err,
        )

    def _uninstall(self) -> int:
        self.out, self.err = io.StringIO(), io.StringIO()
        return bl.uninstall(self.home, self.fake.ctl(), out=self.out, err=self.err)

    def assert_one_error(self, *fragments: str) -> None:
        """One ERROR line on stderr naming every fragment, and nothing on stdout."""
        lines = self.err.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith("ERROR: "), lines)
        for fragment in fragments:
            self.assertIn(fragment, lines[0])
        self.assertEqual(self.out.getvalue(), "")

    def test_the_plist_runs_if_due_daily_and_at_load_with_a_minimal_path(self) -> None:
        plist = plistlib.loads(bl.render_plist(self.spec))
        self.assertEqual(plist["Label"], bl.LABEL)
        self.assertEqual(
            plist["ProgramArguments"],
            [
                "/opt/fixture-python",
                str(self.spec.script),
                *bl.AGENT_ARGS,
                "--destination",
                "/opt/fixture-dest",
            ],
        )
        self.assertEqual(bl.AGENT_ARGS, ("--mode", "run", "--if-due"))
        # No Weekday: launchd reads that as every day.
        self.assertEqual(plist["StartCalendarInterval"], {"Hour": 6, "Minute": 0})
        self.assertIs(plist["RunAtLoad"], True)
        self.assertEqual(
            plist["EnvironmentVariables"]["PATH"], "/opt/fixture-git/bin:/usr/bin:/bin"
        )
        logs = os_schedule_gen.logs_dir(self.spec.state_base)
        self.assertEqual(Path(plist["StandardErrorPath"]).parent, logs)

    def test_a_named_repository_follows_the_agent_args_and_none_adds_nothing(
        self,
    ) -> None:
        self.assertIsNone(self.spec.repo)
        pinned = replace(self.spec, repo=Path("/opt/fixture-repo"))
        args = plistlib.loads(bl.render_plist(pinned))["ProgramArguments"]
        self.assertEqual(
            args[2:],
            [
                *bl.AGENT_ARGS,
                "--repo",
                "/opt/fixture-repo",
                "--destination",
                "/opt/fixture-dest",
            ],
        )

    def test_the_default_time_path_and_plist_location(self) -> None:
        self.assertEqual(
            os_schedule_gen.parse_daily_time(bl.DEFAULT_TIME),
            os_schedule_gen.DailySchedule(hour=6, minute=0),
        )
        # git in a system directory is not repeated on PATH.
        self.assertEqual(bl.baked_path("/usr/bin/git"), "/usr/bin:/bin")
        self.assertEqual(
            bl.plist_path(self.home),
            self.home / "Library" / "LaunchAgents" / f"{bl.LABEL}.plist",
        )

    def test_agent_state_is_one_bounded_print_of_bin_launchctl(self) -> None:
        answers: "dict[str, tuple[Answer, str]]" = {
            "loaded": (LOADED, AGENT_LOADED),
            "absent": (ABSENT, AGENT_ABSENT),
            "error": (UNKNOWN, AGENT_UNKNOWN),
            "timeout": (subprocess.TimeoutExpired("launchctl", 1.0), AGENT_UNKNOWN),
            "cannot run": (OSError("no launchctl"), AGENT_UNKNOWN),
        }
        for name, (answer, expected) in answers.items():
            with self.subTest(name):
                fake = FakeLaunchctl(self).expect(PRINT, answer)
                self.assertEqual(bl.agent_state(fake.ctl()), expected)
                self.assertEqual(fake.calls, [PRINT])
                self.assertEqual(fake.timeouts, [launchd_agent.LAUNCHCTL_TIMEOUT_S])

    def test_the_host_launchctl_runs_each_call_through_the_tool_runner(self) -> None:
        seen: list[tuple[list[str], "Path | None", float]] = []
        codes = iter([0, launchd_agent.PRINT_ABSENT_RC, 5, TIMEOUT_RC, NOT_STARTED_RC])

        def route(argv: Sequence[str]) -> "tuple[int, str, str]":
            return next(codes), "fixture out", "fixture err"

        runner = fx.FakeRunner({"launchctl": route})

        def run(
            argv: Sequence[str], cwd: "Path | None", timeout: float
        ) -> "tuple[int, str, str]":
            seen.append((list(argv), cwd, timeout))
            return runner(argv, cwd, timeout)

        slept: list[str] = []
        waiter = bl.Waiter(clock=lambda: 7.0, sleep=lambda s: slept.append("s"))
        ctl = bl.host_launchctl(run, UID, waiter)
        self.assertEqual((ctl.uid, ctl.clock()), (UID, 7.0))
        ctl.sleep(0.5)
        self.assertEqual(slept, ["s"])
        states = [bl.agent_state(ctl) for _ in range(5)]
        self.assertEqual(
            states,
            [AGENT_LOADED, AGENT_ABSENT, AGENT_UNKNOWN, AGENT_UNKNOWN, AGENT_UNKNOWN],
        )
        bound = launchd_agent.LAUNCHCTL_TIMEOUT_S
        self.assertEqual(seen, [(PRINT, None, bound)] * 5)

    def test_a_dry_run_prints_the_plan_and_writes_nothing(self) -> None:
        self.assertEqual(self._install(dry_run=True), 0)
        printed = self.out.getvalue()
        self.assertIn(bl.LABEL, printed)
        self.assertIn(bl.render_plist(self.spec).decode("utf-8"), printed)
        self.assertIn(f"  {shlex.join(BOOTOUT)}", printed)
        self.assertIn(f"  {shlex.join(self.bootstrap())}", printed)
        self.assertFalse(bl.plist_path(self.home).exists())
        self.assertFalse(self.spec.state_base.exists())
        self.assertEqual(self.fake.calls, [])

    def test_install_writes_then_unloads_then_bootstraps_then_confirms(self) -> None:
        # The first install has nothing to boot out, so bootout's exit is not the check.
        for bootout in (OK, (3, "", "no such service")):
            with self.subTest(bootout=bootout):
                home = Path(self.enterContext(tempfile.TemporaryDirectory()))
                spec = replace(
                    self.spec, home=home, state_base=home / ".workspace_backup"
                )
                target = bl.plist_path(home)
                self.fake = FakeLaunchctl(self)
                seen: list[bool] = []
                self.fake.before_each = lambda: seen.append(target.exists())
                self.fake.expect(BOOTOUT, bootout).expect(PRINT, ABSENT)
                self.fake.expect(self.bootstrap(home), OK).expect(PRINT, LOADED)
                self.assertEqual(self._install(spec=spec), 0, self.err.getvalue())
                self.assertEqual(self.fake.script, [])
                self.assertEqual(seen, [True] * 4)
                self.assertEqual(
                    set(self.fake.timeouts), {launchd_agent.LAUNCHCTL_TIMEOUT_S}
                )
                logs = os_schedule_gen.logs_dir(spec.state_base)
                self.assertEqual(stat.S_IMODE(logs.stat().st_mode), 0o700)
                self.assertEqual(target.read_bytes(), bl.render_plist(spec))
                self.assertEqual(self.err.getvalue(), "")

    def test_install_names_the_run_that_proves_it_and_where_a_failure_shows(
        self,
    ) -> None:
        self.expect_loading()
        self.assertEqual(self._install(), 0, self.err.getvalue())
        printed = self.out.getvalue()
        # A kickstart runs --if-due, which on a day already covered only records a skip.
        self.assertNotIn(shlex.join(["/bin/launchctl", "kickstart", SERVICE]), printed)
        self.assertIn("next scheduled firing", printed)
        self.assertIn('"last exit code"', printed)
        error_log = plistlib.loads(bl.render_plist(self.spec))["StandardErrorPath"]
        status = [self.spec.python, str(self.spec.script), "--mode", "status"]
        status += ["--destination", "/opt/fixture-dest"]
        commands = [line.strip() for line in printed.splitlines() if line[:2] == "  "]
        self.assertEqual(
            commands, [shlex.join(status), shlex.quote(error_log), shlex.join(PRINT)]
        )

    def test_a_failed_bootstrap_that_launchd_then_reports_loaded_is_installed(
        self,
    ) -> None:
        # Reinstalling while the old agent still tears down: print already says absent, and
        # bootstrap fails with an I/O error that the next reads of launchd's state outlast.
        self.fake.expect(BOOTOUT, OK).expect(PRINT, ABSENT)
        self.fake.expect(self.bootstrap(), (5, "", "Input/output error"))
        self.expect_reads(ABSENT)
        self.expect_reads(LOADED)
        self.assertEqual(self._install(), 0, self.err.getvalue())
        self.assertEqual(self.fake.script, [])

    def test_an_old_agent_that_never_unloads_is_launchds_error_and_recoverable(
        self,
    ) -> None:
        self.fake.expect(BOOTOUT, OK)
        self.expect_reads(LOADED, READS_PAST_THE_DEADLINE)
        self.assertEqual(self._install(), 2)
        deadline = f"{launchd_agent.DEFAULT_DEADLINE_S:g} s"
        self.assert_one_error(
            f"{bl.LABEL}: still loaded after {deadline}", "re-running install recovers"
        )
        self.assertTrue(bl.plist_path(self.home).exists())

    def test_an_unknown_answer_ends_install_at_once_and_says_so(self) -> None:
        for after_bootstrap in (False, True):
            with self.subTest(after_bootstrap=after_bootstrap):
                self.fake = FakeLaunchctl(self).expect(BOOTOUT, OK)
                if after_bootstrap:
                    self.expect_reads(ABSENT)
                    self.fake.expect(self.bootstrap(), OK)
                self.expect_reads(UNKNOWN)
                self.assertEqual(self._install(), 2)
                self.assertEqual(self.fake.sleeps, [])
                self.assert_one_error(
                    "answered unknown (exit 5: fixture print failure)",
                    "an agent loaded before this install may already be unloaded",
                    "re-running install recovers",
                )

    def test_a_bootstrap_that_fails_and_never_loads_names_its_failure(self) -> None:
        self.fake.expect(BOOTOUT, OK).expect(PRINT, ABSENT)
        self.fake.expect(self.bootstrap(), (5, "", "fixture bootstrap error\n"))
        self.expect_reads(ABSENT, READS_PAST_THE_DEADLINE)
        self.assertEqual(self._install(), 2)
        self.assert_one_error(
            "still absent",
            "launchctl bootstrap failed (exit 5): fixture bootstrap error",
            "re-running install recovers",
        )

    def test_a_launchctl_that_cannot_run_is_one_error_line(self) -> None:
        self.fake.expect(BOOTOUT, OSError("fixture: no launchctl"))
        self.assertEqual(self._install(), 2)
        self.assert_one_error(
            "could not run launchctl bootout", "re-running install recovers"
        )

    def test_an_unusable_spec_exits_2_before_any_write_or_verb(self) -> None:
        cases = {
            "relative python": replace(self.spec, python="fixture-python"),
            "relative script": replace(self.spec, script=Path("workspace_backup.py")),
            "relative git": replace(self.spec, git_path="bin/git"),
            "relative state": replace(self.spec, state_base=Path(".workspace_backup")),
            "relative home": replace(self.spec, home=Path("fixture-home")),
            "colon in git's directory": replace(self.spec, git_path="/opt/a:b/git"),
            "relative repository": replace(self.spec, repo=Path("fixture-repo")),
            "relative destination": replace(self.spec, destination=Path("fixture-d")),
            # plistlib raises on one, which would be a traceback rather than this line.
            "control character": replace(self.spec, script=Path("/opt/fixture\x01kit")),
            "NUL": replace(self.spec, repo=Path("/opt/fixture\x00repo")),
            "DEL": replace(self.spec, destination=Path("/opt/fixture\x7fdest")),
            "C1": replace(self.spec, git_path="/opt/fixture\x85bin/git"),
        }
        for label, spec in cases.items():
            for dry_run in (True, False):
                with self.subTest(label, dry_run=dry_run):
                    self.assertEqual(self._install(dry_run=dry_run, spec=spec), 2)
                    self.assertEqual(self.fake.calls, [])
                    self.assert_one_error()
        self.assertFalse(bl.plist_path(self.home).exists())
        self.assertFalse(self.spec.state_base.exists())

    def test_a_plist_or_log_folder_that_cannot_be_written_exits_2_before_any_verb(
        self,
    ) -> None:
        blockers = {"logs": self.spec.state_base, "plist": self.home / "Library"}
        for name, blocker in blockers.items():
            with self.subTest(name):
                blocker.write_bytes(b"not a directory")
                self.assertEqual(self._install(), 2)
                self.assertEqual(self.fake.calls, [])
                self.assert_one_error("could not")
                blocker.unlink()

    def test_printed_commands_survive_paths_with_spaces(self) -> None:
        spec = replace(
            self.spec,
            python="/opt/fixture python/bin/python3",
            script=Path("/opt/fixture kit/workspace_backup.py"),
        )
        self.expect_loading()
        self.assertEqual(self._install(spec=spec), 0)
        commands = [
            shlex.split(line)
            for line in self.out.getvalue().splitlines()
            if "--mode status" in line
        ]
        status = [spec.python, str(spec.script), "--mode", "status"]
        self.assertEqual(commands, [[*status, "--destination", "/opt/fixture-dest"]])

    def test_uninstall_removes_the_plist_only_once_launchd_confirms(self) -> None:
        target = bl.plist_path(self.home)
        target.parent.mkdir(parents=True)
        target.write_bytes(b"plist")
        self.fake.expect(BOOTOUT, OK)
        self.expect_reads(LOADED, READS_PAST_THE_DEADLINE)
        self.assertEqual(self._uninstall(), 2)
        self.assertTrue(target.exists())
        self.assert_one_error("still loaded", "its plist is kept")
        # A bootout that fails is not the check: the agent may never have been loaded.
        seen: list[bool] = []
        self.fake.before_each = lambda: seen.append(target.exists())
        self.fake.expect(BOOTOUT, (3, "", "no such service")).expect(PRINT, ABSENT)
        self.assertEqual(self._uninstall(), 0)
        self.assertFalse(target.exists())
        self.assertEqual(seen, [True, True])
        self.fake.expect(BOOTOUT, OK).expect(PRINT, ABSENT)
        self.assertEqual(self._uninstall(), 0)

    def test_uninstall_keeps_the_plist_when_launchd_answers_unknown(self) -> None:
        target = bl.plist_path(self.home)
        target.parent.mkdir(parents=True)
        target.write_bytes(b"plist")
        self.fake.expect(BOOTOUT, OK).expect(PRINT, UNKNOWN)
        self.assertEqual(self._uninstall(), 2)
        self.assertEqual(self.fake.sleeps, [])
        self.assertTrue(target.exists())
        self.assert_one_error("exit 5: fixture print failure", "its plist is kept")

    def test_uninstall_reports_a_plist_it_cannot_remove(self) -> None:
        # A directory where the plist belongs: unlink refuses it.
        bl.plist_path(self.home).mkdir(parents=True)
        self.fake.expect(BOOTOUT, OK).expect(PRINT, ABSENT)
        self.assertEqual(self._uninstall(), 2)
        self.assertIn("could not remove", self.err.getvalue())
        self.assertTrue(bl.plist_path(self.home).is_dir())


if __name__ == "__main__":
    unittest.main()
