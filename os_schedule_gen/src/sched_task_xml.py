"""The general Task Scheduler XML renderer, and the trigger types it renders.

Private to os_schedule_gen: consumers reach it through the re-export in
``os_schedule_gen.py``.

One template serves every Windows caller. The sweep's weekly task is one call to it (see
``build_task_xml``); a caller that runs every few minutes and also at sign-in is another,
with a different trigger list and logon type. Holding the ``Settings`` block once is the
point: its catch-up and no-overlap rules are policy, and two templates would let a fix land
in one and not the other.

The trigger union is a ``typing.Union`` rather than a runtime ``X | Y``, which between
classes needs 3.10, so this file still imports on 3.9.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Union
from xml.sax.saxutils import escape

if TYPE_CHECKING:
    # Type-only: the renderer reads only the schedules' derived notations, so it needs
    # neither class at run time.
    from sched_daily import DailySchedule
    from sched_weekly import WeeklySchedule

#: The principal logon types a caller may request. S4U runs whether or not the user is
#: signed in, with no stored password; InteractiveToken runs only inside the user's own
#: signed-in session, which is what a job needing that session's credentials wants.
LOGON_TYPES = ("S4U", "InteractiveToken")
# Task Scheduler's repetition interval bounds, in minutes: at least one minute, and at
# most one day, past which a daily trigger is the right tool instead.
_MIN_REPEAT_MINUTES = 1
_MAX_REPEAT_MINUTES = 1440


@dataclass(frozen=True)
class WeeklyAt:
    """A ``CalendarTrigger`` firing once a week at *schedule*, not before *start_date*."""

    schedule: WeeklySchedule
    start_date: date


@dataclass(frozen=True)
class DailyAt:
    """A ``CalendarTrigger`` firing every day at *schedule*, not before *start_date*."""

    schedule: DailySchedule
    start_date: date


@dataclass(frozen=True)
class RepeatEvery:
    """A ``TimeTrigger`` from *start* (naive local time), repeating every *minutes*.

    Its ``Repetition`` carries no ``Duration``, which Task Scheduler reads as "repeat
    indefinitely". *minutes* must lie in 1..1440; ``render_task_xml`` refuses otherwise.
    """

    minutes: int
    start: datetime


@dataclass(frozen=True)
class AtLogon:
    r"""A ``LogonTrigger`` for one user, given as ``DOMAIN\user`` by the caller at run time."""

    user_id: str


TaskTrigger = Union[WeeklyAt, DailyAt, RepeatEvery, AtLogon]


def _validate(triggers: Sequence[TaskTrigger], logon_type: str) -> None:
    """Raise ValueError for input Task Scheduler would reject or silently misread."""
    if not triggers:
        raise ValueError("a task needs at least one trigger")
    if logon_type not in LOGON_TYPES:
        raise ValueError(f"logon_type must be one of {LOGON_TYPES}, not {logon_type!r}")
    for trigger in triggers:
        if isinstance(trigger, RepeatEvery) and not (
            _MIN_REPEAT_MINUTES <= trigger.minutes <= _MAX_REPEAT_MINUTES
        ):
            raise ValueError(
                f"repeat minutes must be {_MIN_REPEAT_MINUTES}..{_MAX_REPEAT_MINUTES}, "
                f"not {trigger.minutes}"
            )


def _trigger_xml(trigger: TaskTrigger) -> str:
    """One trigger element, indented to sit inside ``<Triggers>``."""
    if isinstance(trigger, WeeklyAt):
        start = f"{trigger.start_date.isoformat()}T{trigger.schedule.hhmm}:00"
        return f"""    <CalendarTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek>
        <DaysOfWeek>{trigger.schedule.xml_day_element}</DaysOfWeek>
        <WeeksInterval>1</WeeksInterval>
      </ScheduleByWeek>
    </CalendarTrigger>"""
    if isinstance(trigger, DailyAt):
        start = f"{trigger.start_date.isoformat()}T{trigger.schedule.hhmm}:00"
        return f"""    <CalendarTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>"""
    if isinstance(trigger, RepeatEvery):
        start = trigger.start.isoformat(timespec="seconds")
        return f"""    <TimeTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      <Repetition>
        <Interval>PT{trigger.minutes}M</Interval>
      </Repetition>
    </TimeTrigger>"""
    return f"""    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(trigger.user_id)}</UserId>
    </LogonTrigger>"""


def render_task_xml(
    *,
    task_name: str,
    description: str,
    triggers: Sequence[TaskTrigger],
    logon_type: str,
    command: str,
    arguments: str,
    working_directory: "str | None",
    execution_limit: str,
) -> str:
    """Render a Task Scheduler (schema 1.4) task, least privilege, as an XML string.

    *command* is an absolute executable; *arguments* an already-quoted Windows command
    line, XML-escaped here with ``"`` as ``&quot;``; *execution_limit* an ISO 8601
    duration such as ``PT10M``. A *working_directory* of None omits the element, which
    Task Scheduler reads as its own default; an empty *arguments* omits that element too.
    Triggers render in the order given. Raises ValueError for no triggers, an unknown
    *logon_type*, or a repeat interval outside 1..1440.
    """
    _validate(triggers, logon_type)
    trigger_block = "\n".join(_trigger_xml(trigger) for trigger in triggers)
    args = escape(arguments, {'"': "&quot;"})
    # Each optional element carries its own leading newline and indent, so leaving one
    # out leaves no blank line behind.
    args_line = f"\n      <Arguments>{args}</Arguments>" if arguments else ""
    work_line = (
        f"\n      <WorkingDirectory>{escape(working_directory)}</WorkingDirectory>"
        if working_directory is not None
        else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(description)}</Description>
    <URI>{escape(task_name)}</URI>
  </RegistrationInfo>
  <Triggers>
{trigger_block}
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>{logon_type}</LogonType>
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
    <ExecutionTimeLimit>{escape(execution_limit)}</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(command)}</Command>{args_line}{work_line}
    </Exec>
  </Actions>
</Task>"""
