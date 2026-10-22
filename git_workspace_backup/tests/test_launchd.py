#!/usr/bin/env python3
"""The daily agent's plist, its three-answer state, and install/uninstall ordering.

launchctl never runs for real: every call is answered by a FakeRunner route, which refuses an
unrouted launchctl outright. ``home`` is always a temporary directory, the uid is a fixture
literal, and every wait runs on a fake clock whose sleeps are recorded, never slept.
"""

from __future__ import annotations

import io
import plistlib
import shlex
import tempfile
import unittest
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import _fixtures as fx

import backup_launchd as bl
import schedule_artifacts
from backup_proc import LAUNCHCTL_TIMEOUT_S, NOT_STARTED_RC, TIMEOUT_RC
from backup_status import AGENT_ABSENT, AGENT_LOADED, AGENT_UNKNOWN

#: The uid every test hands the module; never the host's own.
FIXTURE_UID = 501
#: launchctl print exits that are neither loaded nor absent: an error, a timeout, no binary.
UNKNOWN_CODES = (5, TIMEOUT_RC, NOT_STARTED_RC)
SERVICE = f"gui/{FIXTURE_UID}/{bl.LABEL}"
#: The fake clock's deadline, and so how many polls a wait that never settles makes.
DEADLINE_S = 2.0
STUCK_SLEEPS = [bl.POLL_INTERVAL_S] * int(DEADLINE_S / bl.POLL_INTERVAL_S)


class FakeLaunchctl:
    """Answers ``print`` from a queue (absent once it runs out) and records every call.

    When *watch* is given, each verb also records whether that path existed as it arrived.
    """

    def __init__(
        self,
        print_codes: Sequence[int] = (),
        *,
        bootstrap_code: int = 0,
        bootout_code: int = 0,
        watch: "Path | None" = None,
    ) -> None:
        self.print_codes = list(print_codes)
        self.bootstrap_code = bootstrap_code
        self.bootout_code = bootout_code
        self.watch = watch
        self.argvs: list[list[str]] = []
        self.seen: list[tuple[str, bool]] = []

    @property
    def verbs(self) -> list[str]:
        """The verb of every call, in order."""
        return [argv[1] for argv in self.argvs]

    def __call__(self, argv: Sequence[str]) -> "tuple[int, str, str]":
        verb = argv[1]
        self.argvs.append(list(argv))
        if self.watch is not None:
            self.seen.append((verb, self.watch.exists()))
        if verb == "print":
            code = self.print_codes.pop(0) if self.print_codes else bl.PRINT_ABSENT_RC
            return code, "", f"fixture print {code}" if code else ""
        if verb == "bootstrap":
            failed = "fixture bootstrap error" if self.bootstrap_code else ""
            return self.bootstrap_code, "", failed
        return self.bootout_code, "", ""


class Recorder:
    """A RunCmd routing launchctl through FakeRunner, recording each call's cwd and bound."""

    def __init__(self, launchctl: fx.Route) -> None:
        self.runner = fx.FakeRunner({"launchctl": launchctl})
        self.bounds: list[tuple["Path | None", float]] = []

    def __call__(
        self, argv: Sequence[str], cwd: "Path | None", timeout: float
    ) -> "tuple[int, str, str]":
        self.bounds.append((cwd, timeout))
        return self.runner(argv, cwd, timeout)


