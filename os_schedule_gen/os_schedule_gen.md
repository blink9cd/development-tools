# os_schedule_gen.py

A multi-OS scheduler file generator: a shared library that owns one weekly or daily
instant and derives from it the four artifacts each platform's scheduler wants: a Task
Scheduler XML task, a systemd `.service`/`.timer` pair, a launchd LaunchAgent plist, and
a crontab line. A small command line (*The command line* below) renders one schedule
into those files for a person to install; the library's consumers import the builders
and never load it.

## Why it exists

It was written for a weekly maintenance job, called *the sweep* below, and its defaults
name that job: `TASK_NAME` is `\Maintenance\WeeklySweep`, `SYSTEMD_UNIT_BASENAME` is
`weekly-sweep`, `LAUNCHD_LABEL` is `local.weekly-sweep`, and the arguments are a plain weekly
`--mode run`. Every other caller passes its own label, arguments, and schedule, as the
backup's launchd agent does. This repository uses only the launchd half, for that agent:
`DailySchedule`, `build_launchd_plist`, `launchd_plist_path`, and `logs_dir` in
`git_workspace_backup/src/backup_launchd.py`, and `parse_daily_time` and `logs_dir` in
`git_workspace_backup/git_workspace_backup.py`.

Everything here is a pure function of its arguments: no subprocess, no filesystem, no
config. That is also what makes it testable: every platform's artifact is generated and
asserted from a single machine, including the two that could never run there.

Installing what these functions return stays with the caller, deliberately. Generating
a systemd unit is a total function; `systemctl --user enable` is a host mutation. Keeping
those on opposite sides of a folder boundary is what stops the second from creeping into
the first's tests.

## The one rule every generator here obeys

The output describes the target, never the host that generated it. Four independent
instances live in this module, and each was a real defect or a real near-miss:

| Instance | What goes wrong without it |
| --- | --- |
| `PurePosixPath` in the systemd and launchd working directories | Plain `Path` is host-flavored; generating from Windows emitted `\tmp\spaced dir` and real systemd rejected it as "path is not absolute" |
| `PureWindowsPath` in the Task Scheduler working directory | The mirror image: generating from Linux would emit forward slashes into a Windows-only artifact |
| Fixed ASCII day tokens in `_SYSTEMD_DAYS` | A `strftime("%a")` render follows `LC_TIME` and can emit a localized token systemd cannot parse |
| A per-format escape at each builder | systemd doubles `%`, cron backslashes it, and plistlib needs neither, so one shared escaper would be wrong for two of the three (the full rules: *What the builders refuse and escape*) |

## Public surface

