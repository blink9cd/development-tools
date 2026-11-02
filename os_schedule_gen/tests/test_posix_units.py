#!/usr/bin/env python3
"""The three POSIX artifacts, field by field: each field is load-bearing.

Where the unit files go, and what they contain. The Windows XML lives with the
schedule model instead: it carries no PATH bake and no user-scope location, so it has
nothing in common with these three beyond being derived from the same instant.
"""

from __future__ import annotations

import plistlib
import tempfile
import unittest
from pathlib import Path

import os_schedule_gen as sa
from _fixtures import FAKE_PATH, FAKE_PYTHON, FAKE_SCRIPT


class UnitLocationTests(unittest.TestCase):
    """Where each POSIX backend's files go: always under an injectable home.

    These are pure path arithmetic, so the base is a temp directory rather than a
    literal ``/home/<name>``: a home-rooted absolute path names a machine's user, which
    committed files keep out, and a fixture is no exception.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"

    def test_systemd_units_default_under_dot_config(self) -> None:
        self.assertEqual(
            sa.systemd_unit_dir(self.home, environ={}),
            self.home / ".config" / "systemd" / "user",
        )

    def test_systemd_units_follow_xdg_config_home_when_it_is_set(self) -> None:
        """Writing to ~/.config on a box that relocated it installs a unit systemd
        never reads, the quietest possible failure, so the variable is honored."""
        relocated = self.home / "elsewhere-cfg"
        self.assertEqual(
            sa.systemd_unit_dir(self.home, environ={"XDG_CONFIG_HOME": str(relocated)}),
            relocated / "systemd" / "user",
        )

    def test_the_launch_agent_is_user_scoped_not_system_scoped(self) -> None:
        """Under the home dir, so installing needs no root (see requires_elevation)."""
        self.assertEqual(
            sa.launchd_plist_path(self.home),
            self.home / "Library" / "LaunchAgents" / f"{sa.LAUNCHD_LABEL}.plist",
        )

    def test_the_launchd_label_carries_no_organization_identifier(self) -> None:
        """A real reverse-DNS domain here would embed an organization's identifier in source."""
        self.assertTrue(sa.LAUNCHD_LABEL.startswith("local."))


