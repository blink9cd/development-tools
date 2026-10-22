#!/usr/bin/env python3
"""Render one weekly instant into the artifact each platform's scheduler understands.

Shared library (not a CLI): pure functions only, no host access, no subprocess, no
config. A caller supplies the instant and the two absolute paths; this module hands
back the Task Scheduler XML, the systemd ``.service``/``.timer`` pair, the launchd
plist bytes, or the crontab line -- and nothing here writes any of them. Installing
those artifacts is the caller's job, and deliberately stays there: the four
generators are total functions of their arguments, which is what makes every
platform's form testable from any one machine.

Whose defaults these are
------------------------
The defaults -- ``TASK_NAME``, ``SYSTEMD_UNIT_BASENAME``, ``LAUNCHD_LABEL`` and a plain
weekly ``--mode run`` -- are those of the weekly maintenance job this library was written
for, called "the sweep" below. Every other caller passes its own label, arguments and
triggers, as the backup's launchd agent does.

The one thing every generator here shares
-----------------------------------------
**The output describes the TARGET, never the host that generated it.** Four
independent instances of that rule live below, and each was a real defect or a real
near-miss:

* :class:`PurePosixPath` (never plain ``Path``) in the systemd and launchd
  working-directory derivations -- plain ``Path`` is host-flavored, so generating a
  unit from Windows emitted backslashes and real systemd rejected the path as "not
  absolute".
* :class:`PureWindowsPath` in the Task Scheduler working directory, the mirror image.
* Fixed ASCII day tokens in :data:`_SYSTEMD_DAYS` rather than a ``strftime("%a")``
  render, which follows ``LC_TIME`` and can emit a localized token systemd cannot
  parse.
* A per-format escape at each builder rather than one shared helper applied at
  capture -- systemd doubles ``%``, cron backslashes it, and plistlib needs neither.

The day/time itself is held ONCE, as a :class:`WeeklySchedule`, and every notation is
derived from it; see that class for why four hand-maintained copies is the specific
defect it exists to prevent. A daily instant is a :class:`DailySchedule` instead, which
only the launchd builder renders.
"""

from __future__ import annotations

import os
import plistlib
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

# DailySchedule lives in this library's private src/; it is re-exported so every consumer
# still imports schedule_artifacts alone.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from sched_daily import DailySchedule, parse_daily_time  # noqa: E402,F401 - re-exported
from sched_daily import refuse_bad_clock  # noqa: E402

# The general Task Scheduler renderer and its triggers live in src/ too; build_task_xml
# below is one call to it.
from sched_task_xml import (  # noqa: E402,F401 - re-exported
    LOGON_TYPES,
    AtLogon,
    RepeatEvery,
    TaskTrigger,
    WeeklyAt,
    render_task_xml,
)

# Each format's escape, and the one systemd value no escape can save, live there too.
from sched_escape import (  # noqa: E402
    cron_quote,
    refuse_line_breaks,
    systemd_quote,
    systemd_work_dir,
)

