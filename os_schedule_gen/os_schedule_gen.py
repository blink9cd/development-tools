#!/usr/bin/env python3
"""Render one weekly or daily instant into the artifact each platform's scheduler understands.

A multi-OS scheduler file generator: a shared library with a small command-line front
end. The library is pure functions only, no host access, no subprocess, no config: a
caller supplies the instant and the command, and this module hands back the Task
Scheduler XML, the systemd
``.service``/``.timer`` pair, the launchd plist bytes, or the crontab line. Installing
those artifacts is the caller's job, and deliberately stays there: the four
generators are total functions of their arguments, which is what makes every
platform's form testable from any one machine. The command line (:func:`main`) writes
the chosen artifacts into a directory and installs nothing either; its parser is built
here, and the rest of it lives in ``src/sched_cli.py``, loaded only when it runs.

Whose defaults these are
------------------------
The keyword defaults (``TASK_NAME``, ``SYSTEMD_UNIT_BASENAME``, ``LAUNCHD_LABEL``, the
descriptions and a plain weekly ``--mode run``) are those of the weekly maintenance job
this library was first written for, called "the sweep" below, and tests pin them byte for
byte. Every other caller passes its own name, arguments and triggers; the command line
always does.

The one thing every generator here shares
-----------------------------------------
The output describes the target, never the host that generated it. Four
independent instances of that rule live below, and each was a real defect or a real
near-miss:

* :class:`PurePosixPath` (never plain ``Path``) in the systemd and launchd
  working-directory derivations: plain ``Path`` is host-flavored, so generating a
  unit from Windows emitted backslashes and real systemd rejected the path as "not
  absolute".
* :class:`PureWindowsPath` in the Task Scheduler working directory, the mirror image.
* Fixed ASCII day tokens in :data:`_SYSTEMD_DAYS` rather than a ``strftime("%a")``
  render, which follows ``LC_TIME`` and can emit a localized token systemd cannot
  parse.
* A per-format escape at each builder rather than one shared helper applied at
  capture: systemd doubles ``%``, cron backslashes it, and plistlib needs neither.

The day/time itself is held once, as a :class:`WeeklySchedule`, and every notation is
derived from it; see that class for why four hand-maintained copies is the specific
defect it exists to prevent. A daily instant is a :class:`DailySchedule` instead, which
derives its own notations, and every builder accepts either.
"""

from __future__ import annotations

import os
import plistlib
import sys
from datetime import date
from functools import partial
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    import argparse

# DailySchedule lives in this library's private src/; it is re-exported so every consumer
# still imports os_schedule_gen alone.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from sched_daily import DailySchedule, parse_daily_time  # noqa: E402,F401 - re-exported

# WeeklySchedule lives there too, and the host facts an installer needs (which backend,
# which directory) with it.
from sched_hosts import (  # noqa: E402,F401 - re-exported
    BACKEND_CRON_EMIT,
    BACKEND_LAUNCHD,
    BACKEND_SYSTEMD,
    BACKEND_TASK_SCHEDULER,
    logs_dir,
    requires_elevation,
    select_backend,
    systemd_unit_dir,
)
from sched_weekly import DAYS, WeeklySchedule, parse_schedule  # noqa: E402,F401 - re-exported

# The general Task Scheduler renderer and its triggers live in src/ too; build_task_xml
# below is one call to it.
from sched_task_xml import (  # noqa: E402,F401 - re-exported
    LOGON_TYPES,
    AtLogon,
    DailyAt,
    RepeatEvery,
    TaskTrigger,
    WeeklyAt,
    render_task_xml,
)

# Each format's escape, and the one systemd value no escape can save, live there too.
from sched_escape import (  # noqa: E402
    command_words,
    cron_quote,
    cron_word,
    refuse_bad_name,
    refuse_command_breaks,
    refuse_task_controls,
    systemd_directory,
    systemd_exec_word,
    systemd_line_value,
    systemd_quote,
    systemd_work_dir,
    windows_quote,
    windows_word,
)