| Name | Meaning |
| --- | --- |
| `WeeklySchedule` | One weekly instant; every notation below is a property derived from it. Defined in `src/sched_weekly.py`. |
| `parse_schedule(day, time_hhmm)` | The schedule from a CLI's `--day`/`--time`; `None` when the time is malformed. |
| `DAYS` | The weekday names, in the order Task Scheduler spells them; bound a CLI's `--day` to this. |
| `build_task_xml(schedule, python, script, start_date, *, task_name=TASK_NAME, description=..., args=("--mode", "run"), working_directory=None, execution_limit="PT2H", logon_type="S4U")` | Schema-valid least-privilege weekly or daily Task Scheduler XML; one call to `render_task_xml`. |
| `render_task_xml(*, task_name, description, triggers, logon_type, command, arguments, working_directory, execution_limit)` | Any least-privilege Task Scheduler task, from a trigger list. |
| `WeeklyAt` · `DailyAt` · `RepeatEvery` · `AtLogon` · `TaskTrigger` | The trigger types `render_task_xml` renders, and their union. |
| `LOGON_TYPES` | The logon types `render_task_xml` accepts; the constant in `src/sched_task_xml.py` is the list. |
| `build_systemd_units(schedule, python, script, path_value, *, unit_basename=SYSTEMD_UNIT_BASENAME, description=..., args=("--mode", "run"), working_directory=None, time_limit_seconds=RUN_TIME_LIMIT_SECONDS)` | The `.service` then `.timer` text. |
| `build_launchd_plist(schedule, python, script, state_dir, path_value, *, label=LAUNCHD_LABEL, args=("--mode", "run"), run_at_load=False, every_hour=False, working_directory=None, log_paths=None)` | The LaunchAgent plist, as bytes. |
| `build_cron_line(schedule, python, script, path_value, *, args=("--mode", "run"), working_directory=None)` | The crontab line for an operator to paste. |
| `build_parser()` · `main(argv=None)` | The command line's parser, and its entry point returning the exit code. |
| `systemd_unit_dir(home, environ=None)` | Where a systemd user unit lives, honoring `XDG_CONFIG_HOME`. |
| `launchd_plist_path(home, label=LAUNCHD_LABEL)` | Where the user LaunchAgent plist lives. |
| `DailySchedule` · `parse_daily_time(time_hhmm)` | One instant that fires every day, and its `--time` parser (`None` when malformed). |
| `logs_dir(state_dir)` | Where sweep logs live. One definition, because the launchd registrar must create it long before the sweep writes into it. |
| `select_backend(platform, has_systemd=...)` | Which backend a platform installs into. Defined, with `systemd_unit_dir`, `logs_dir`, `requires_elevation` and `BACKEND_*`, in `src/sched_hosts.py`. |
| `requires_elevation(backend)` | True only for Task Scheduler; both POSIX backends are user-scoped. |
| `TASK_NAME` · `SYSTEMD_UNIT_BASENAME` · `LAUNCHD_LABEL` | The keys each scheduler looks a job up by: literal names only because the schedulers require them. |
| `BACKEND_*` | Stable backend identifier strings (printed to operators, asserted in tests). |
| `RUN_TIME_LIMIT_HOURS` · `RUN_TIME_LIMIT_SECONDS` | One ceiling, two notations; a consumer's own per-call bound derives from the seconds form. |

## Daily schedules, and jobs other than the sweep

`DailySchedule(hour, minute)` and `parse_daily_time("HH:MM")` describe an instant that fires
every day. Every builder accepts either schedule type, and each spells "every day" its own
way:

| Builder | Daily form |
| --- | --- |
| `build_systemd_units` | `OnCalendar=*-*-* HH:MM` |
| `build_cron_line` | `m h * * *` |
| `build_task_xml` | a `DailyAt` trigger: a `CalendarTrigger` with `<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>` |
| `build_launchd_plist` | a `StartCalendarInterval` with no `Weekday` |

Each builder's keywords let a tool other than the sweep author its own job: a name (the
task path, the unit basename, the launchd `label`), a description, `args`, a working
directory, logs, a time limit and a logon type, as each format has them. Every keyword
defaults to the sweep's value, and those defaults are test-pinned byte for byte.

*script* may be `None`, for a program run with its `args` alone; the working directory that
would have been derived from the script's folder is then left out, so each scheduler uses
its own default, unless `working_directory` names one. `build_launchd_plist` takes
`state_dir=None` too, and `log_paths=(stdout, stderr)` in place of the two paths derived
from it; with neither, the plist has no log keys. `build_cron_line` enters a
`working_directory` with `cd "<dir>" &&` ahead of the command, so the job does not run if
the directory cannot be entered; the directory must be absolute, because `cd` reads a
leading `-` as an option.

`every_hour=True` keeps only the schedule's minute, so the agent fires at that minute of every
hour. It is for a caller that decides for itself whether a run is due, as the sweep's
`--mode run --if-due` does. It is a calendar trigger rather than `StartInterval` because launchd
coalesces calendar firings missed during sleep into one on wake, while it simply misses an
interval firing that falls during sleep.

## Task Scheduler tasks other than the sweep

`render_task_xml` renders any task from a list of triggers, in the order given:

| Trigger | Renders |
| --- | --- |
| `WeeklyAt(schedule, start_date)` | A `CalendarTrigger` on the schedule's weekday and time, not before `start_date`. |
| `RepeatEvery(minutes, start)` | A `TimeTrigger` from `start`, a naive local time, repeating every `minutes` (1 to 1440). Its `Repetition` has no `Duration`, which Task Scheduler reads as "repeat indefinitely". |
| `AtLogon(user_id)` | A `LogonTrigger` for one `DOMAIN\user`, which the caller supplies at run time. |

