"""The command line's work: check the options, render the chosen targets, write the files.

Private to os_schedule_gen. The parser itself is built in ``os_schedule_gen.py``; the
argparse ``type=`` converters, the help texts that are not one line, and everything after
parsing live here. The parent imports this module only inside its CLI functions, so a
consumer importing the library for its builders never loads it, nor ``help_formatter`` and
``stdio_utf8``, which this module puts on ``sys.path``.

Entry point assumptions: ``main`` parses with the parent's ``build_parser`` and hands the
namespace to ``run``, which reads the output
directory, writes only inside it, and prints one path per written file on stdout. Every
refusal goes to stderr with the exit code the epilog states.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import secrets
import sys
from datetime import date, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, Dict, List, Optional, Tuple, Union

# The two sibling libraries the parser needs, resolved from this file's own location
# (<root>/os_schedule_gen/src/), never from the working directory.
_UTILITIES = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_UTILITIES / "help_formatter"))
sys.path.insert(0, str(_UTILITIES / "stdio_utf8"))

import os_schedule_gen as sa  # noqa: E402 - the builders this front end renders through
import stdio_utf8  # noqa: E402 - reached by the insert above
from sched_daily import DailySchedule, parse_daily_time  # noqa: E402
from sched_escape import JOB_NAME  # noqa: E402
from sched_weekly import WeeklySchedule  # noqa: E402

#: The artifacts the CLI can write, in the order they are rendered and printed.
TARGETS = ("task-xml", "systemd", "launchd", "cron")
#: The exit codes the epilog states.
EXIT_WRITTEN, EXIT_EXISTS, EXIT_BAD_INPUT = 0, 1, 2
#: Mode each written file is created with: owner read-write, everyone read, as unit files
#: and plists conventionally are. The kernel subtracts the process umask at creation, so
#: under ``umask 077`` the file is 0600 rather than readable by everyone.
_FILE_MODE = 0o644
#: Flags for creating a temporary file: never an existing name or a symlink (O_EXCL), and
#: binary on Windows, whose C runtime would otherwise turn each \n into \r\n.
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
#: Fresh temporary names tried before giving up; a collision means a name was taken.
_TEMPORARY_ATTEMPTS = 100
#: The largest --time-limit-minutes: one year. Beyond it systemd ignores TimeoutStartSec
#: and the Task Scheduler schema refuses the duration, so a larger value is a mistake.
MAX_TIME_LIMIT_MINUTES = 365 * 24 * 60
#: The characters no artifact here can hold inside one value.
_LINE_BREAKS = ("\n", "\r", "\0")
#: Builder keyword names, as a builder's ValueError spells them, and the option each is.
_OPTION_FOR_KEYWORD = (
    ("working_directory", "--working-directory"),
    ("description", "--description"),
    ("path_value", "--path-env"),
    ("python", "--program"),
    ("args", "--arg"),
)
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

# Line breaks written in: the parent's formatter keeps them, as it must for the epilog.
DESCRIPTION = (
    "Render one weekly or daily schedule into the files each platform's scheduler\n"
    "reads (Task Scheduler XML, a systemd .service/.timer pair, a launchd plist and a\n"
    "crontab line) and write them into --output-dir. It installs nothing."
)
EPILOG = """\
Prerequisites: none beyond an existing --output-dir. Run it, then register each file
with its own scheduler yourself (schtasks /Create /XML, systemctl --user enable,
launchctl bootstrap, crontab); this tool never installs or starts anything.

Exit codes:
  0  every file was written; stdout lists one path per line
  1  a destination already exists and --force was not given; nothing was written
  2  bad input (an option refused, a missing --output-dir, a value a format cannot
     hold) or a file could not be written; the reason is on stderr

An argument starting with "-" is given with "=": --arg=--mode.

Task Scheduler expands %%NAME%% environment references in the task's command, its
arguments and its working directory, and offers no escape for them: a literal
%%NAME%% in a Task XML value is replaced when the task runs.