TASK_NAME = r"\Maintenance\WeeklySweep"  # documented Task Scheduler key
# The systemd unit basename and the launchd label: the keys those two schedulers look a
# job up by, so they are literal names only because those schedulers require one,
# each stated once here, with every filename derived from it. The launchd label is
# deliberately neutral: `local.` rather than a real reverse-DNS org domain, which would
# embed an organization's identifier in shipped source.
SYSTEMD_UNIT_BASENAME = "weekly-sweep"
LAUNCHD_LABEL = "local.weekly-sweep"
# The wall-clock ceiling on one scheduled sweep, held once and rendered into three
# notations: the Windows task's <ExecutionTimeLimit> as an ISO 8601 duration, the systemd
# service's TimeoutStartSec= as seconds, and RUN_TIME_LIMIT_SECONDS for a caller that
# bounds a run it starts itself.
# Three literals for one policy is how a raised ceiling lands on one platform and not the
# others; the hours form is the pivot because ISO 8601 needs it and seconds derive from
# it exactly.
RUN_TIME_LIMIT_HOURS = 2
RUN_TIME_LIMIT_SECONDS = RUN_TIME_LIMIT_HOURS * 3600
# The sweep's own arguments: every builder's default, and the bytes the pins hold.
_SWEEP_ARGS = ("--mode", "run")


def launchd_plist_path(home: Path, label: str = LAUNCHD_LABEL) -> Path:
    """Where the user LaunchAgent plist lives, under the injectable *home*.

    A user agent (``~/Library/LaunchAgents``) rather than a system daemon
    (``/Library/LaunchDaemons``): it needs no elevation, which is what makes the POSIX
    backends genuinely privilege-free instead of merely skipping the Windows check. The
    cost is stated rather than hidden: a user agent only fires while its user has a
    session, and a caller installing one should say so. The filename follows *label*, so
    a second agent gets its own plist rather than overwriting the sweep's. A *label*
    outside the job-name set raises ValueError, so the file cannot land elsewhere.
    """
    refuse_bad_name(label, what="label")
    return home / "Library" / "LaunchAgents" / f"{label}.plist"


def build_task_xml(
    schedule: "WeeklySchedule | DailySchedule",
    python: str,
    script: Optional[str],
    start_date: date,
    *,
    task_name: str = TASK_NAME,
    description: str = "Weekly maintenance sweep (S4U, least privilege).",
    args: "tuple[str, ...]" = _SWEEP_ARGS,
    working_directory: Optional[str] = None,
    execution_limit: str = f"PT{RUN_TIME_LIMIT_HOURS}H",
    logon_type: str = "S4U",
) -> str:
    r"""Build a schema-valid, least-privilege weekly or daily Task Scheduler XML string.

    ``python`` is the executable (for the sweep an absolute python.exe, never the
    ``py`` Store alias); ``script``, when given, is the absolute tool path, always quoted
    in Arguments so spaces survive, and None drops it. Each of *args* is bare or quoted by
    :func:`sched_escape.windows_word`. The weekly day is enforced by <DaysOfWeek>, a daily
    schedule by <ScheduleByDay>; the time-of-day comes from StartBoundary (its date only
    sets "not before"). *working_directory* None means the script's folder, or no element
    when there is no script. A line break or a control character but tab in any value,
    or a *task_name* that is not a ``\Folder\Task`` path, raises ValueError.

    Both come from *schedule*, so this form is derived like the three POSIX ones
    rather than the independent original it began as (see :class:`WeeklySchedule`).
    The keywords default to the sweep's task, which a test pins byte for byte.
    """
    for refuse in (refuse_command_breaks, refuse_task_controls):
        refuse(
            python,
            script,
            args,
            task_name=task_name,
            description=description,
            working_directory=working_directory,
            execution_limit=execution_limit,
        )
    if working_directory is None and script is not None:
        # PureWindowsPath, not Path: each generator's output must be a function of its
        # target platform, never of the host it happens to run on. Plain `Path` is
        # host-flavored, so generating this XML from Linux would emit forward slashes
        # into a Windows-only artifact. The POSIX generators take the mirror-image step.
        working_directory = str(PureWindowsPath(script).parent)
    trigger: TaskTrigger = (
        WeeklyAt(schedule, start_date)
        if isinstance(schedule, WeeklySchedule)
        else DailyAt(schedule, start_date)
    )
    return render_task_xml(
        task_name=task_name,
        description=description,
        triggers=(trigger,),
        logon_type=logon_type,
        command=python,
        arguments=" ".join(command_words((script,), args, windows_quote, windows_word)),
        working_directory=working_directory,
        execution_limit=execution_limit,
    )


