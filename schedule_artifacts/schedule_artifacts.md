# schedule_artifacts.py

Shared library (not a CLI) owning one weekly instant and the four artifacts each
platform's scheduler wants derived from it: a Task Scheduler XML task, a systemd
`.service`/`.timer` pair, a launchd LaunchAgent plist, and a crontab line.

## Why it exists

It was written for a weekly maintenance job, called *the sweep* below, and its defaults are
still that job's: `TASK_NAME`, `SYSTEMD_UNIT_BASENAME`, `LAUNCHD_LABEL` and a plain weekly
`--mode run`. Every other caller passes its own label, arguments and schedule, as the
backup's launchd agent does. This repository uses only the launchd half, for that agent:
`DailySchedule`, `build_launchd_plist`, `launchd_plist_path` and `logs_dir` in
`git_workspace_backup/src/backup_launchd.py`, and `parse_daily_time` and `logs_dir` in
`git_workspace_backup/git_workspace_backup.py`.

Everything here is a **pure function of its arguments**: no subprocess, no filesystem, no
config. That is also what makes it testable — every platform's artifact is generated and
asserted from a single machine, including the two that could never run there.

Installing what these functions return stays with the caller, deliberately. Generating
a systemd unit is a total function; `systemctl --user enable` is a host mutation. Keeping
those on opposite sides of a folder boundary is what stops the second from creeping into
the first's tests.

## The one rule every generator here obeys

**The output describes the TARGET, never the host that generated it.** Four independent
instances live in this module, and each was a real defect or a real near-miss:

| Instance | What goes wrong without it |
| --- | --- |
| `PurePosixPath` in the systemd and launchd working directories | Plain `Path` is host-flavored; generating from Windows emitted `\tmp\spaced dir` and real systemd rejected it as "path is not absolute" |
| `PureWindowsPath` in the Task Scheduler working directory | The mirror image: generating from Linux would emit forward slashes into a Windows-only artifact |
| Fixed ASCII day tokens in `_SYSTEMD_DAYS` | A `strftime("%a")` render follows `LC_TIME` and can emit a localized token systemd cannot parse |
| A per-format escape at each builder | systemd doubles `%`, cron backslashes it, plistlib needs neither — one shared escaper would be wrong for two of the three (the full rules: *What the builders refuse and escape*) |

## Public surface

| Name | Meaning |
| --- | --- |
| `WeeklySchedule` | One weekly instant; every notation below is a property derived from it. |
| `parse_schedule(day, time_hhmm)` | The schedule from a CLI's `--day`/`--time`; `None` when the time is malformed. |
| `DAYS` | The weekday names, in the order Task Scheduler spells them — bound a CLI's `--day` to this. |
| `build_task_xml(schedule, python, script, start_date)` | Schema-valid S4U weekly Task Scheduler XML; one call to `render_task_xml`. |
| `render_task_xml(*, task_name, description, triggers, logon_type, command, arguments, working_directory, execution_limit)` | Any least-privilege Task Scheduler task, from a trigger list. |
| `WeeklyAt` · `RepeatEvery` · `AtLogon` · `TaskTrigger` | The trigger types `render_task_xml` renders, and their union. |
| `LOGON_TYPES` | The logon types `render_task_xml` accepts; the constant in `src/sched_task_xml.py` is the list. |
| `build_systemd_units(schedule, python, script, path_value)` | The `.service` then `.timer` text. |
| `build_launchd_plist(schedule, python, script, state_dir, path_value, *, label=LAUNCHD_LABEL, args=("--mode", "run"), run_at_load=False, every_hour=False)` | The LaunchAgent plist, as bytes; *schedule* may be weekly or daily. |
| `build_cron_line(schedule, python, script, path_value)` | The crontab line for an operator to paste. |
| `systemd_unit_dir(home, environ=None)` | Where a systemd USER unit lives, honoring `XDG_CONFIG_HOME`. |
| `launchd_plist_path(home, label=LAUNCHD_LABEL)` | Where the user LaunchAgent plist lives. |
| `DailySchedule` · `parse_daily_time(time_hhmm)` | One instant that fires every day, and its `--time` parser (`None` when malformed). |
| `logs_dir(state_dir)` | Where sweep logs live — one definition, because the launchd registrar must create it long before the sweep writes into it. |
| `select_backend(platform, has_systemd=...)` | Which backend a platform installs into. |
| `requires_elevation(backend)` | True only for Task Scheduler; both POSIX backends are user-scoped. |
| `TASK_NAME` · `SYSTEMD_UNIT_BASENAME` · `LAUNCHD_LABEL` | The keys each scheduler looks a job up BY — literal names only because the schedulers require them. |
| `BACKEND_*` | Stable backend identifier strings (printed to operators, asserted in tests). |
| `RUN_TIME_LIMIT_HOURS` · `RUN_TIME_LIMIT_SECONDS` | One ceiling, two notations; a consumer's own per-call bound derives from the seconds form. |

