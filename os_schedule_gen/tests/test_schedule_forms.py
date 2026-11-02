#!/usr/bin/env python3
"""The weekly instant itself, and the two forms derived from it without a unit file.

Grouped with the schedule rather than with the POSIX unit content because these are
about the model: parsing the CLI pair, deriving every notation from one instant, and
choosing which backend a platform gets. The files those choices eventually produce are
``test_posix_units``.
"""

from __future__ import annotations

import plistlib
import unittest
from datetime import date, datetime
from pathlib import Path

import os_schedule_gen as sa
from _fixtures import FAKE_PATH, FAKE_PYTHON, FAKE_SCRIPT, SCHEDULE_FORMS


class ParseScheduleTests(unittest.TestCase):
    """The single validation point for --day/--time."""

    def test_accepts_valid_clock_times(self) -> None:
        self.assertEqual(
            sa.parse_schedule("Sunday", "03:00"),
            sa.WeeklySchedule(weekday="Sunday", hour=3, minute=0),
        )
        self.assertEqual(
            sa.parse_schedule("Saturday", "23:59"),
            sa.WeeklySchedule(weekday="Saturday", hour=23, minute=59),
        )

    def test_rejects_malformed_times(self) -> None:
        # None, not an exception: the caller turns it into exit 2 with a message naming
        # --time, before any host call fires.
        self.assertIsNone(sa.parse_schedule("Sunday", "25:99"))
        self.assertIsNone(sa.parse_schedule("Sunday", "3pm"))
        self.assertIsNone(sa.parse_schedule("Sunday", ""))


class ScheduleValidationTests(unittest.TestCase):
    """Each schedule refuses a field no backend can render, at construction.

    Regression: ``WeeklySchedule("Monday /><Evil", 99, 99)`` was accepted, and the Task
    Scheduler renderer interpolates the weekday as an element name, which no XML escape
    covers, so the doc's "every text field is XML-escaped" held only for valid days.
    """

    def test_a_weekday_outside_days_is_refused(self) -> None:
        for weekday in ("Monday /><Evil", "monday", "Mon", ""):
            with self.subTest(weekday=weekday):
                with self.assertRaises(ValueError):
                    sa.WeeklySchedule(weekday=weekday, hour=3, minute=0)

    def test_an_hour_or_minute_out_of_range_is_refused(self) -> None:
        for hour, minute in ((24, 0), (-1, 0), (0, 60), (0, -1), (99, 99)):
            with self.subTest(hour=hour, minute=minute):
                with self.assertRaises(ValueError):
                    sa.WeeklySchedule(weekday="Monday", hour=hour, minute=minute)
                with self.assertRaises(ValueError):
                    sa.DailySchedule(hour=hour, minute=minute)

    def test_a_bool_hour_or_minute_is_refused(self) -> None:
        # bool is an int, so True passed the range check: cron's fields then read
        # "False True * * 1", and launchd's plist carried <true/> where it wants an integer.
        for hour, minute in ((True, 0), (0, False), (False, True)):
            with self.subTest(hour=hour, minute=minute):
                with self.assertRaises(ValueError):
                    sa.WeeklySchedule(weekday="Monday", hour=hour, minute=minute)
                with self.assertRaises(ValueError):
                    sa.DailySchedule(hour=hour, minute=minute)

    def test_the_range_ends_are_accepted(self) -> None:
        for hour, minute in ((0, 0), (23, 59)):
            with self.subTest(hour=hour, minute=minute):
                self.assertEqual(
                    sa.WeeklySchedule("Sunday", hour, minute).hhmm,
                    f"{hour:02d}:{minute:02d}",
                )
                self.assertEqual(
                    sa.DailySchedule(hour, minute).hhmm, f"{hour:02d}:{minute:02d}"
                )