def build_systemd_units(
    schedule: "WeeklySchedule | DailySchedule",
    python: str,
    script: Optional[str],
    path_value: str,
    *,
    unit_basename: str = SYSTEMD_UNIT_BASENAME,
    description: str = "Weekly maintenance sweep",
    args: "tuple[str, ...]" = _SWEEP_ARGS,
    working_directory: Optional[str] = None,
    time_limit_seconds: int = RUN_TIME_LIMIT_SECONDS,
) -> "tuple[str, str]":
    r"""The ``.service`` then ``.timer`` text for a systemd user timer.

    Two files because systemd separates what runs from when: the service is a
    ``oneshot`` (it exits; it is not a daemon), and the timer is the unit ``enable``
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

    ``WorkingDirectory`` is derived with :class:`PurePosixPath`, not plain ``Path``:
    ``Path`` is host-flavored, so generating this unit from a Windows process emitted
    ``\tmp\spaced dir`` and real systemd rejected it as "path is not absolute". Caught only
    by a live systemd: every faked run seam had accepted it.

    ``Environment=`` carries the register-time PATH the caller captured, because the
    unit would otherwise inherit the user manager's bare distro path. It is left
    out entirely when that capture is empty: an ``Environment="PATH="`` would blank the
    search path outright, which is strictly worse than inheriting a short one.

    The keywords default to the sweep's units. *script* None drops that word, and with
    it the derived directory; an explicit *working_directory* is held to the same rules.
    *unit_basename* is refused outside ``[A-Za-z0-9._-]``, which no escape could widen,
    and *description* gets ``%`` doubled (systemd expands specifiers in it).
    """
    refuse_command_breaks(
        python,
        script,
        args,
        path_value=path_value,
        description=description,
        working_directory=working_directory,
    )
    refuse_bad_name(unit_basename, what="unit_basename")
    if isinstance(time_limit_seconds, bool) or time_limit_seconds < 1:
        raise ValueError(
            f"time_limit_seconds must be a positive int: {time_limit_seconds!r}"
        )
    if working_directory is not None:
        work: Optional[str] = systemd_directory(
            working_directory, what="working_directory"
        )
    else:
        work = systemd_work_dir(script) if script is not None else None
    title = systemd_line_value(description, what="description")
    # Ahead of ExecStart so the unit reads in the order it takes effect, and quoted for
    # the same reason the two paths are: systemd splits on whitespace itself.
    environment = (
        f"Environment={systemd_quote('PATH=' + path_value, exec_word=False)}\n"
        if path_value
        else ""
    )
    exec_quote = partial(systemd_quote, exec_word=True)
    command = " ".join(
        command_words((python, script), args, exec_quote, systemd_exec_word)
    )
    work_line = f"WorkingDirectory={work}\n" if work is not None else ""
    service = f"""[Unit]
Description={title}

[Service]
Type=oneshot
{environment}ExecStart={command}
{work_line}TimeoutStartSec={time_limit_seconds}
"""
    timer = f"""[Unit]
Description={title}

[Timer]
OnCalendar={schedule.systemd_on_calendar}
Persistent=true
Unit={unit_basename}.service

[Install]
WantedBy=timers.target
"""
    return service, timer