`logon_type` is `S4U`, which runs whether or not the user is signed in, or
`InteractiveToken`, which runs only inside the user's signed-in session. The run level is
always least privilege. `arguments` is an already-quoted Windows command line, and
`execution_limit` an ISO 8601 duration such as `PT10M`. Every text field is XML-escaped.

The `Settings` block is shared by every task and is not a parameter. Each task skips a run
that would overlap the one in progress, runs a missed start as soon as it can, and ignores
battery state. Holding those rules once is the reason there is one template rather than one
per caller.

The renderer raises `ValueError` before producing any XML when there are no triggers, when
the logon type is not in `LOGON_TYPES`, or when a repeat interval is outside 1 to 1440 minutes.

`build_task_xml` keeps its positional signature, and with its keywords left at their
defaults its output is unchanged: a test pins the sweep's task byte for byte.

## The command line

It renders one schedule into any of the four targets and writes the files into an existing
directory. It installs nothing: register each file with its own scheduler afterward.

```text
python os_schedule_gen.py --name NAME --time HH:MM --program PATH --output-dir DIR
                             [--day DAY] [--arg WORD]... [--target TARGET]... [--force]
                             [--description TEXT] [--working-directory DIR]
                             [--path-env PATH] [--log-dir DIR]
                             [--time-limit-minutes MINUTES] [--logon-type TYPE]
                             [--start-date YYYY-MM-DD]
```

| Option | Required | Default | Meaning |
| --- | --- | --- | --- |
| `--name` | yes | n/a | `[A-Za-z0-9][A-Za-z0-9._-]*` only: a leading `-` or `.`, and `.` and `..`, are refused. The systemd unit basename, the launchd `Label`, the task `\<name>`, and every file name. |
| `--time HH:MM` | yes | n/a | The 24-hour time it fires. |
| `--day DAY` | no | none: every day | A `DAYS` member. Given, the schedule is weekly; omitted, daily. |
| `--program PATH` | yes | n/a | The executable. |
| `--arg WORD` | no | none | One argument word, repeatable, in order. Write a word starting with `-` as `--arg=--mode`. |
| `--output-dir DIR` | yes | n/a | Must already exist. |
| `--target` | no | all four | `task-xml`, `systemd`, `launchd` or `cron`; repeatable. |
| `--force` | no | off | Overwrite files that already exist. |
| `--description` | no | `Scheduled job <name>` | systemd `Description=` and the task's `Description`. |
| `--working-directory DIR` | no | none | The working directory in every artifact; omitted, none is written and each scheduler uses its own default. Must be absolute, in the POSIX or the Windows sense, for every target; systemd and cron need a POSIX one. |
| `--path-env PATH` | no | none: inherit | The job's `PATH` under systemd, launchd and cron, as the builders' `path_value`. |
| `--log-dir DIR` | no | none | An absolute directory, for every target: launchd's `StandardOutPath` and `StandardErrorPath`, `<DIR>/<name>.out.log` and `.err.log`; omitted, both keys are left out. |
| `--time-limit-minutes` | no | 120 | A whole number from 1 to 525600 (one year): the task's `ExecutionTimeLimit`, written `PT<n>M` as given rather than normalized, and systemd's `TimeoutStartSec`, in seconds. |
| `--logon-type` | no | `S4U` | A `LOGON_TYPES` member; Task XML only. |
| `--start-date` | no | today | `YYYY-MM-DD`: the task's `StartBoundary` date, "not before". |

The files are `<name>.xml` (UTF-16 with a byte-order mark, as `schtasks /XML` expects),
`<name>.service` and `<name>.timer`, `<name>.plist`, and `<name>.crontab` (the one line and
a newline). Stdout lists one path per file written. Every target is rendered, and every
destination checked, before the first file is written, so a refusal or an existing file
leaves the directory as it was. Each file is written through a temporary file and an
atomic rename, created with mode 0644 less the process umask (0600 under `umask 077`). The
umask is applied by the kernel at creation rather than read with `os.umask`, whose
set-and-restore would briefly change it for every thread in the process.