class SystemdUnitContentTests(unittest.TestCase):
    """The generated units, field by field: each field is load-bearing."""

    def setUp(self) -> None:
        self.service, self.timer = sa.build_systemd_units(
            sa.WeeklySchedule(weekday="Sunday", hour=3, minute=0),
            FAKE_PYTHON,
            FAKE_SCRIPT,
            FAKE_PATH,
        )

    def test_the_service_is_a_oneshot_not_a_daemon(self) -> None:
        self.assertIn("Type=oneshot", self.service)

    def test_the_service_bakes_the_register_time_path(self) -> None:
        """Without this the unit inherits the user manager's environment, which is a
        bare distro PATH, so every backend outside /usr/bin reports could-not-run and
        the week's verdict says nothing. Measured: 9 entries against 71 interactively."""
        self.assertIn(f'Environment="PATH={FAKE_PATH}"', self.service)

    def test_a_percent_in_the_path_is_doubled_so_systemd_keeps_the_assignment(
        self,
    ) -> None:
        """Regression for a silent drop, not a loud failure.

        systemd resolves specifiers inside Environment=, and an unrecognized one makes
        it discard the whole assignment ("Failed to resolve specifiers in PATH=...,
        ignoring"). The unit then installs cleanly, passes `systemd-analyze verify`, and
        carries no PATH at all; the only symptom is a useless verdict a week later.
        Confirmed against real systemd, which accepts the doubled form.
        """
        service, _timer = sa.build_systemd_units(
            sa.WeeklySchedule(weekday="Sunday", hour=3, minute=0),
            FAKE_PYTHON,
            FAKE_SCRIPT,
            "/opt/a%b/bin:/usr/bin",
        )
        self.assertIn('Environment="PATH=/opt/a%%b/bin:/usr/bin"', service)

    def test_an_empty_path_emits_no_environment_line_at_all(self) -> None:
        """An `Environment="PATH="` would be worse than inheriting: it would blank the
        search path outright, so even /usr/bin backends would stop resolving."""
        service, _timer = sa.build_systemd_units(
            sa.WeeklySchedule(weekday="Sunday", hour=3, minute=0),
            FAKE_PYTHON,
            FAKE_SCRIPT,
            "",
        )
        self.assertNotIn("Environment=", service)

    def test_the_service_bounds_its_own_runtime_from_the_shared_ceiling(self) -> None:
        """One policy, three notations: the same constant renders the Windows task's
        ISO 8601 limit, this seconds value, and the consumer's per-call subprocess bound.

        Two of the three are asserted in this library, which owns them. The third
        belongs to the consumer that derives its subprocess ceiling from
        ``RUN_TIME_LIMIT_SECONDS``, and that consumer's suite proves it. Asserting it here
        would invert the dependency, making a library import the consumer it exists to
        serve.
        """
        self.assertIn(f"TimeoutStartSec={sa.RUN_TIME_LIMIT_SECONDS}", self.service)

    def test_the_timer_is_persistent_so_a_missed_week_still_runs(self) -> None:
        """The counterpart of the Windows task's StartWhenAvailable: a box that was
        asleep at the scheduled instant sweeps on next boot instead of skipping."""
        self.assertIn("Persistent=true", self.timer)

    def test_the_timer_names_its_calendar_and_its_service(self) -> None:
        self.assertIn("OnCalendar=Sun 03:00", self.timer)
        self.assertIn("Unit=weekly-sweep.service", self.timer)
        self.assertIn("Description=Weekly maintenance sweep\n", self.service)
        self.assertIn("Description=Weekly maintenance sweep\n", self.timer)

    def test_the_timer_installs_into_timers_target(self) -> None:
        """Without [Install] WantedBy, `systemctl enable` has nothing to link."""
        self.assertIn("[Install]", self.timer)
        self.assertIn("WantedBy=timers.target", self.timer)

    def test_the_working_directory_is_posix_shaped_whatever_host_generated_it(
        self,
    ) -> None:
        """Regression: a host-flavored `Path` emitted a Windows WorkingDirectory.

        ``Path(script).parent`` is ``WindowsPath`` on Windows, so generating this unit
        from a Windows process produced ``WorkingDirectory=\\tmp\\spaced dir`` and real
        systemd rejected the unit with "path is not absolute". Every faked run seam had
        accepted it; only a live `systemd-analyze verify` caught it.

        This test reproduces the defect without any systemd (it fails on Windows under
        a plain ``Path`` and passes under ``PurePosixPath``), so the fix stays pinned on
        a machine that has none.
        """
        service, _timer = sa.build_systemd_units(
            sa.WeeklySchedule(weekday="Sunday", hour=3, minute=0),
            "/usr/bin/python3",
            "/opt/example/sweep/weekly_sweep.py",
            FAKE_PATH,
        )
        line = next(
            entry
            for entry in service.splitlines()
            if entry.startswith("WorkingDirectory=")
        )
        self.assertEqual(line, "WorkingDirectory=/opt/example/sweep")
        self.assertNotIn("\\", line)


class LaunchdPlistContentTests(unittest.TestCase):
    """The plist, verified by parsing it back rather than by matching its text.

    A round-trip through plistlib is a real check on the artifact: it proves the bytes
    are a well-formed plist launchd can read, which string assertions on hand-rolled
    XML cannot. This is the compensating rigor for the one backend that cannot be
    live-verified from a Windows host.
    """

    def setUp(self) -> None:
        self.state = Path("/var/state/weekly_sweep")
        self.plist = plistlib.loads(
            sa.build_launchd_plist(
                sa.WeeklySchedule(weekday="Wednesday", hour=13, minute=45),
                FAKE_PYTHON,
                FAKE_SCRIPT,
                self.state,
                FAKE_PATH,
            )
        )

    def test_the_plist_bakes_the_path_with_no_escaping_of_its_own(self) -> None:
        """A LaunchAgent gets a minimal environment for the same reason a systemd unit
        does. plistlib owns the serialization, so unlike the systemd form this needs no
        specifier doubling and no quoting: a space in an entry survives untouched."""
        spaced = plistlib.loads(
            sa.build_launchd_plist(
                sa.WeeklySchedule(weekday="Wednesday", hour=13, minute=45),
                FAKE_PYTHON,
                FAKE_SCRIPT,
                self.state,
                "/opt/a b/bin:/usr/bin",
            )
        )
        self.assertEqual(self.plist["EnvironmentVariables"]["PATH"], FAKE_PATH)
        self.assertEqual(
            spaced["EnvironmentVariables"]["PATH"], "/opt/a b/bin:/usr/bin"
        )

    def test_an_empty_path_omits_the_environment_dict(self) -> None:
        """Same reasoning as the systemd branch: baking an empty PATH is strictly worse
        than inheriting one, so emit nothing rather than an empty override."""
        bare = plistlib.loads(
            sa.build_launchd_plist(
                sa.WeeklySchedule(weekday="Wednesday", hour=13, minute=45),
                FAKE_PYTHON,
                FAKE_SCRIPT,
                self.state,
                "",
            )
        )
        self.assertNotIn("EnvironmentVariables", bare)

    def test_the_calendar_interval_carries_all_three_fields(self) -> None:
        self.assertEqual(
            self.plist["StartCalendarInterval"],
            {"Weekday": 3, "Hour": 13, "Minute": 45},
        )

    def test_the_label_matches_the_filename_the_installer_writes(self) -> None:
        self.assertEqual(self.plist["Label"], sa.LAUNCHD_LABEL)

    def test_installing_a_schedule_does_not_also_start_a_sweep(self) -> None:
        """False by default, as arming a systemd timer does not run its service: a bare
        --mode run at load starts a two-hour sweep. The sweep opts in behind --if-due."""
        self.assertIs(self.plist["RunAtLoad"], False)

    def test_a_dead_run_leaves_output_somewhere_readable(self) -> None:
        """launchd discards a dead job's streams, and this tool's own log is never
        opened if it dies early, so the plist redirects both into the state dir."""
        self.assertEqual(
            self.plist["StandardOutPath"], str(self.state / "logs" / "launchd.out.log")
        )
        self.assertEqual(
            self.plist["StandardErrorPath"],
            str(self.state / "logs" / "launchd.err.log"),
        )