Example (daily at 02:30, every target, into ./out):
  %(prog)s --name nightly-report --time 02:30 --program /usr/bin/python3 \\
    --arg /opt/example/report.py --arg=--mode --arg run --output-dir ./out
"""

#: Each option's ``--help`` text, keyed short; the parent binds them to the options.
HELP = {
    "name": (
        "Job name: the systemd unit, the launchd Label, the task \\<name> and every "
        "file name. Letters, digits, '.', '_' and '-' only, starting with a letter "
        "or digit."
    ),
    "time": "24-hour time of day it fires.",
    "day": "Fire weekly on this day; it fires every day when omitted.",
    "program": "The executable to run.",
    "arg": (
        "One argument word for the program, repeatable, in order. Write a word "
        "starting with '-' as --arg=--mode."
    ),
    "out": "An existing directory the files are written into.",
    "target": "An artifact to write, repeatable; all four when omitted.",
    # The parser gives --force a default of None, not False, so the formatter hides it
    # as it hides every other unset default.
    "force": "Overwrite files that already exist.",
    "description": (
        "systemd Description= and the task's Description; 'Scheduled job <name>' "
        "when omitted."
    ),
    "work": (
        "The working directory in every artifact (cron enters it with cd); each "
        "scheduler's own default when omitted. Must be absolute; systemd and cron "
        "need a POSIX one."
    ),
    "path": "PATH for the job under systemd, launchd and cron; inherited when omitted.",
    "logs": (
        "An absolute directory for launchd's StandardOutPath and StandardErrorPath, "
        "<DIR>/<name>.out.log and .err.log; launchd discards the output when omitted."
    ),
    "limit": (
        "The task's ExecutionTimeLimit (PT<n>M) and systemd's TimeoutStartSec; "
        f"at most {MAX_TIME_LIMIT_MINUTES} (one year)."
    ),
    "logon": "The Task XML logon type.",
    "start": "The task's StartBoundary date, 'not before'; today when omitted.",
}

Schedule = Union[WeeklySchedule, DailySchedule]
Files = List[Tuple[str, bytes]]


def job_name(value: str) -> str:
    """The argparse ``type=`` for ``--name``: :data:`sched_escape.JOB_NAME`'s set."""
    if JOB_NAME.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            f"use letters, digits, '.', '_' and '-' only, starting with a letter or "
            f"digit: {value!r}"
        )
    return value


def clock_time(value: str) -> DailySchedule:
    """The argparse ``type=`` for ``--time``: a 24-hour ``HH:MM``, as a daily instant."""
    schedule = parse_daily_time(value)
    if schedule is None:
        raise argparse.ArgumentTypeError(f"not a 24-hour HH:MM time: {value!r}")
    return schedule


def iso_date(value: str) -> date:
    """The argparse ``type=`` for ``--start-date``: exactly ``YYYY-MM-DD``, a real date."""
    if _ISO_DATE.fullmatch(value) is not None:
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            pass  # the right shape, but no such day: refused below with every other miss
    raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {value!r}")


def positive_int(value: str) -> int:
    """The argparse ``type=`` for ``--time-limit-minutes``: a whole number, 1 to a year."""
    try:
        number = int(value)
    except ValueError:
        number = 0
    if not 1 <= number <= MAX_TIME_LIMIT_MINUTES:
        raise argparse.ArgumentTypeError(
            f"not a whole number from 1 to {MAX_TIME_LIMIT_MINUTES}: {value!r}"
        )
    return number


def existing_dir(value: str) -> Path:
    """The argparse ``type=`` for ``--output-dir``: a directory that already exists."""
    if not Path(value).is_dir():
        raise argparse.ArgumentTypeError(f"not an existing directory: {value!r}")
    return Path(value)


def _prog() -> str:
    """This program's name, from argv[0], for every message it prints."""
    return os.path.basename(sys.argv[0])


def _refuse(message: str) -> int:
    """Print *message* on stderr under the program's name; return the bad-input code."""
    print(f"{_prog()}: {message}", file=sys.stderr)
    return EXIT_BAD_INPUT