The command line maps onto the builders with *script* `None`: `--program` is the
executable and `--arg` its words, so the working directory is only ever the one
`--working-directory` names.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Every file was written. |
| 1 | A destination already exists and `--force` was not given; nothing was written. |
| 2 | Bad input: an option refused by the parser, a malformed `--time`, a name outside the set, a missing `--output-dir`, a relative `--working-directory` or `--log-dir`, a value holding a line break, or a value a builder refuses (named with its target and option). Also a file that could not be written; the files listed before it were. |

### Examples

```bash
mkdir -p out
python os_schedule_gen/os_schedule_gen.py --name nightly-report \
  --time 02:30 --program /usr/bin/python3 --arg /opt/example/report.py \
  --arg=--mode --arg run --output-dir out
python os_schedule_gen/os_schedule_gen.py --name weekly-report \
  --day Sunday --time 03:15 --program /opt/example/bin/report --target systemd \
  --target cron --working-directory /opt/example --output-dir out
```

The first writes all five files for a daily job; the second a weekly systemd pair and
crontab line only. `samples/README.md` has a run to check by hand against committed output.

## Why cron is emit-only

`BACKEND_CRON_EMIT` is not an install backend. A `.timer` or `.plist` is a per-job file
a caller wholly authors, so installing and removing one touches only what it wrote.
`crontab -` is a read-modify-write of one shared file that also holds the operator's
unrelated jobs, so a bug there destroys entries the caller never wrote. That asymmetry, not any
difference in expressiveness, is why cron gets a line to paste.

## What the builders refuse and escape

A path is data, and a scheduler must read it back as exactly that path. So:

- `WeeklySchedule` and `DailySchedule` raise `ValueError` when constructed with a weekday
  outside `DAYS`, an hour outside 0 to 23, a minute outside 0 to 59, or a `bool` hour or
  minute (a `bool` is an `int`, so it passed the range check). The Task Scheduler
  XML uses the weekday as an element *name*, which no XML escape covers, so this check is
  what makes "every text field is XML-escaped" hold.
- `build_systemd_units`, `build_cron_line` and `build_task_xml` raise `ValueError` for a
  value holding a newline, a carriage return or a NUL. No escape keeps one inside a unit-file value or a
  crontab line, and a value carrying one could start a key, or a job, of its own.
- `build_task_xml` also raises `ValueError`, naming the keyword, for any value holding a
  C0 control character other than tab, or DEL. XML 1.0 cannot carry the C0 controls at
  all, so the file would not parse, and DEL means nothing in any task field. Tab stays: XML
  holds it and the Windows command line reads it as an ordinary character inside quotes.
  Its `task_name` must be a task path: a leading `\`, then `\`-separated segments, each in
  the set a unit name is held to, so a `..` segment, an empty one, or markup is refused.
- **systemd:** each `ExecStart=` word is double-quoted, with `\` and `"` backslash-escaped,
  `%` doubled (a specifier) and `$` doubled (a variable). The `Environment=` value is
  quoted the same way except for `$`, which has no meaning there. `WorkingDirectory=` takes
  the rest of its line as an unquoted path, so it gets only `%` doubled, and a script whose
  directory is relative, ends in `\` or starts or ends in whitespace raises `ValueError`:
  systemd reads a leading `~` as the home directory and a leading `-` as "ignore if
  missing", joins a line ending in `\` to the next, folding the `TimeoutStartSec=` key into
  the path, and strips a value's surrounding whitespace. Every quoted value ends its line
  with a quote, so a trailing `\` there is harmless. The escapes are `src/sched_escape.py`.
- **cron:** each value is one double-quoted `sh` word, with `$`, a backtick, `"` and `\`
  backslash-escaped, and then a backslash before each `%`, which crontab(5) otherwise
  reads as a newline. Not `shlex.quote`: cron keeps a backslash before anything but `%`, so
  a `\%` inside single quotes could not survive it. Double quotes also keep an ordinary
  path's line exactly as it was.