TASK_NAME = r"\GateTreeSweep\WeeklySweep"  # documented Task Scheduler key
# The systemd unit basename and the launchd label: the keys those two schedulers look a
# job up BY, so they are literal names only because those schedulers require one --
# each stated once here, with every filename derived from it. The launchd label is
# deliberately neutral: `local.` rather than a real reverse-DNS org domain, which would
# embed an organization's identifier in shipped source.
SYSTEMD_UNIT_BASENAME = "gate-tree-sweep"
LAUNCHD_LABEL = "local.gate-tree-sweep.weekly"
# Which scheduler a platform installs into. Values are stable strings rather than an
# enum because they are printed in operator-facing messages and asserted in tests.
BACKEND_TASK_SCHEDULER = "task-scheduler"
BACKEND_SYSTEMD = "systemd-user-timer"
BACKEND_LAUNCHD = "launchd-agent"
# Not an install backend: cron is EMIT-ONLY. A .timer or .plist is a per-job file this
# tool wholly authors, so install and remove touch only what we wrote. `crontab -` is a
# read-modify-write of ONE shared file that also holds the operator's unrelated jobs, so
# a bug there destroys entries we never wrote. That asymmetry -- not any difference in
# expressiveness -- is why cron gets a line to paste rather than a fourth installer.
BACKEND_CRON_EMIT = "cron-emit"
# The wall-clock ceiling on one scheduled sweep, held ONCE and rendered into three
# notations: the Windows task's <ExecutionTimeLimit> as an ISO 8601 duration, the systemd
# service's TimeoutStartSec= as seconds, and the caller's per-call subprocess bound.
# Three literals for one policy is how a raised ceiling lands on one platform and not the
# others; the hours form is the pivot because ISO 8601 needs it and seconds derive from
# it exactly.
RUN_TIME_LIMIT_HOURS = 2
RUN_TIME_LIMIT_SECONDS = RUN_TIME_LIMIT_HOURS * 3600
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
    """One weekly instant, and the ONE representation every backend derives from.

    Four schedulers spell the same instant four different ways: Task Scheduler wants an
    English day ELEMENT and an ISO ``StartBoundary``, systemd an abbreviated day NAME,
    launchd and cron an INTEGER -- and those integers count Sunday as 0 where ISO
    weekdays count Monday as 1. Holding the instant once and deriving each notation from
    it is what stops the four from drifting apart; four hand-maintained copies is exactly
    the defect this type exists to prevent, which is why the Windows form derives from it
    too rather than remaining the independent original it started as.
    """

    weekday: str  # a DAYS member -- also Task Scheduler's own English day name
    hour: int
    minute: int

    def __post_init__(self) -> None:
        """Refuse a field no backend renders: the XML uses *weekday* as an element NAME."""
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
        """Day index counting Sunday as 0 -- the convention launchd and cron SHARE.

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

        Plain integers, deliberately NOT zero-padded like :attr:`hhmm`: cron reads
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


def systemd_unit_dir(home: Path, environ: "dict[str, str] | None" = None) -> Path:
    """Where a systemd USER unit lives, honoring ``XDG_CONFIG_HOME``.

    Resolved from the injectable *home* rather than the real ``~`` so tests never write
    into the operator's own unit directory, and from ``XDG_CONFIG_HOME`` when set because
    that is where systemd itself looks -- writing to ``~/.config`` on a box that
    relocated it would install a unit systemd never reads, the quietest possible failure.
    """
    env = os.environ if environ is None else environ
    base = env.get("XDG_CONFIG_HOME")
    root = Path(base) if base else home / ".config"
    return root / "systemd" / "user"


def launchd_plist_path(home: Path, label: str = LAUNCHD_LABEL) -> Path:
    """Where the user LaunchAgent plist lives, under the injectable *home*.

    A user agent (``~/Library/LaunchAgents``) rather than a system daemon
    (``/Library/LaunchDaemons``): it needs no elevation, which is what makes the POSIX
    backends genuinely privilege-free instead of merely skipping the Windows check. The
    cost is stated rather than hidden -- a user agent only fires while its user has a
    session, and the caller's ``register`` prints that. The filename follows *label*, so
    a second agent gets its own plist rather than overwriting the sweep's.
    """
    return home / "Library" / "LaunchAgents" / f"{label}.plist"


def logs_dir(state_dir: Path) -> Path:
    """Where sweep logs live under *state_dir*.

    One definition because three callers need the same directory and one of them --
    the launchd registrar -- must CREATE it long before the sweep that writes into it
    ever runs. A repeated ``"logs"`` literal is how those two locations silently stop
    being the same place.
    """
    return state_dir / "logs"


def build_task_xml(
    schedule: WeeklySchedule, python: str, script: str, start_date: date
) -> str:
    """Build a schema-valid S4U weekly Task Scheduler XML string.

    ``python`` is the interpreter (an absolute python.exe -- never the ``py``
    Store alias); ``script`` is the absolute tool path (quoted+XML-escaped inside
    Arguments so spaces survive). The weekly day is enforced by <DaysOfWeek>; the
    time-of-day comes from StartBoundary (its date only sets "not before").

    Both come from *schedule*, so this form is DERIVED like the three POSIX ones
    rather than the independent original it began as -- see :class:`WeeklySchedule`.
    """
    return render_task_xml(
        task_name=TASK_NAME,
        description="Weekly maintenance sweep (S4U, least privilege).",
        triggers=(WeeklyAt(schedule, start_date),),
        logon_type="S4U",
        command=python,
        arguments=f'"{script}" --mode run',
        # PureWindowsPath, not Path: each generator's output must be a function of its
        # TARGET platform, never of the host it happens to run on. Plain `Path` is
        # host-flavored, so generating this XML from Linux would emit forward slashes
        # into a Windows-only artifact. The POSIX generators take the mirror-image step.
        working_directory=str(PureWindowsPath(script).parent),
        execution_limit=f"PT{RUN_TIME_LIMIT_HOURS}H",
    )


def build_systemd_units(
    schedule: WeeklySchedule, python: str, script: str, path_value: str
) -> "tuple[str, str]":
    r"""The ``.service`` then ``.timer`` text for a systemd USER timer.

    Two files because systemd separates WHAT runs from WHEN: the service is a
    ``oneshot`` (it exits -- it is not a daemon), and the timer is the unit ``enable``
    attaches to ``timers.target``.

    ``Persistent=true`` is the counterpart of the Windows task's
    ``StartWhenAvailable``: a box asleep or powered off at the scheduled instant runs
    the sweep on next boot instead of silently skipping the week.

    Each ``ExecStart=`` and ``Environment=`` value reads back unchanged
    (:func:`sched_escape.systemd_quote`), and one holding a line break raises ValueError.
    The quotes matter alone too: systemd splits ``ExecStart`` on whitespace, so a venv
    under a spaced directory would become two arguments. ``WorkingDirectory=`` cannot be
    quoted, so a *script* whose directory is relative, ends in ``\`` or has surrounding
    whitespace raises ValueError (:func:`sched_escape.systemd_work_dir`); any other one
    reads back unchanged.

    ``WorkingDirectory`` is derived with :class:`PurePosixPath`, NOT plain ``Path``:
    ``Path`` is host-flavored, so generating this unit from a Windows process emitted
    ``\tmp\spaced dir`` and real systemd rejected it as "path is not absolute". Caught by
    the live boundary test and by nothing else -- every faked run seam had accepted it.

    ``Environment=`` carries the register-time PATH the caller captured, because the
    unit would otherwise inherit the user manager's bare distro path. It is OMITTED
    entirely when that capture is empty: an ``Environment="PATH="`` would blank the
    search path outright, which is strictly worse than inheriting a short one.
    """
    refuse_line_breaks(python=python, script=script, path_value=path_value)
    work = systemd_work_dir(script)
    # Ahead of ExecStart so the unit reads in the order it takes effect, and quoted for
    # the same reason the two paths are: systemd splits on whitespace itself.
    environment = (
        f"Environment={systemd_quote('PATH=' + path_value, exec_word=False)}\n"
        if path_value
        else ""
    )
    command = " ".join(systemd_quote(word, exec_word=True) for word in (python, script))
    service = f"""[Unit]
