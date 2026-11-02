# `os_schedule_gen` sample: one weekly and one daily schedule, every target

Two commands, each writing all four targets into its own folder. The weekly one sets
every optional field the artifacts carry: a description, a working directory, a `PATH`, a
launchd log folder, a time limit and a logon type. The daily one sets only the required
options, so its files show each scheduler's defaults: no working directory, an inherited
`PATH`, no launchd log keys. Both fix `--start-date`, which otherwise defaults to today,
so the Task XML is the same on every run. Every path is a made-up one under `/opt/example`
or `/var/tmp`; nothing is installed or run. One command renders every target from the
same values, so the Task XML carries those POSIX paths too: a real Windows task would be
rendered on its own, with `--target task-xml` and Windows paths.

Run it from the tool's own folder, the one holding `os_schedule_gen.py` and this
`samples/` folder, with any Python 3.11 or newer:

```bash
tool="$PWD/os_schedule_gen.py"
samples="$PWD/samples"
scratch="$(mktemp -d)"
mkdir "$scratch/weekly" "$scratch/daily"

python "$tool" --name weekly-backup --day Sunday --time 03:15 \
  --program /usr/bin/python3 --arg /opt/example/backup.py --arg=--mode --arg run \
  --description "Weekly backup (example)" --working-directory /opt/example \
  --path-env /usr/local/bin:/usr/bin:/bin --log-dir /var/tmp/example-logs \
  --time-limit-minutes 45 --logon-type InteractiveToken \
  --start-date 2026-01-04 --output-dir "$scratch/weekly"
python "$tool" --name nightly-report --time 02:30 \
  --program /opt/example/bin/report "--arg=--out=/var/tmp/report dir" \
  --start-date 2026-01-05 --output-dir "$scratch/daily"

diff -r "$samples/weekly" "$scratch/weekly" && diff -r "$samples/daily" "$scratch/daily" \
  && echo "matches the samples"
```

Expected: each command prints the five paths it wrote, in the order `.xml`, `.service`,
`.timer`, `.plist`, `.crontab`, and the last line is `matches the samples`.

What to look at:

- [`weekly/weekly-backup.timer`](weekly/weekly-backup.timer) fires on `Sun 03:15`, and
  [`daily/nightly-report.timer`](daily/nightly-report.timer) on `*-*-* 02:30`, systemd's
  "every day". The crontab lines differ the same way: `15 3 * * 0` against `30 2 * * *`.
- [`weekly/weekly-backup.xml`](weekly/weekly-backup.xml) is UTF-16 with a byte-order
  mark, as `schtasks /XML` expects, so view it with `iconv -f UTF-16 -t UTF-8`. Its trigger
  is a `ScheduleByWeek`; the daily task's is a `ScheduleByDay` with an interval of 1.
- The arguments `--mode` and `run` are written bare everywhere, while the daily sample's
  one argument holds a space, so it is quoted in each format's own way: double quotes for
  systemd and cron, and the Windows command-line form in the Task XML's `Arguments`.
- The daily `.service` has no `WorkingDirectory=` line and no `Environment=` line, and its
  `.plist` has no `WorkingDirectory`, `StandardOutPath` or `EnvironmentVariables` key.

A variation to try: run the weekly command again into the same folder. It exits 1, names
each file that already exists, and writes nothing; add `--force` to overwrite them.

`tests/test_samples.py` runs the two `python` commands above, read from this file, and
compares every file byte for byte.

## Cleanup

```bash
scratch="${scratch:?run the block above first}"
rm -rf "$scratch"
unset scratch tool samples
```