class ScheduleDerivationTests(unittest.TestCase):
    """Every emitted schedule form derives from one WeeklySchedule.

    Four backends state the same weekly instant in four notations: Task Scheduler's
    ``<DaysOfWeek>``, systemd's ``OnCalendar=``, launchd's
    ``StartCalendarInterval.Weekday``, and cron's day-of-week field. Four
    hand-maintained copies of one schedule would drift apart, so this class is the gate
    on it, including the Windows form, which was the original and must stay a derived
    form rather than a fifth independent copy.

    The two conventions that make an off-by-one easy: systemd's ``OnCalendar`` takes
    an abbreviated name while launchd and cron take an integer, and those integers put
    Sunday at 0 while ISO weekdays put Monday at 1.
    """

    def test_every_weekday_maps_to_the_same_day_in_all_four_forms(self) -> None:
        for weekday, ref, xml_element, systemd_day, dow in SCHEDULE_FORMS:
            with self.subTest(weekday=weekday):
                # Validate the table first against datetime: a wrong reference date
                # would make every assertion below agree about the wrong day.
                self.assertEqual(sa.DAYS[ref.weekday()], weekday)
                self.assertEqual(ref.isoweekday() % 7, dow)

                schedule = sa.WeeklySchedule(weekday=weekday, hour=3, minute=0)
                self.assertEqual(schedule.xml_day_element, xml_element)
                self.assertEqual(schedule.systemd_day, systemd_day)
                self.assertEqual(schedule.launchd_weekday, dow)
                self.assertEqual(schedule.cron_day_of_week, dow)

    def test_the_table_covers_every_day_the_cli_accepts(self) -> None:
        """No weekday --day allows may go unexercised above."""
        self.assertEqual(sorted(row[0] for row in SCHEDULE_FORMS), sorted(sa.DAYS))

    def test_time_of_day_renders_zero_padded_in_every_form(self) -> None:
        schedule = sa.WeeklySchedule(weekday="Sunday", hour=3, minute=5)
        self.assertEqual(schedule.hhmm, "03:05")
        self.assertEqual(schedule.systemd_on_calendar, "Sun 03:05")
        self.assertEqual(schedule.cron_fields, "5 3 * * 0")

    def test_midnight_and_last_minute_survive_the_padding(self) -> None:
        midnight = sa.WeeklySchedule(weekday="Monday", hour=0, minute=0)
        self.assertEqual(midnight.hhmm, "00:00")
        self.assertEqual(midnight.cron_fields, "0 0 * * 1")
        latest = sa.WeeklySchedule(weekday="Saturday", hour=23, minute=59)
        self.assertEqual(latest.hhmm, "23:59")
        self.assertEqual(latest.systemd_on_calendar, "Sat 23:59")

    def test_all_four_backends_schedule_the_same_action(self) -> None:
        """The POSIX schedule runs the same step list as Windows, not a subset.

        Every backend's action is ``<python> <script> --mode run``, so the sweep's whole
        step list follows from the one mode. Asserted directly rather than trusted: a
        backend that scheduled something narrower would pass its own emitter test and fail
        right here.
        """
        schedule = sa.WeeklySchedule(weekday="Sunday", hour=3, minute=0)

        xml = sa.build_task_xml(schedule, FAKE_PYTHON, FAKE_SCRIPT, date(2026, 8, 16))
        self.assertIn(f"<Command>{FAKE_PYTHON}</Command>", xml)
        self.assertIn("--mode run", xml)
        self.assertIn(FAKE_SCRIPT, xml)

        service, _timer = sa.build_systemd_units(
            schedule, FAKE_PYTHON, FAKE_SCRIPT, FAKE_PATH
        )
        # Both paths quoted: systemd splits ExecStart on whitespace itself, so an
        # unquoted path containing a space becomes two arguments and the unit dies.
        self.assertIn(f'ExecStart="{FAKE_PYTHON}" "{FAKE_SCRIPT}" --mode run', service)

        plist = plistlib.loads(
            sa.build_launchd_plist(
                schedule, FAKE_PYTHON, FAKE_SCRIPT, Path("/tmp/state"), FAKE_PATH
            )
        )
        self.assertEqual(
            plist["ProgramArguments"], [FAKE_PYTHON, FAKE_SCRIPT, "--mode", "run"]
        )

        cron = sa.build_cron_line(schedule, FAKE_PYTHON, FAKE_SCRIPT, FAKE_PATH)
        self.assertTrue(
            cron.endswith(f'"{FAKE_PYTHON}" "{FAKE_SCRIPT}" --mode run'), cron
        )
        self.assertTrue(cron.startswith("0 3 * * 0 "), cron)


