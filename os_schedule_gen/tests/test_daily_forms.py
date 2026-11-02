#!/usr/bin/env python3
"""A daily instant, rendered by all four builders rather than by launchd alone.

Each scheduler spells "every day" its own way: systemd a ``*-*-*`` date wildcard, cron a
``*`` day-of-week, Task Scheduler a ``ScheduleByDay`` with an interval of one, and launchd
a ``StartCalendarInterval`` with no ``Weekday``. Each form is asserted here against the
scheduler's own spelling, and the weekly forms beside them are left exactly as they were.

The Task XML is parsed with the standard-library parser, which is safe: its only input is
the string the code under test just produced.
"""

from __future__ import annotations

import plistlib
import unittest
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import os_schedule_gen as sa
from _fixtures import FAKE_PATH, FAKE_PYTHON, FAKE_SCRIPT

_NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
_DAILY = sa.DailySchedule(hour=6, minute=5)
_START = date(2026, 1, 5)


class DailyNotationTests(unittest.TestCase):
    """The daily instant derives each scheduler's notation, zero-padded where it is."""

    def test_systemd_reads_a_date_wildcard_as_every_day(self) -> None:
        self.assertEqual(_DAILY.systemd_on_calendar, "*-*-* 06:05")

    def test_cron_reads_a_star_day_of_week_as_every_day(self) -> None:
        self.assertEqual(_DAILY.cron_fields, "5 6 * * *")

    def test_midnight_and_the_last_minute_render(self) -> None:
        self.assertEqual(
            sa.DailySchedule(hour=0, minute=0).systemd_on_calendar, "*-*-* 00:00"
        )
        self.assertEqual(
            sa.DailySchedule(hour=23, minute=59).cron_fields, "59 23 * * *"
        )


class DailyBuilderTests(unittest.TestCase):
    """Every builder accepts a DailySchedule and renders it as every day."""

    def test_the_timer_fires_every_day(self) -> None:
        _service, timer = sa.build_systemd_units(
            _DAILY, FAKE_PYTHON, FAKE_SCRIPT, FAKE_PATH
        )
        self.assertIn("OnCalendar=*-*-* 06:05\n", timer)

    def test_the_cron_line_fires_every_day(self) -> None:
        line = sa.build_cron_line(_DAILY, FAKE_PYTHON, FAKE_SCRIPT, "")
        self.assertTrue(line.startswith("5 6 * * * "), line)

    def test_the_task_triggers_by_day_with_an_interval_of_one(self) -> None:
        xml = sa.build_task_xml(_DAILY, FAKE_PYTHON, FAKE_SCRIPT, _START)
        trigger = ET.fromstring(xml).find(f"{_NS}Triggers/{_NS}CalendarTrigger")
        assert trigger is not None
        self.assertEqual(trigger.findtext(f"{_NS}StartBoundary"), "2026-01-05T06:05:00")
        self.assertEqual(trigger.findtext(f"{_NS}ScheduleByDay/{_NS}DaysInterval"), "1")
        self.assertIsNone(trigger.find(f"{_NS}ScheduleByWeek"))

    def test_the_plist_has_no_weekday(self) -> None:
        plist = plistlib.loads(
            sa.build_launchd_plist(
                _DAILY, FAKE_PYTHON, FAKE_SCRIPT, Path("/var/tmp/state"), FAKE_PATH
            )
        )
        self.assertEqual(plist["StartCalendarInterval"], {"Hour": 6, "Minute": 5})


class DailyTriggerTests(unittest.TestCase):
    """``DailyAt`` renders through the general renderer, beside the other triggers."""

    def test_a_daily_trigger_renders_alongside_a_logon_trigger(self) -> None:
        xml = sa.render_task_xml(
            task_name=r"\FakeFolder\FakeDaily",
            description="A fake daily task.",
            triggers=(sa.DailyAt(_DAILY, _START), sa.AtLogon("FAKEDOMAIN\\fakeuser")),
            logon_type="InteractiveToken",
            command=r"C:\fake\tool.exe",
            arguments="--once",
            working_directory=None,
            execution_limit="PT10M",
        )
        root = ET.fromstring(xml)
        triggers = [child.tag for child in root.find(f"{_NS}Triggers") or ()]
        self.assertEqual(triggers, [f"{_NS}CalendarTrigger", f"{_NS}LogonTrigger"])

    def test_no_working_directory_omits_the_element(self) -> None:
        xml = sa.render_task_xml(
            task_name=r"\FakeFolder\FakeDaily",
            description="A fake daily task.",
            triggers=(sa.DailyAt(_DAILY, _START),),
            logon_type="S4U",
            command=r"C:\fake\tool.exe",
            arguments="",
            working_directory=None,
            execution_limit="PT10M",
        )
        self.assertNotIn("WorkingDirectory", xml)


if __name__ == "__main__":
    unittest.main()