Description=Weekly maintenance sweep

[Service]
Type=oneshot
{environment}ExecStart={command} --mode run
WorkingDirectory={work}
TimeoutStartSec={RUN_TIME_LIMIT_SECONDS}
"""
    timer = f"""[Unit]
Description=Weekly maintenance sweep

[Timer]
OnCalendar={schedule.systemd_on_calendar}
Persistent=true
Unit={SYSTEMD_UNIT_BASENAME}.service

[Install]
WantedBy=timers.target
"""
    return service, timer


def build_launchd_plist(
    schedule: "WeeklySchedule | DailySchedule",
    python: str,
    script: str,
    state_dir: Path,
    path_value: str,
    *,
    label: str = LAUNCHD_LABEL,
    args: "tuple[str, ...]" = ("--mode", "run"),
    run_at_load: bool = False,
    every_hour: bool = False,
) -> bytes:
    """The user LaunchAgent plist, as the bytes to write.

    Built with :mod:`plistlib` rather than a format string. The Windows path hand-rolls
    XML only because Task Scheduler's schema is not a plist; here the stdlib owns the
    serialization, which deletes an entire escaping-bug class -- note the custom
    ``&quot;`` escape map :func:`render_task_xml` needs and this does not.

    ``ProgramArguments`` is a LIST, so a space in either path needs no quoting at all --
    launchd execs the vector directly instead of handing it to a shell.

    ``RunAtLoad`` is false by default, so installing an agent does not start its job
    (arming a systemd timer does not run its service either). The sweep opts in: its
    ``--if-due`` makes a load-time run a no-op unless its slot is due. The two log paths
    make a crashed run diagnosable -- launchd otherwise discards a dead job's output,
    and the sweep's own log is not written if it dies before opening one.

    ``WorkingDirectory`` uses :class:`PurePosixPath` for the same reason as
    :func:`build_systemd_units`: the output must describe the target, not the host that
    generated it.

    ``label``, ``args`` and ``run_at_load`` default to a plain weekly ``--mode run``
    under the sweep's label (its agent before ``--if-due``); a regression test pins those
    bytes. A DailySchedule omits ``Weekday``, which launchd reads as every day.

    ``every_hour`` keeps only the schedule's minute, which fires every hour, for a
    caller that decides for itself whether a run is due: missed calendar firings
    coalesce into one on wake, where a ``StartInterval`` firing during sleep is simply
    missed (``man launchd.plist``). Its default, false, changes no byte of the plist.
    """
    logs = logs_dir(state_dir)
    interval: "dict[str, int]" = {"Hour": schedule.hour, "Minute": schedule.minute}
    if isinstance(schedule, WeeklySchedule):
        interval = {"Weekday": schedule.launchd_weekday, **interval}
    if every_hour:
        interval = {"Minute": schedule.minute}
    plist: "dict[str, object]" = {
        "Label": label,
        "ProgramArguments": [python, script, *args],
        "StartCalendarInterval": interval,
        "WorkingDirectory": str(PurePosixPath(script).parent),
        "StandardOutPath": str(logs / "launchd.out.log"),
        "StandardErrorPath": str(logs / "launchd.err.log"),
        "RunAtLoad": run_at_load,
    }
    # The launchd half of the same problem the systemd Environment= line solves: an
    # agent is handed a minimal environment, not a login shell's. Set only when the
    # capture is non-empty, matching build_systemd_units -- an empty override would
    # blank the search path rather than fall back to the inherited one. No escaping
    # here, and that asymmetry is the point: plistlib owns the serialization, so a
    # space, a percent, or a quote in an entry survives without special-casing.
    if path_value:
        plist["EnvironmentVariables"] = {"PATH": path_value}
    return plistlib.dumps(plist)


def build_cron_line(
    schedule: WeeklySchedule, python: str, script: str, path_value: str
) -> str:
    """The crontab line for an operator to paste (never written by this tool).

    Each value is one double-quoted word (:func:`sched_escape.cron_quote`): cron hands
    the command to ``/bin/sh``, so a space would otherwise split it and a ``$(...)``
    would run. A value holding a line break raises ValueError. See
    :data:`BACKEND_CRON_EMIT` for why this is emitted rather than installed.

    The captured PATH is an INLINE ``VAR=value cmd`` prefix, not a standalone ``PATH=``
    crontab line, which would apply to every job below it, the operator's unrelated ones
    included: the same shared-file hazard that makes this backend emit-only.

    ``%`` gets a BACKSLASH, not doubled as in the systemd unit: per crontab(5) an
    unescaped percent becomes a newline and the rest is fed to the job as stdin. Third
    format, third escape -- hence per-builder escaping. Unlike the systemd and launchd
    branches this one is not live-verified, because the tool never installs it.
    """
    refuse_line_breaks(python=python, script=script, path_value=path_value)
    # The escape is HOISTED out of the f-string deliberately. A backslash inside an
    # f-string *expression* is a SyntaxError before 3.12 -- PEP 701 relaxed it -- so the
    # inline form made this whole module unimportable on 3.11. Measured on Rocky 9
    # (3.11.13): the sweep's `--help` died at import. No check on a 3.12+ interpreter can
    # see this: the construct is erased by the parser, so it survives both an AST scan and
    # ast.parse(feature_version=(3, 11)). Keep it hoisted.
    prefix = f"PATH={cron_quote(path_value)} " if path_value else ""
    command = f"{cron_quote(python)} {cron_quote(script)} --mode run"
    return f"{schedule.cron_fields} {prefix}{command}"


def select_backend(platform: str, *, has_systemd: bool) -> str:
    """Which scheduling backend *platform* installs into.

    A pure predicate taking the platform and the systemd answer, rather than reading
    ``sys.platform`` and probing inside the installer, so every branch is reachable from
    a test on a single machine -- and the reason the POSIX paths are fully covered from
    a Windows box.

    *has_systemd* is consulted only where the platform does not already decide, i.e. on
    the Linux branch; the caller's ``resolve_backend`` is what avoids paying for the
    probe elsewhere.
    """
    if platform == "win32":
        return BACKEND_TASK_SCHEDULER
    if platform == "darwin":
        return BACKEND_LAUNCHD
    return BACKEND_SYSTEMD if has_systemd else BACKEND_CRON_EMIT


def requires_elevation(backend: str) -> bool:
    """True when installing this backend's schedule needs an elevated prompt.

    Only Task Scheduler does. Both POSIX backends install into the invoking user's own
    scope (``systemctl --user``, ``~/Library/LaunchAgents``), so there is no privilege to
    acquire -- the Windows elevation gate is genuinely inapplicable off Windows rather
    than being skipped by an exception.
    """
    return backend == BACKEND_TASK_SCHEDULER