## Daily schedules and a second LaunchAgent

`DailySchedule(hour, minute)` and `parse_daily_time("HH:MM")` describe an instant that fires
every day. Only the launchd builder renders one: `build_launchd_plist` accepts either schedule
type and omits `Weekday` for a daily one, which launchd reads as every day.

`build_launchd_plist` and `launchd_plist_path` also take `label=`, and the builder takes
`args=` and `run_at_load=`, so a tool other than the sweep can author its own agent. The
defaults, a plain weekly `--mode run` under the sweep's label, are test-pinned byte for byte.

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

`build_task_xml` keeps its signature and its output, which a test pins byte for byte.

## Why cron is emit-only

`BACKEND_CRON_EMIT` is not an install backend. A `.timer` or `.plist` is a per-job file
a caller wholly authors, so installing and removing one touches only what it wrote.
`crontab -` is a read-modify-write of **one shared file** that also holds the operator's
unrelated jobs, so a bug there destroys entries the caller never wrote. That asymmetry — not any
difference in expressiveness — is why cron gets a line to paste.

## What the builders refuse and escape

A path is data, and a scheduler must read it back as exactly that path. So:

- `WeeklySchedule` and `DailySchedule` raise `ValueError` when constructed with a weekday
  outside `DAYS`, an hour outside 0 to 23, a minute outside 0 to 59, or a `bool` hour or
  minute (a `bool` is an `int`, so it passed the range check). The Task Scheduler
  XML uses the weekday as an element *name*, which no XML escape covers, so this check is
  what makes "every text field is XML-escaped" hold.
- `build_systemd_units` and `build_cron_line` raise `ValueError` for a value holding a
  newline, a carriage return or a NUL. No escape keeps one inside a unit-file value or a
  crontab line, and a value carrying one could start a key, or a job, of its own.
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

`tests/test_hostile_values.py` checks the meaning, not the text, with paths holding
`$(id)`, backticks, `"`, `%`, `\`, `$HOME` and spaces, and values ending in `\` or in
whitespace. It reads each unit as INI after joining a line that ends in an unescaped `\` to
the next, as systemd does, so no key can appear that the builder did not write and none it
wrote can be swallowed by the line above. It then decodes each field by a model of
systemd's rules for it, because no systemd runs where the units are generated. Each cron
command goes through a model of cron's `%` rule, then a real `sh`: `-n` to parse it, and
a `printf` of its words, under a `PATH` holding no command at all, to read each one back.
Nothing is ever installed.

## Notes

- Every function is **pure**. No filesystem access, no I/O, no config; consumers own
  their own writing and their own host probes. It therefore carries no `stdio_utf8`
  dependency and cannot fail at import time.
- It ships no `samples/`: there is no input→output transform over a *file*.
- Its suites live in `tests/`, run through the thin aggregator `test_schedule_artifacts.py`,
  which fails when a grouped file loses tests or a new one arrives without a floor.
- A tool that imports it by putting its folder on `sys.path` must also name that folder, and
  its `src/`, in `[tool.mypy] mypy_path`: mypy cannot see a runtime insert, so without the
  entry `import schedule_artifacts` does not resolve when a single folder is type-checked.

## Testing schedule_artifacts itself

```bash
python schedule_artifacts/test_schedule_artifacts.py
```