def _line_break_refusal(args: argparse.Namespace) -> Optional[str]:
    """The refusal for the first option holding a line break or NUL, or None."""
    values = [
        ("--program", args.program),
        ("--description", args.description),
        ("--working-directory", args.working_directory),
        ("--path-env", args.path_env),
        ("--log-dir", args.log_dir),
    ] + [("--arg", word) for word in args.arg or ()]
    for option, value in values:
        if value is not None and any(char in value for char in _LINE_BREAKS):
            return f"{option} holds a line break or NUL: {value!r}"
    return None


def _relative_dir_refusal(args: argparse.Namespace) -> Optional[str]:
    """The refusal for a relative ``--working-directory`` or ``--log-dir``, or None.

    Checked for every target, not only those whose format refuses one: a relative path
    means whatever directory each scheduler happens to start in. ``--working-directory``
    may be absolute in the POSIX or the Windows sense, since a Task XML may be rendered
    alone; ``--log-dir`` only ever reaches launchd, so it must start with ``/``.
    """
    work = args.working_directory
    if work is not None and not (
        PurePosixPath(work).is_absolute() or PureWindowsPath(work).is_absolute()
    ):
        return f"--working-directory must be an absolute path: {work!r}"
    if args.log_dir is not None and not PurePosixPath(args.log_dir).is_absolute():
        return f"--log-dir must be an absolute path: {args.log_dir!r}"
    return None


def _with_option(message: str) -> str:
    """A builder's ValueError message, its leading keyword name replaced by the option."""
    for keyword, option in _OPTION_FOR_KEYWORD:
        if message.startswith(keyword):
            return f"{option} ({keyword}){message[len(keyword) :]}"
    return message


def _task_xml(schedule: Schedule, args: argparse.Namespace, start: date) -> Files:
    """``<name>.xml``, UTF-16 with its byte-order mark, as ``schtasks /XML`` expects."""
    xml = sa.build_task_xml(
        schedule,
        args.program,
        None,
        start,
        task_name="\\" + args.name,
        description=args.description,
        args=tuple(args.arg or ()),
        working_directory=args.working_directory,
        execution_limit=f"PT{args.time_limit_minutes}M",
        logon_type=args.logon_type,
    )
    return [(f"{args.name}.xml", xml.encode("utf-16"))]


def _systemd(schedule: Schedule, args: argparse.Namespace, start: date) -> Files:
    """``<name>.service`` and ``<name>.timer``."""
    service, timer = sa.build_systemd_units(
        schedule,
        args.program,
        None,
        args.path_env or "",
        unit_basename=args.name,
        description=args.description,
        args=tuple(args.arg or ()),
        working_directory=args.working_directory,
        time_limit_seconds=args.time_limit_minutes * 60,
    )
    return [
        (f"{args.name}.service", service.encode("utf-8")),
        (f"{args.name}.timer", timer.encode("utf-8")),
    ]


def _launchd(schedule: Schedule, args: argparse.Namespace, start: date) -> Files:
    """``<name>.plist``, logging to ``<log-dir>/<name>.{out,err}.log`` when asked."""
    logs = PurePosixPath(args.log_dir) if args.log_dir is not None else None
    plist = sa.build_launchd_plist(
        schedule,
        args.program,
        None,
        None,
        args.path_env or "",
        label=args.name,
        args=tuple(args.arg or ()),
        working_directory=args.working_directory,
        log_paths=(
            (str(logs / f"{args.name}.out.log"), str(logs / f"{args.name}.err.log"))
            if logs is not None
            else None
        ),
    )
    return [(f"{args.name}.plist", plist)]


def _cron(schedule: Schedule, args: argparse.Namespace, start: date) -> Files:
    """``<name>.crontab``: the one line, then a newline, as crontab(5) needs."""
    line = sa.build_cron_line(
        schedule,
        args.program,
        None,
        args.path_env or "",
        args=tuple(args.arg or ()),
        working_directory=args.working_directory,
    )
    return [(f"{args.name}.crontab", (line + "\n").encode("utf-8"))]


