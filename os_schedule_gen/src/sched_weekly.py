"""The weekly instant, held once, and every notation a scheduler wants derived from it.

Private to os_schedule_gen: consumers reach it through the re-export in
``os_schedule_gen.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sched_daily import refuse_bad_clock

#: The weekday names, in the order Task Scheduler spells them. Public because the
#: caller's ``--day`` bounds itself to this tuple with argparse ``choices``, and a
#: second hand-typed list is how that CLI starts accepting a day no backend renders.
DAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
# systemd's OnCalendar day tokens, indexed to match DAYS (Monday first). Fixed ASCII
# abbreviations rather than a strftime("%a") render, which would follow LC_TIME and could
# emit a localized token systemd does not parse.
_SYSTEMD_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class WeeklySchedule:
    """One weekly instant, and the one representation every backend derives from.

    Four schedulers spell the same instant four different ways: Task Scheduler wants an
    English day element and an ISO ``StartBoundary``, systemd an abbreviated day name,
    launchd and cron an integer, and those integers count Sunday as 0 where ISO
    weekdays count Monday as 1. Holding the instant once and deriving each notation from
    it is what stops the four from drifting apart; four hand-maintained copies is exactly
    the defect this type exists to prevent, which is why the Windows form derives from it
    too rather than remaining the independent original it started as.
    """

    weekday: str  # a DAYS member, and also Task Scheduler's own English day name
    hour: int
    minute: int

    def __post_init__(self) -> None:
        """Refuse a field no backend renders: the XML uses *weekday* as an element name."""
        if self.weekday not in DAYS:
            raise ValueError(
                f"weekday must be one of {', '.join(DAYS)}: {self.weekday!r}"
            )
        refuse_bad_clock(self.hour, self.minute)

    @property
    def _day_index(self) -> int:
        """Monday-zero index of *weekday*: the pivot every other form is derived from."""
        return DAYS.index(self.weekday)

    @property
    def _sunday_zero_day(self) -> int:
        """Day index counting Sunday as 0, the convention launchd and cron share.

        Named once because two backends use it. Deriving cron's field from launchd's
        would read as a coincidence between unrelated schedulers, while writing the
        arithmetic twice would let a fix to one silently drift from the other.
        """
        return (self._day_index + 1) % 7

    @property
    def hhmm(self) -> str:
        """Zero-padded 24-hour ``HH:MM`` (Windows ``StartBoundary``, systemd time)."""
        return f"{self.hour:02d}:{self.minute:02d}"

    @property
    def xml_day_element(self) -> str:
        """Task Scheduler's ``<DaysOfWeek>`` child element."""
        return f"<{self.weekday} />"

    @property
    def systemd_day(self) -> str:
        """The abbreviated day token systemd's ``OnCalendar=`` accepts."""
        return _SYSTEMD_DAYS[self._day_index]

    @property
    def systemd_on_calendar(self) -> str:
        """The whole ``OnCalendar=`` value."""
        return f"{self.systemd_day} {self.hhmm}"

    @property
    def launchd_weekday(self) -> int:
        """The ``StartCalendarInterval.Weekday`` launchd wants (Sunday 0 .. Saturday 6)."""
        return self._sunday_zero_day

    @property
    def cron_day_of_week(self) -> int:
        """The day-of-week field cron wants (Sunday 0 .. Saturday 6)."""
        return self._sunday_zero_day

    @property
    def cron_fields(self) -> str:
        """The five cron time fields: ``minute hour * * day-of-week``.

        Plain integers, deliberately not zero-padded like :attr:`hhmm`: cron reads
        these as numbers and a leading zero buys nothing.
        """
        return f"{self.minute} {self.hour} * * {self.cron_day_of_week}"


def parse_schedule(day: str, time_hhmm: str) -> "WeeklySchedule | None":
    """The schedule from the CLI's ``--day``/``--time``; None when *time_hhmm* is invalid.

    The single validation point for the pair, for every backend. None rather than a
    raised exception because the caller turns it into an exit-2 message naming
    ``--time`` before any host call fires; *day* needs no validation here because
    argparse already bounds it to :data:`DAYS` with ``choices``.
    """
    try:
        parsed = datetime.strptime(time_hhmm, "%H:%M")
    except ValueError:
        return None
    return WeeklySchedule(weekday=day, hour=parsed.hour, minute=parsed.minute)