- **Arguments:** `args` are written bare only when `is_bare_word` holds: a non-empty run
  of ASCII letters, digits and `_ . / : = + , @ -`. None of those is whitespace, a quote,
  a backslash, `%`, `$`, `~`, a glob or an `sh` operator, so no format reads any of them
  specially, and that is what keeps the sweep's `--mode run` byte for byte as it was. Any
  other argument takes its format's quoting: systemd's and cron's above, and in the Task
  XML the Windows command-line form (`CommandLineToArgvW`): double-quoted, with a run of
  backslashes doubled before a `"` (which is itself escaped) and at the end. The Task
  XML's script word is always quoted, as it always was, and is now escaped the same way:
  a `"` or a trailing `\` in it used to end the quote early.
- **A `.bat` or `.cmd` program:** it is not run by `CommandLineToArgvW`. `cmd.exe` parses its
  command line instead, and treats `=`, `,` and `;` as delimiters between arguments, so
  `--level=3` arrives in a batch file as two words. The bare-word rule's guarantee that each
  argument arrives as exactly the word given covers programs that split their command line
  with `CommandLineToArgvW`, the Microsoft C runtime's rule, and not batch files.
- **systemd `Description=`:** it gets `%` doubled, and one ending in `\` or starting or
  ending in whitespace raises `ValueError`, as `WorkingDirectory=` does. A
  `unit_basename` outside `[A-Za-z0-9._-]`, or not starting with a letter or digit, raises
  `ValueError`: no escape keeps any other character in a unit name as written, and a
  leading `-` or `.` would read as an option or hide the file.
- **launchd:** the `label` is held to the same set as a unit name, in `build_launchd_plist`
  and `launchd_plist_path` alike: it names the plist file, and a `/` or `..` in it would
  put that file outside `LaunchAgents`.
- **Task Scheduler:** it expands `%NAME%` environment references in an action's command,
  arguments and working directory, and no escape for that exists. A value holding one is
  XML-escaped like any text but reaches the task as written, so the scheduler may expand
  it; no test here can observe that, because no Task Scheduler runs where they run.

`tests/test_hostile_values.py` checks the meaning, not the text, with paths holding
`$(id)`, backticks, `"`, `%`, `\`, `$HOME` and spaces, and values ending in `\` or in
whitespace. It reads each unit as INI after joining a line that ends in an unescaped `\` to
the next, as systemd does, so no key can appear that the builder did not write and none it
wrote can be swallowed by the line above. It then decodes each field by a model of
systemd's rules for it, because no systemd runs where the units are generated. Each cron
command goes through a model of cron's `%` rule, then a real `sh`: `-n` to parse it, and
a `printf` of its words, under a `PATH` holding no command at all, to read each one back.
Nothing is ever installed. `tests/test_hostile_args.py` does the same for arguments in
all three command-line formats, decoding the Task XML's `Arguments` with a model of
`CommandLineToArgvW` that is itself checked against `subprocess.list2cmdline`.

## Notes

- Every library function is pure. No filesystem access, no I/O, no config;
  consumers own their own writing and their own host probes. The command line's half,
  `src/sched_cli.py`, is imported only inside `build_parser` and `main`, so importing the
  library never loads it or the `stdio_utf8` and `help_formatter` it needs, which ship
  beside it at the repository root.
- `samples/` holds one weekly and one daily schedule rendered to every target, with the
  commands that produce them; `tests/test_samples.py` reads those commands out of the
  README and checks every file byte for byte.
- Its suites live in `tests/`, run through the thin aggregator `test_os_schedule_gen.py`,
  which fails when a grouped file loses tests or a new one arrives without a floor.
- A tool that imports it by putting its folder on `sys.path` must also name that folder, and
  its `src/`, in `[tool.mypy] mypy_path`: mypy cannot see a runtime insert, so without the
  entry `import os_schedule_gen` does not resolve when a single folder is type-checked.

## Testing os_schedule_gen itself

```bash
python os_schedule_gen/test_os_schedule_gen.py
```