class CronLineTests(unittest.TestCase):
    def test_both_paths_are_quoted_because_cron_hands_the_line_to_sh(self) -> None:
        line = sa.build_cron_line(
            sa.WeeklySchedule(weekday="Friday", hour=6, minute=30),
            "/opt/a b/python3",
            "/opt/c d/sweep.py",
            "",
        )
        self.assertEqual(
            line, '30 6 * * 5 "/opt/a b/python3" "/opt/c d/sweep.py" --mode run'
        )

    def test_the_path_is_scoped_to_this_command_not_the_whole_crontab(self) -> None:
        """A bare `PATH=` line in a crontab applies to every job below it, including
        the operator's unrelated ones: the same shared-file hazard that makes this
        backend emit-only in the first place. `VAR=value cmd` is valid sh, and cron
        hands the command to sh, so an inline prefix binds it to this job alone."""
        line = sa.build_cron_line(
            sa.WeeklySchedule(weekday="Friday", hour=6, minute=30),
            FAKE_PYTHON,
            FAKE_SCRIPT,
            FAKE_PATH,
        )
        self.assertEqual(
            line,
            f'30 6 * * 5 PATH="{FAKE_PATH}" "{FAKE_PYTHON}" "{FAKE_SCRIPT}" --mode run',
        )
        self.assertFalse(line.startswith("PATH="))

    def test_a_percent_is_backslash_escaped_for_cron_not_doubled(self) -> None:
        """The third escape for one value, and the reason escaping lives in each
        builder rather than in the capture. crontab(5): an unescaped `%` in the command
        becomes a newline and everything after it is fed to the job as stdin, so the
        command would be truncated mid-PATH. systemd wants `%%` here; cron wants `\\%`.
        """
        line = sa.build_cron_line(
            sa.WeeklySchedule(weekday="Friday", hour=6, minute=30),
            FAKE_PYTHON,
            FAKE_SCRIPT,
            "/opt/a%b/bin",
        )
        self.assertIn(r'PATH="/opt/a\%b/bin"', line)
        self.assertNotIn("%%", line)


class WeeklyPlistDefaultsTests(unittest.TestCase):
    """The keywords a second agent needs must not move a byte of the default plist."""

    def test_the_weekly_defaults_are_byte_identical_to_before(self) -> None:
        """Regression pin: a caller passing no keywords gets the bytes it always got.

        The expected side is plistlib's rendering of a literal dict (the plist as built
        before ``label``/``args``/``run_at_load`` existed), not a stored digest. A digest
        is host-dependent here: the log paths come from a host ``Path``, so a Windows
        process renders them with backslashes and hashes differently. Building those two
        paths the same host-relative way keeps the pin portable, and a bytes comparison
        shows the diff a bare hash hides.
        """
        state = Path("/var/state/weekly_sweep")
        expected = plistlib.dumps(
            {
                "Label": "local.weekly-sweep",
                "ProgramArguments": [FAKE_PYTHON, FAKE_SCRIPT, "--mode", "run"],
                "StartCalendarInterval": {"Weekday": 3, "Hour": 13, "Minute": 45},
                "WorkingDirectory": "/opt/example/sweep",
                "StandardOutPath": str(state / "logs" / "launchd.out.log"),
                "StandardErrorPath": str(state / "logs" / "launchd.err.log"),
                "RunAtLoad": False,
                "EnvironmentVariables": {"PATH": FAKE_PATH},
            }
        )
        actual = sa.build_launchd_plist(
            sa.WeeklySchedule(weekday="Wednesday", hour=13, minute=45),
            FAKE_PYTHON,
            FAKE_SCRIPT,
            state,
            FAKE_PATH,
        )
        self.assertEqual(actual, expected)


