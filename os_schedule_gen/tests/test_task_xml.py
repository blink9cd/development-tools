#!/usr/bin/env python3
"""The general Task Scheduler renderer, and the weekly task it must keep producing.

``render_task_xml`` is the shared form every Windows caller renders through: a weekly
calendar trigger for the sweep, and a repeating trigger plus a logon trigger for a
caller that runs often and must also catch up after a sign-in. The pin below holds the
sweep's weekly task byte for byte, so a change to the general renderer cannot change
what an already-installed machine re-registers.

The structural tests parse the rendered string with the standard-library parser. That
is safe here because the only input is the string the code under test just produced.
"""

from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET
from datetime import date, datetime

import os_schedule_gen as sa

_NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
_FAKE_PYTHON = r"C:\fake\Python314\pythonw.exe"
_FAKE_USER = "FAKEDOMAIN\\fakeuser"
_START = datetime(2026, 10, 14, 9, 30)

# The sweep's weekly task exactly as build_task_xml renders it. Raw string: the Windows
# paths carry literal backslashes.
EXPECTED_WEEKLY_XML = r"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Weekly maintenance sweep (S4U, least privilege).</Description>
    <URI>\Maintenance\WeeklySweep</URI>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-08-16T03:00:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek>
        <DaysOfWeek><Sunday /></DaysOfWeek>
        <WeeksInterval>1</WeeksInterval>
      </ScheduleByWeek>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>S4U</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>C:\fake\py\python.exe</Command>
      <Arguments>&quot;C:\fake\tool\weekly_sweep.py&quot; --mode run</Arguments>
      <WorkingDirectory>C:\fake\tool</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"""


def _render(
    *,
    task_name: str = r"\FakeFolder\FakeTask",
    description: str = "A fake task.",
    triggers: tuple[sa.TaskTrigger, ...] | None = None,
    logon_type: str = "InteractiveToken",
    arguments: str = r'"C:\fake\tool\tool.py" --mode tick',
    execution_limit: str = "PT10M",
) -> str:
    """A valid render_task_xml call, with the fields a test varies as overridable keywords.

    *triggers* defaults to one five-minute repeat, built per call rather than as a default
    value so a missing trigger type fails each test that uses it, not the whole import.
    """
    return sa.render_task_xml(
        task_name=task_name,
        description=description,
        triggers=(
            (sa.RepeatEvery(minutes=5, start=_START),) if triggers is None else triggers
        ),
        logon_type=logon_type,
        command=_FAKE_PYTHON,
        arguments=arguments,
        working_directory=r"C:\fake\tool",
        execution_limit=execution_limit,
    )


def _parse(xml: str) -> ET.Element:
    """The root element; encoded as UTF-16 because the declaration names that encoding."""
    return ET.fromstring(xml.encode("utf-16"))


def _find(root: ET.Element, path: str) -> ET.Element | None:
    """The element at the namespace-free, slash-separated *path*, or None when absent."""
    return root.find("/".join(f"{_NS}{part}" for part in path.split("/")))


def _text(root: ET.Element, path: str) -> str | None:
    """The text of the element at *path*, or None when it is absent."""
    found = _find(root, path)
    return None if found is None else found.text


class BuildTaskXmlPinTests(unittest.TestCase):
    """The sweep's weekly task renders through the general renderer unchanged."""

    def test_build_task_xml_output_is_unchanged(self) -> None:
        xml = sa.build_task_xml(
            sa.WeeklySchedule(weekday="Sunday", hour=3, minute=0),
            r"C:\fake\py\python.exe",
            r"C:\fake\tool\weekly_sweep.py",
            date(2026, 8, 16),
        )
        self.assertEqual(xml, EXPECTED_WEEKLY_XML)


class RenderTaskXmlTriggerTests(unittest.TestCase):
    """Each trigger type renders the element Task Scheduler expects for it."""

    def test_repeat_every_renders_time_trigger_with_indefinite_repetition(
        self,
    ) -> None:
        root = _parse(_render())
        self.assertEqual(
            _text(root, "Triggers/TimeTrigger/Repetition/Interval"), "PT5M"
        )
        # No Duration is what Task Scheduler reads as "repeat indefinitely"; a Duration
        # would silently stop the repetition once that span had passed.
        self.assertIsNone(_find(root, "Triggers/TimeTrigger/Repetition/Duration"))
        self.assertEqual(
            _text(root, "Triggers/TimeTrigger/StartBoundary"), "2026-10-14T09:30:00"
        )

    def test_at_logon_renders_logon_trigger_for_the_user(self) -> None:
        root = _parse(_render(triggers=(sa.AtLogon(user_id=_FAKE_USER),)))
        self.assertEqual(_text(root, "Triggers/LogonTrigger/UserId"), _FAKE_USER)

    def test_two_triggers_render_in_order(self) -> None:
        triggers: tuple[sa.TaskTrigger, ...] = (
            sa.RepeatEvery(minutes=5, start=_START),
            sa.AtLogon(user_id=_FAKE_USER),
        )
        container = _find(_parse(_render(triggers=triggers)), "Triggers")
        assert container is not None
        self.assertEqual(
            [child.tag for child in container],
            [f"{_NS}TimeTrigger", f"{_NS}LogonTrigger"],
        )


class RenderTaskXmlPrincipalAndSettingsTests(unittest.TestCase):
    """The principal and the settings block a caller relies on."""

    def test_interactive_token_logon_type(self) -> None:
        root = _parse(_render(logon_type="InteractiveToken"))
        self.assertEqual(
            _text(root, "Principals/Principal/LogonType"), "InteractiveToken"
        )
        self.assertEqual(_text(root, "Principals/Principal/RunLevel"), "LeastPrivilege")

    def test_settings_keep_the_catch_up_and_no_overlap_rules(self) -> None:
        root = _parse(_render(execution_limit="PT7M"))
        self.assertEqual(_text(root, "Settings/MultipleInstancesPolicy"), "IgnoreNew")
        self.assertEqual(_text(root, "Settings/StartWhenAvailable"), "true")
        self.assertEqual(_text(root, "Settings/DisallowStartIfOnBatteries"), "false")
        self.assertEqual(_text(root, "Settings/StopIfGoingOnBatteries"), "false")
        self.assertEqual(_text(root, "Settings/ExecutionTimeLimit"), "PT7M")

    def test_text_fields_are_xml_escaped(self) -> None:
        hostile = 'a & b < c "d"'
        root = _parse(
            _render(task_name=hostile, description=hostile, arguments=hostile)
        )
        self.assertEqual(_text(root, "RegistrationInfo/URI"), hostile)
        self.assertEqual(_text(root, "RegistrationInfo/Description"), hostile)
        self.assertEqual(_text(root, "Actions/Exec/Arguments"), hostile)


class RenderTaskXmlValidationTests(unittest.TestCase):
    """Bad input is refused before any XML is produced."""

    def test_rejects_no_triggers(self) -> None:
        with self.assertRaises(ValueError):
            _render(triggers=())

    def test_rejects_unknown_logon_type(self) -> None:
        with self.assertRaises(ValueError):
            _render(logon_type="Password")

    def test_rejects_repeat_minutes_out_of_range(self) -> None:
        for minutes in (0, 1441):
            with self.subTest(minutes=minutes), self.assertRaises(ValueError):
                _render(triggers=(sa.RepeatEvery(minutes=minutes, start=_START),))


if __name__ == "__main__":
    unittest.main()