def build_launchd_plist(
    schedule: "WeeklySchedule | DailySchedule",
    python: str,
    script: Optional[str],
    state_dir: Optional[Path],
    path_value: str,
    *,
    label: str = LAUNCHD_LABEL,
    args: "tuple[str, ...]" = _SWEEP_ARGS,
    run_at_load: bool = False,
    every_hour: bool = False,
    working_directory: Optional[str] = None,
    log_paths: "tuple[str, str] | None" = None,
) -> bytes:
    """The user LaunchAgent plist, as the bytes to write.

    Built with :mod:`plistlib` rather than a format string. The Windows path hand-rolls
    XML only because Task Scheduler's schema is not a plist; here the stdlib owns the
    serialization, which deletes an entire escaping-bug class: note the custom
    ``&quot;`` escape map :func:`render_task_xml` needs and this does not.

    ``ProgramArguments`` is a list, so a space in either path needs no quoting at all:
    launchd execs the vector directly instead of handing it to a shell.

    ``RunAtLoad`` is false by default, so installing an agent does not start its job
    (arming a systemd timer does not run its service either). A job that decides for
    itself whether a run is due can opt in, since a load-time run is then a no-op. The
    two log paths make a crashed run diagnosable, since launchd otherwise discards a dead
    job's output, and a job's own log is not written if it dies before opening one.

    ``WorkingDirectory`` uses :class:`PurePosixPath` for the same reason as
    :func:`build_systemd_units`: the output must describe the target, not the host that
    generated it.

    ``label``, ``args`` and ``run_at_load`` default to a plain weekly ``--mode run``
    under the sweep's label; a regression test pins those bytes. A DailySchedule omits
    ``Weekday``, which launchd reads as every day.

    ``every_hour`` keeps only the schedule's minute, which fires every hour, for a
    caller that decides for itself whether a run is due: missed calendar firings
    coalesce into one on wake, where a ``StartInterval`` firing during sleep is simply
    missed (``man launchd.plist``). Its default, false, changes no byte of the plist.

    *script* None drops that word and the directory derived from it; an explicit
    *working_directory* replaces that directory. *log_paths* (stdout, stderr) replaces
    the two log paths derived from *state_dir*, and with neither given both keys are
    left out, so launchd discards the job's output. A bad *label* raises ValueError.
    """
    refuse_bad_name(label, what="label")
    interval: "dict[str, int]" = {"Hour": schedule.hour, "Minute": schedule.minute}
    if isinstance(schedule, WeeklySchedule):
        interval = {"Weekday": schedule.launchd_weekday, **interval}
    if every_hour:
        interval = {"Minute": schedule.minute}
    if working_directory is None and script is not None:
        working_directory = str(PurePosixPath(script).parent)
    if log_paths is None and state_dir is not None:
        logs = logs_dir(state_dir)
        log_paths = (str(logs / "launchd.out.log"), str(logs / "launchd.err.log"))
    plist: "dict[str, object]" = {
        "Label": label,
        "ProgramArguments": [python, *([] if script is None else [script]), *args],
        "StartCalendarInterval": interval,
        "RunAtLoad": run_at_load,
    }
    if working_directory is not None:
        plist["WorkingDirectory"] = working_directory
    if log_paths is not None:
        plist["StandardOutPath"], plist["StandardErrorPath"] = log_paths
    # The launchd half of the same problem the systemd Environment= line solves: an
    # agent is handed a minimal environment, not a login shell's. Set only when the
    # capture is non-empty, matching build_systemd_units: an empty override would
    # blank the search path rather than fall back to the inherited one. No escaping
    # here, and that asymmetry is the point: plistlib owns the serialization, so a
    # space, a percent, or a quote in an entry survives without special-casing. It
    # also sorts the keys, so the order they are set in here changes no byte.
    if path_value:
        plist["EnvironmentVariables"] = {"PATH": path_value}
    return plistlib.dumps(plist)