class DailyLaunchdPlistTests(unittest.TestCase):
    """The daily form, and the keywords a second LaunchAgent needs to be its own job."""

    def setUp(self) -> None:
        self.state = Path("/var/state/daily_job")
        self.plist = plistlib.loads(
            sa.build_launchd_plist(
                sa.DailySchedule(hour=6, minute=0),
                FAKE_PYTHON,
                FAKE_SCRIPT,
                self.state,
                FAKE_PATH,
                label="local.example.daily",
                args=("--mode", "run", "--if-due"),
                run_at_load=True,
            )
        )

    def test_a_daily_interval_has_no_weekday_key(self) -> None:
        """launchd reads an absent Weekday as every day; a zero would mean Sunday."""
        self.assertEqual(self.plist["StartCalendarInterval"], {"Hour": 6, "Minute": 0})

    def test_the_label_keyword_names_the_job(self) -> None:
        self.assertEqual(self.plist["Label"], "local.example.daily")

    def test_the_args_keyword_follows_the_script(self) -> None:
        self.assertEqual(
            self.plist["ProgramArguments"],
            [FAKE_PYTHON, FAKE_SCRIPT, "--mode", "run", "--if-due"],
        )

    def test_run_at_load_can_be_requested(self) -> None:
        """A daily job wants RunAtLoad: no calendar firing survives a power-off."""
        self.assertIs(self.plist["RunAtLoad"], True)

    def test_the_plist_path_follows_the_label(self) -> None:
        home = Path("/opt/fixture-home")
        self.assertEqual(
            sa.launchd_plist_path(home, label="local.example.daily"),
            home / "Library" / "LaunchAgents" / "local.example.daily.plist",
        )


class DailyTimeParseTests(unittest.TestCase):
    """The single validation point for a daily --time value."""

    def test_a_valid_time_parses(self) -> None:
        self.assertEqual(
            sa.parse_daily_time("06:00"), sa.DailySchedule(hour=6, minute=0)
        )

    def test_hhmm_is_zero_padded(self) -> None:
        self.assertEqual(sa.DailySchedule(hour=6, minute=5).hhmm, "06:05")

    def test_an_invalid_time_is_none(self) -> None:
        for bad in ("24:00", "6pm", "", "12:60"):
            with self.subTest(bad=bad):
                self.assertIsNone(sa.parse_daily_time(bad))


class EveryHourPlistTests(unittest.TestCase):
    """every_hour: a caller whose own --if-due decides, fired at its minute of every hour."""

    @staticmethod
    def _interval(
        schedule: "sa.WeeklySchedule | sa.DailySchedule", every_hour: bool
    ) -> object:
        plist = plistlib.loads(
            sa.build_launchd_plist(
                schedule,
                FAKE_PYTHON,
                FAKE_SCRIPT,
                Path("/var/state/weekly_sweep"),
                FAKE_PATH,
                every_hour=every_hour,
            )
        )
        return plist["StartCalendarInterval"]

    def test_every_hour_keeps_only_the_minute(self) -> None:
        """launchd reads an absent Hour and Weekday as every hour of every day."""
        weekly = sa.WeeklySchedule(weekday="Sunday", hour=3, minute=7)
        self.assertEqual(self._interval(weekly, True), {"Minute": 7})
        self.assertEqual(
            self._interval(sa.DailySchedule(hour=6, minute=0), True), {"Minute": 0}
        )

    def test_every_hour_false_keeps_the_weekly_instant(self) -> None:
        weekly = sa.WeeklySchedule(weekday="Sunday", hour=3, minute=7)
        self.assertEqual(
            self._interval(weekly, False), {"Weekday": 0, "Hour": 3, "Minute": 7}
        )