_RENDERERS: "Dict[str, Callable[[Schedule, argparse.Namespace, date], Files]]" = {
    "task-xml": _task_xml,
    "systemd": _systemd,
    "launchd": _launchd,
    "cron": _cron,
}


def _render_all(
    schedule: Schedule, args: argparse.Namespace, start: date
) -> "Union[List[Tuple[Path, bytes]], str]":
    """Every chosen target's files under the output dir, or the refusal that stopped one."""
    chosen = [target for target in TARGETS if target in (args.target or TARGETS)]
    files: "List[Tuple[Path, bytes]]" = []
    for target in chosen:
        try:
            rendered = _RENDERERS[target](schedule, args, start)
        except ValueError as exc:
            return f"cannot render the {target} target: {_with_option(str(exc))}"
        files += [(args.output_dir / name, data) for name, data in rendered]
    return files


def _create_temporary(path: Path) -> "Tuple[int, str]":
    """A new, exclusively created file beside *path*, mode 0644 less the umask: (fd, name).

    Not ``tempfile.mkstemp``, which creates 0600 and so needs a chmod to a mode chosen
    without the umask. Reading the umask means ``os.umask`` set-and-restore, which
    briefly changes it for every thread in the process; letting ``os.open`` create the
    file with the wanted mode has the kernel apply the umask instead, with no window.
    """
    for _attempt in range(_TEMPORARY_ATTEMPTS):
        name = str(path.parent / f".{path.name}.{secrets.token_hex(8)}")
        try:
            return os.open(name, _CREATE_FLAGS, _FILE_MODE), name
        except FileExistsError:
            continue  # someone holds that name; draw another
    raise FileExistsError(f"no free temporary name beside {path}")


def _write_atomically(path: Path, data: bytes) -> None:
    """Write *data* to *path* through a temporary file in the same directory.

    ``os.replace`` swaps the whole file in at once, so a scheduler or editor reading an
    overwritten artifact sees the old file or the new one, never half of each. The
    temporary file is removed if anything fails before the swap; one already gone is
    not an error, so the failure that stopped the write is the one reported.
    """
    handle, temporary = _create_temporary(path)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def _write_all(files: "List[Tuple[Path, bytes]]") -> int:
    """Write each file and print its path; exit 2 at the first that cannot be written."""
    for path, data in files:
        try:
            _write_atomically(path, data)
        except OSError as exc:
            return _refuse(f"could not write {path}: {exc}")
        print(path, file=sys.stdout)
    return EXIT_WRITTEN


def run(args: argparse.Namespace, *, today: Callable[[], date] = date.today) -> int:
    """Render and write the files *args* asks for; return the exit code.

    Validate, then render every target, then check every destination, and only then
    write: a refusal or an existing file therefore leaves the output directory exactly
    as it was. *today* is the ``--start-date`` default, injectable for tests.
    """
    refusal = _line_break_refusal(args) or _relative_dir_refusal(args)
    if refusal is not None:
        return _refuse(refusal)
    if args.description is None:
        args.description = f"Scheduled job {args.name}"
    schedule: Schedule = (
        args.time
        if args.day is None
        else WeeklySchedule(args.day, args.time.hour, args.time.minute)
    )
    files = _render_all(schedule, args, args.start_date or today())
    if isinstance(files, str):
        return _refuse(files)
    existing = [path for path, _data in files if os.path.lexists(path)]
    if existing and not args.force:
        for path in existing:
            print(f"{_prog()}: {path} already exists", file=sys.stderr)
        print(
            f"{_prog()}: nothing was written; pass --force to overwrite",
            file=sys.stderr,
        )
        return EXIT_EXISTS
    return _write_all(files)


def main(argv: Optional[List[str]] = None) -> int:
    """Parse *argv* with the parent's parser, then :func:`run`; returns the exit code."""
    # A piped stdout inherits the locale code page on Windows, which cannot encode a
    # non-ASCII path; force UTF-8 before printing anything.
    stdio_utf8.force_utf8_streams(sys.stdout, sys.stderr)
    return run(sa.build_parser().parse_args(argv))