def build_cron_line(
    schedule: "WeeklySchedule | DailySchedule",
    python: str,
    script: Optional[str],
    path_value: str,
    *,
    args: "tuple[str, ...]" = _SWEEP_ARGS,
    working_directory: Optional[str] = None,
) -> str:
    """The crontab line for an operator to paste (never written by this tool).

    Each value is one double-quoted word (:func:`sched_escape.cron_quote`): cron hands
    the command to ``/bin/sh``, so a space would otherwise split it and a ``$(...)``
    would run. A value holding a line break raises ValueError. See
    :data:`BACKEND_CRON_EMIT` for why this is emitted rather than installed.

    The captured PATH is an inline ``VAR=value cmd`` prefix, not a standalone ``PATH=``
    crontab line, which would apply to every job below it, the operator's unrelated ones
    included: the same shared-file hazard that makes this backend emit-only.

    ``%`` gets a backslash, not doubled as in the systemd unit: per crontab(5) an
    unescaped percent becomes a newline and the rest is fed to the job as stdin. Third
    format, third escape, hence per-builder escaping. It is checked against a model of
    cron's ``%`` rule and a real ``sh``, never a live cron, because nothing here installs it.

    *script* None drops that word; each of *args* is bare or quoted by
    :func:`sched_escape.cron_word`. A *working_directory* is entered with a ``cd`` ahead
    of the command, so a directory that cannot be entered stops the job before it runs.
    """
    refuse_command_breaks(
        python, script, args, path_value=path_value, working_directory=working_directory
    )
    if working_directory is not None and not working_directory.startswith("/"):
        # cd reads a leading "-" as an option, and an absolute path cannot start with one.
        raise ValueError(f"working_directory must be absolute: {working_directory!r}")
    # The escape is hoisted out of the f-string deliberately. A backslash inside an
    # f-string *expression* is a SyntaxError before 3.12 (PEP 701 relaxed it), so the
    # inline form made this whole module unimportable on 3.11, as measured on Rocky 9
    # (3.11.13). No check on a 3.12+ interpreter can see this: the construct is erased by
    # the parser, so it survives both an AST scan and ast.parse(feature_version=(3, 11)).
    prefix = f"PATH={cron_quote(path_value)} " if path_value else ""
    enter = f"cd {cron_quote(working_directory)} && " if working_directory else ""
    command = " ".join(command_words((python, script), args, cron_quote, cron_word))
    return f"{schedule.cron_fields} {enter}{prefix}{command}"


def build_parser() -> "argparse.ArgumentParser":
    """The command line's parser: named options only, each default shown by ``--help``.

    Every option is added here; the help texts are ``sched_cli.HELP``. ``sched_cli`` and
    the two libraries it puts on the path load only here and in :func:`main`, so
    importing this module for its builders never loads them.
    """
    import sched_cli as cli  # first: it puts help_formatter and stdio_utf8 on sys.path

    import help_formatter
    import stdio_utf8

    parser = stdio_utf8.EscapingArgumentParser(
        prog=os.path.basename(sys.argv[0]),
        description=cli.DESCRIPTION,
        epilog=cli.EPILOG,
        formatter_class=help_formatter.RawNoneHidingFormatter,
    )
    text = cli.HELP
    parser.add_argument("--name", required=True, type=cli.job_name, help=text["name"])
    parser.add_argument(
        "--time", required=True, type=cli.clock_time, metavar="HH:MM", help=text["time"]
    )
    parser.add_argument("--day", choices=DAYS, help=text["day"])
    parser.add_argument(
        "--program", required=True, metavar="PATH", help=text["program"]
    )
    parser.add_argument("--arg", action="append", metavar="WORD", help=text["arg"])
    parser.add_argument(
        "--output-dir",
        required=True,
        type=cli.existing_dir,
        metavar="DIR",
        help=text["out"],
    )
    parser.add_argument(
        "--target", action="append", choices=cli.TARGETS, help=text["target"]
    )
    parser.add_argument(
        "--force", action="store_true", default=None, help=text["force"]
    )
    parser.add_argument("--description", help=text["description"])
    parser.add_argument("--working-directory", metavar="DIR", help=text["work"])
    parser.add_argument("--path-env", metavar="PATH", help=text["path"])
    parser.add_argument("--log-dir", metavar="DIR", help=text["logs"])
    parser.add_argument(
        "--time-limit-minutes",
        type=cli.positive_int,
        metavar="MINUTES",
        default=RUN_TIME_LIMIT_HOURS * 60,
        help=text["limit"],
    )
    parser.add_argument(
        "--logon-type", choices=LOGON_TYPES, default=LOGON_TYPES[0], help=text["logon"]
    )
    parser.add_argument(
        "--start-date", type=cli.iso_date, metavar="YYYY-MM-DD", help=text["start"]
    )
    return parser


def main(argv: "Optional[list[str]]" = None) -> int:
    """Command-line entry point; returns the exit code the epilog lists (``sched_cli.main``)."""
    import sched_cli

    return sched_cli.main(argv)


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