class FakeClock:
    """A monotonic clock that only moves when slept on."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class LaunchdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.clock = FakeClock()
        self.waiter = bl.Waiter(
            clock=lambda: self.clock.now, sleep=self.clock.sleep, deadline_s=DEADLINE_S
        )
        self.spec = bl.AgentSpec(
            home=self.home,
            state_base=self.home / ".workspace_backup",
            python="/opt/fixture-python",
            script=Path(
                "/opt/fixture-kit/projects/development_tools/git_workspace_backup/git_workspace_backup.py"
            ),
            schedule=schedule_artifacts.DailySchedule(hour=6, minute=0),
            git_path="/opt/fixture-git/bin/git",
            uid=FIXTURE_UID,
            destination=Path("/opt/fixture-dest"),
        )
        self.out, self.err = io.StringIO(), io.StringIO()

    def _install(
        self,
        launchctl: fx.Route,
        *,
        dry_run: bool = False,
        spec: "bl.AgentSpec | None" = None,
    ) -> int:
        self.out, self.err = io.StringIO(), io.StringIO()
        return bl.install(
            self.spec if spec is None else spec,
            run=fx.FakeRunner({"launchctl": launchctl}),
            waiter=self.waiter,
            dry_run=dry_run,
            out=self.out,
            err=self.err,
        )

    def _uninstall(self, launchctl: fx.Route) -> int:
        self.out, self.err = io.StringIO(), io.StringIO()
        return bl.uninstall(
            self.home,
            FIXTURE_UID,
            run=fx.FakeRunner({"launchctl": launchctl}),
            waiter=self.waiter,
            out=self.out,
            err=self.err,
        )

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
        logs = schedule_artifacts.logs_dir(self.spec.state_base)
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
            schedule_artifacts.parse_daily_time(bl.DEFAULT_TIME),
            schedule_artifacts.DailySchedule(hour=6, minute=0),
        )
        # git in a system directory is not repeated on PATH.
        self.assertEqual(bl.baked_path("/usr/bin/git"), "/usr/bin:/bin")
        self.assertEqual(
            bl.plist_path(self.home),
            self.home / "Library" / "LaunchAgents" / f"{bl.LABEL}.plist",
        )

    def test_agent_state_reads_three_answers_through_one_bounded_print(self) -> None:
        answers = {0: AGENT_LOADED, bl.PRINT_ABSENT_RC: AGENT_ABSENT}
        answers.update(dict.fromkeys(UNKNOWN_CODES, AGENT_UNKNOWN))
        for code, expected in answers.items():
            with self.subTest(code=code):
                launchctl = FakeLaunchctl([code])
                runner = Recorder(launchctl)
                self.assertEqual(bl.agent_state(FIXTURE_UID, run=runner), expected)
                self.assertEqual(launchctl.argvs, [[bl.LAUNCHCTL, "print", SERVICE]])
                self.assertEqual(runner.bounds, [(None, LAUNCHCTL_TIMEOUT_S)])

    def test_waiting_polls_until_the_wanted_state_or_the_deadline(self) -> None:
        runner = fx.FakeRunner({"launchctl": FakeLaunchctl([0, 0])})
        reading = bl.wait_for_state(
            FIXTURE_UID, AGENT_ABSENT, run=runner, waiter=self.waiter
        )
        self.assertEqual(reading.state, AGENT_ABSENT)
        self.assertEqual(self.clock.sleeps, [bl.POLL_INTERVAL_S] * 2)
        self.clock.sleeps.clear()
        loading = fx.FakeRunner({"launchctl": FakeLaunchctl([bl.PRINT_ABSENT_RC, 0])})
        reading = bl.wait_for_state(
            FIXTURE_UID, AGENT_LOADED, run=loading, waiter=self.waiter
        )
        self.assertEqual(
            (reading.state, self.clock.sleeps), (AGENT_LOADED, [bl.POLL_INTERVAL_S])
        )
        self.clock.sleeps.clear()
        stuck = fx.FakeRunner({"launchctl": FakeLaunchctl([0] * 20)})
        reading = bl.wait_for_state(
            FIXTURE_UID, AGENT_ABSENT, run=stuck, waiter=self.waiter
        )
        self.assertEqual((reading.state, reading.code), (AGENT_LOADED, 0))
        self.assertEqual(self.clock.sleeps, STUCK_SLEEPS)

    def test_an_unknown_answer_ends_the_wait_at_once(self) -> None:
        for wanted in (AGENT_ABSENT, AGENT_LOADED):
            for code in UNKNOWN_CODES:
                with self.subTest(wanted=wanted, code=code):
                    self.clock.sleeps.clear()
                    runner = fx.FakeRunner({"launchctl": FakeLaunchctl([code])})
                    reading = bl.wait_for_state(
                        FIXTURE_UID, wanted, run=runner, waiter=self.waiter
                    )
                    self.assertEqual(reading.state, AGENT_UNKNOWN)
                    self.assertEqual(reading.code, code)
                    self.assertEqual(reading.detail, f"fixture print {code}")
                    self.assertEqual(self.clock.sleeps, [])

    def test_waiting_for_an_unsettled_state_is_refused(self) -> None:
        runner = fx.FakeRunner({"launchctl": FakeLaunchctl()})
        for wanted in (AGENT_UNKNOWN, "bogus"):
            with self.subTest(wanted), self.assertRaises(ValueError):
                bl.wait_for_state(FIXTURE_UID, wanted, run=runner, waiter=self.waiter)

    def test_a_dry_run_prints_the_plan_and_writes_nothing(self) -> None:
        launchctl = FakeLaunchctl()
        self.assertEqual(self._install(launchctl, dry_run=True), 0)
        printed = self.out.getvalue()
        self.assertIn(bl.LABEL, printed)
        self.assertIn(bl.render_plist(self.spec).decode("utf-8"), printed)
        self.assertIn(str(bl.plist_path(self.home)), printed)
        self.assertFalse(bl.plist_path(self.home).exists())
        self.assertFalse(self.spec.state_base.exists())
        self.assertEqual(launchctl.verbs, [])

    def test_install_writes_then_unloads_then_bootstraps_then_confirms(self) -> None:
        # The first install has nothing to boot out, so bootout's exit is not the check.
        for bootout_code in (0, 3):
            with self.subTest(bootout_code=bootout_code):
                # A fresh home each time: the watch must see this install's own plist, not
                # one an earlier iteration left.
                home = Path(self.enterContext(tempfile.TemporaryDirectory()))
                spec = replace(
                    self.spec, home=home, state_base=home / ".workspace_backup"
                )
                target = bl.plist_path(home)
                self.assertFalse(target.exists())
                self.out, self.err = io.StringIO(), io.StringIO()
                launchctl = FakeLaunchctl(
                    [bl.PRINT_ABSENT_RC, 0], bootout_code=bootout_code, watch=target
                )
                runner = Recorder(launchctl)
                code = bl.install(
                    spec,
                    run=runner,
                    waiter=self.waiter,
                    dry_run=False,
                    out=self.out,
                    err=self.err,
                )
                self.assertEqual(code, 0, self.err.getvalue())
                self.assertEqual(
                    launchctl.argvs,
                    [
                        [bl.LAUNCHCTL, "bootout", SERVICE],
                        [bl.LAUNCHCTL, "print", SERVICE],
                        [bl.LAUNCHCTL, "bootstrap", f"gui/{FIXTURE_UID}", str(target)],
                        [bl.LAUNCHCTL, "print", SERVICE],
                    ],
                )
                self.assertEqual(launchctl.seen[0], ("bootout", True))
                self.assertEqual(set(runner.bounds), {(None, LAUNCHCTL_TIMEOUT_S)})
                self.assertTrue(schedule_artifacts.logs_dir(spec.state_base).is_dir())
                self.assertEqual(target.read_bytes(), bl.render_plist(spec))
                self.assertEqual(self.err.getvalue(), "")

    def test_install_names_the_run_that_proves_it_and_where_a_failure_shows(
        self,
    ) -> None:
        self.assertEqual(self._install(FakeLaunchctl([bl.PRINT_ABSENT_RC, 0])), 0)
        printed = self.out.getvalue()
        # A kickstart runs --if-due, which on a day already covered only records a skip.
        self.assertNotIn(shlex.join([bl.LAUNCHCTL, "kickstart", SERVICE]), printed)
        self.assertIn("next scheduled firing", printed)
        self.assertIn('"last exit code"', printed)
        error_log = plistlib.loads(bl.render_plist(self.spec))["StandardErrorPath"]
        status = [self.spec.python, str(self.spec.script), "--mode", "status"]
        status += ["--destination", "/opt/fixture-dest"]
        commands = [line.strip() for line in printed.splitlines() if line[:2] == "  "]
        self.assertEqual(
            commands,
            [
                shlex.join(status),
                shlex.quote(error_log),
                shlex.join([bl.LAUNCHCTL, "print", SERVICE]),
            ],
        )

    def test_a_failed_bootstrap_that_launchd_then_reports_loaded_is_installed(
        self,
    ) -> None:
        # Reinstalling while the old agent still tears down: print already says absent, and
        # bootstrap fails with an I/O error that the next reads of launchd's state outlast.
        absent = bl.PRINT_ABSENT_RC
        launchctl = FakeLaunchctl([absent, absent, 0], bootstrap_code=5)
        self.assertEqual(self._install(launchctl), 0, self.err.getvalue())
        self.assertEqual(
            launchctl.verbs, ["bootout", "print", "bootstrap", "print", "print"]
        )

    def test_each_install_failure_exits_2_and_says_why(self) -> None:
        absent = bl.PRINT_ABSENT_RC
        cases: dict[str, tuple[FakeLaunchctl, list[str], tuple[str, ...]]] = {
            "old agent never unloads": (
                FakeLaunchctl([0] * 20),
                ["bootout", *["print"] * 5],
                ("still loaded",),
            ),
            # A failed bootstrap is not final until launchd's state confirms it.
            "bootstrap fails and never loads": (
                FakeLaunchctl([absent], bootstrap_code=5),
                ["bootout", "print", "bootstrap", *["print"] * 5],
                (
                    "bootstrap failed (exit 5)",
                    "fixture bootstrap error",
                    "still absent",
                ),
            ),
            "not loaded afterwards": (
                FakeLaunchctl([absent]),
                ["bootout", "print", "bootstrap", *["print"] * 5],
                ("still absent",),
            ),
        }
        for code in UNKNOWN_CODES:
            cases[f"unknown while unloading {code}"] = (
                FakeLaunchctl([code]),
                ["bootout", "print"],
                (f"exited {code}", f"fixture print {code}"),
            )
            cases[f"unknown once bootstrapped {code}"] = (
                FakeLaunchctl([absent, code]),
                ["bootout", "print", "bootstrap", "print"],
                (f"exited {code}",),
            )
        for label, (launchctl, verbs, fragments) in cases.items():
            with self.subTest(label):
                self.assertEqual(self._install(launchctl), 2)
                self.assertEqual(launchctl.verbs, verbs)
                for fragment in fragments:
                    self.assertIn(fragment, self.err.getvalue())
                # What is left, and the way out: a first install has no previous agent to
                # name, and every one of these leaves a plist a second install replaces.
                self.assertNotIn("previous agent", self.err.getvalue())
                self.assertIn("re-running install recovers", self.err.getvalue())

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
                    launchctl = FakeLaunchctl()
                    self.assertEqual(
                        self._install(launchctl, dry_run=dry_run, spec=spec), 2
                    )
                    self.assertEqual(launchctl.verbs, [])
                    self.assertIn("ERROR", self.err.getvalue())
                    self.assertEqual(self.out.getvalue(), "")
        self.assertFalse(bl.plist_path(self.home).exists())
        self.assertFalse(self.spec.state_base.exists())

    def test_a_plist_that_cannot_be_written_exits_2_before_any_verb(self) -> None:
        (self.home / "Library").write_bytes(b"not a directory")
        launchctl = FakeLaunchctl()
        self.assertEqual(self._install(launchctl), 2)
        self.assertEqual(launchctl.verbs, [])
        self.assertIn("could not", self.err.getvalue())

    def test_printed_commands_survive_paths_with_spaces(self) -> None:
        spec = replace(
            self.spec,
            python="/opt/fixture python/bin/python3",
            script=Path("/opt/fixture kit/workspace_backup.py"),
        )
        self.assertEqual(
            self._install(FakeLaunchctl([bl.PRINT_ABSENT_RC, 0]), spec=spec), 0
        )
        commands = [
            shlex.split(line)
            for line in self.out.getvalue().splitlines()
            if "--mode status" in line
        ]
        self.assertEqual(
            commands,
            [
                [
                    spec.python,
                    str(spec.script),
                    "--mode",
                    "status",
                    "--destination",
                    "/opt/fixture-dest",
                ]
            ],
        )

    def test_uninstall_removes_the_plist_only_once_launchd_confirms(self) -> None:
        target = bl.plist_path(self.home)
        target.parent.mkdir(parents=True)
        target.write_bytes(b"plist")
        stuck = FakeLaunchctl([0] * 20)
        self.assertEqual(self._uninstall(stuck), 2)
        self.assertEqual(stuck.verbs, ["bootout", *["print"] * 5])
        self.assertTrue(target.exists())
        self.assertIn("kept", self.err.getvalue())
        # A bootout that fails is not the check: the agent may never have been loaded.
        gone = FakeLaunchctl(bootout_code=3, watch=target)
        self.assertEqual(self._uninstall(gone), 0)
        self.assertFalse(target.exists())
        self.assertEqual(gone.seen, [("bootout", True), ("print", True)])
        self.assertEqual(self._uninstall(FakeLaunchctl()), 0)

    def test_uninstall_keeps_the_plist_when_launchd_answers_unknown(self) -> None:
        target = bl.plist_path(self.home)
        target.parent.mkdir(parents=True)
        target.write_bytes(b"plist")
        for code in UNKNOWN_CODES:
            with self.subTest(code=code):
                self.clock.sleeps.clear()
                launchctl = FakeLaunchctl([code])
                self.assertEqual(self._uninstall(launchctl), 2)
                self.assertEqual(launchctl.verbs, ["bootout", "print"])
                self.assertEqual(self.clock.sleeps, [])
                self.assertTrue(target.exists())
                self.assertIn(f"exited {code}", self.err.getvalue())

    def test_uninstall_reports_a_plist_it_cannot_remove(self) -> None:
        # A directory where the plist belongs: unlink refuses it.
        bl.plist_path(self.home).mkdir(parents=True)
        self.assertEqual(self._uninstall(FakeLaunchctl()), 2)
        self.assertIn("could not remove", self.err.getvalue())
        self.assertTrue(bl.plist_path(self.home).is_dir())


if __name__ == "__main__":
    unittest.main()