class TaskXmlTests(unittest.TestCase):
    def test_s4u_weekly_fields(self) -> None:
        xml = sa.build_task_xml(
            sa.WeeklySchedule(weekday="Sunday", hour=3, minute=0),
            r"C:\py\python.exe",
            r"C:\a b\weekly_sweep.py",
            datetime(2026, 7, 21).date(),
        )
        self.assertIn("<LogonType>S4U</LogonType>", xml)
        self.assertIn("<RunLevel>LeastPrivilege</RunLevel>", xml)
        self.assertIn("<StartWhenAvailable>true</StartWhenAvailable>", xml)
        self.assertIn("<DisallowStartIfOnBatteries>false", xml)
        self.assertIn("<Sunday />", xml)
        self.assertIn("<StartBoundary>2026-07-21T03:00:00</StartBoundary>", xml)
        self.assertIn("--mode run", xml)
        self.assertIn("&quot;C:\\a b\\weekly_sweep.py&quot;", xml)  # quoted+escaped
        self.assertIn(r"<Command>C:\py\python.exe</Command>", xml)

    def test_day_element_maps(self) -> None:
        xml = sa.build_task_xml(
            sa.WeeklySchedule(weekday="Wednesday", hour=23, minute=30),
            "p",
            "s",
            datetime(2026, 1, 1).date(),
        )
        self.assertIn("<Wednesday />", xml)
        self.assertIn("<StartBoundary>2026-01-01T23:30:00</StartBoundary>", xml)


class BackendSelectionTests(unittest.TestCase):
    """Which scheduler each platform installs into: every branch, from one machine."""

    def test_windows_and_macos_are_decided_by_the_platform_alone(self) -> None:
        # has_systemd is inert on these two branches; passing both answers proves it.
        for answer in (True, False):
            with self.subTest(has_systemd=answer):
                self.assertEqual(
                    sa.select_backend("win32", has_systemd=answer),
                    sa.BACKEND_TASK_SCHEDULER,
                )
                self.assertEqual(
                    sa.select_backend("darwin", has_systemd=answer),
                    sa.BACKEND_LAUNCHD,
                )

    def test_linux_splits_on_whether_a_user_manager_is_running(self) -> None:
        self.assertEqual(
            sa.select_backend("linux", has_systemd=True), sa.BACKEND_SYSTEMD
        )
        self.assertEqual(
            sa.select_backend("linux", has_systemd=False), sa.BACKEND_CRON_EMIT
        )

    def test_an_unknown_posix_platform_still_gets_a_backend(self) -> None:
        """A BSD is not Windows and not macOS, so it takes the systemd/cron branch.

        Falling through rather than raising is the honest answer: those boxes really do
        run either systemd or cron, and the probe, not a platform allowlist this file
        would have to keep current, is what decides.
        """
        self.assertEqual(
            sa.select_backend("freebsd14", has_systemd=False), sa.BACKEND_CRON_EMIT
        )

    def test_only_the_windows_backend_needs_elevation(self) -> None:
        self.assertTrue(sa.requires_elevation(sa.BACKEND_TASK_SCHEDULER))
        for backend in (
            sa.BACKEND_SYSTEMD,
            sa.BACKEND_LAUNCHD,
            sa.BACKEND_CRON_EMIT,
        ):
            with self.subTest(backend=backend):
                self.assertFalse(sa.requires_elevation(backend))
