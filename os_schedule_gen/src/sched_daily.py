"""The daily instant: one hour and minute, every day.

Private to os_schedule_gen: consumers reach it through the re-export in
``os_schedule_gen.py``.

Held apart from ``WeeklySchedule`` rather than as a day-optional variant of it: every
notation that class derives needs a day, so an optional day would put a branch in each of
them. This class derives its own notations instead, each the scheduler's spelling of
"every day": a ``*-*-*`` date for systemd and a ``*`` day-of-week for cron, while launchd
omits ``Weekday`` and Task Scheduler takes a ``DailyAt`` trigger.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

#: The largest hour and minute a 24-hour clock shows; both start at 0.
_MAX_HOUR = 23
_MAX_MINUTE = 59


def refuse_bad_clock(hour: int, minute: int) -> None:
    """Raise ValueError unless *hour* is 0-23 and *minute* 0-59, neither of them a bool.

    Shared by both schedule types: every notation renders the pair as bare numbers, so an
    out-of-range value would reach a scheduler as a time it rejects or misreads. A bool is
    an int that passes the range check, yet cron's fields would read ``True`` and launchd's
    plist would carry ``<true/>``.
    """
    for name, value in (("hour", hour), ("minute", minute)):
        if isinstance(value, bool):
            raise ValueError(f"{name} must be an int, not a bool: {value!r}")
    if not 0 <= hour <= _MAX_HOUR:
        raise ValueError(f"hour must be 0-{_MAX_HOUR}: {hour!r}")
    if not 0 <= minute <= _MAX_MINUTE:
        raise ValueError(f"minute must be 0-{_MAX_MINUTE}: {minute!r}")


@dataclass(frozen=True)
class DailySchedule:
    """One clock time that fires every day; ValueError when it is not a 24-hour time."""

    hour: int
    minute: int

    def __post_init__(self) -> None:
        """Refuse an hour or minute no scheduler can fire at."""
        refuse_bad_clock(self.hour, self.minute)

    @property
    def hhmm(self) -> str:
        """The time as zero-padded ``HH:MM`` (``StartBoundary``, systemd, messages)."""
        return f"{self.hour:02d}:{self.minute:02d}"

    @property
    def systemd_on_calendar(self) -> str:
        """The ``OnCalendar=`` value: any year, month and day, at :attr:`hhmm`."""
        return f"*-*-* {self.hhmm}"

    @property
    def cron_fields(self) -> str:
        """The five cron time fields: ``minute hour * * *``, plain integers as cron reads."""
        return f"{self.minute} {self.hour} * * *"


def parse_daily_time(time_hhmm: str) -> "DailySchedule | None":
    """The schedule from a ``--time`` value; None when it is not a 24-hour ``HH:MM``.

    None rather than a raised exception, matching ``parse_schedule``: the caller turns it
    into an exit-2 message naming ``--time`` before any host call fires.
    """
    try:
        parsed = datetime.strptime(time_hhmm, "%H:%M")
    except ValueError:
        return None
    return DailySchedule(hour=parsed.hour, minute=parsed.minute)
