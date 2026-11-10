# Changelog

Notable changes to this project, newest first.

## 0.2.2 - 2026-11-10

### Changed

- The license is now two files. `LICENSE` holds the GNU AGPL v3 text exactly as the Free
  Software Foundation publishes it, so GitHub and other tools recognize it. `NOTICE` holds
  the copyright line and the "or any later version" grant. The terms are unchanged:
  AGPL-3.0-or-later.

## 0.2.1 - 2026-11-09

### Added

- `bounded_run`'s `run_bounded` takes `inherit_stderr=True`, which gives the child the
  caller's own stderr instead of a pipe, so a long run's progress lines appear as they
  are written rather than when it ends. The result's `stderr` is then `""`.

### Fixed

- Two of `bounded_run`'s tests failed on Windows and now run only where their subject
  does. The check that an unkillable `ps` is abandoned runs on POSIX alone, as its only
  caller does, and the late-tracking live test waits for a grandchild process on POSIX
  alone, since on Windows the child is still suspended at that point.

## 0.2.0 - 2026-11-02

### Added

- `os_schedule_gen` replaces the `schedule_artifacts` library, under the new name. It now
  has a small command line that renders one daily or weekly schedule into the files for
  Task Scheduler, a systemd user timer, a launchd agent, and a crontab line, with a
  `samples/` folder showing each. Its `--help` prints no default beside the on/off
  `--force`. Its default job names are now `\Maintenance\WeeklySweep`, `weekly-sweep`, and
  `local.weekly-sweep`. The backup passes its own label, so its agent is unchanged.
- `os_schedule_gen.build_task_xml`, `build_systemd_units`, and `build_cron_line` now take a
  daily schedule as well as a weekly one, and keyword arguments for the job's arguments and
  working directory. `build_task_xml` also takes a `task_name`, a description, an
  execution limit, and a logon type, and raises `ValueError` for a `task_name` that is not a
  task path: a leading `\`, then `\`-separated segments, each held to the set a systemd unit
  name is held to (letters, digits, `.`, `_`, and `-`, starting with a letter or digit).
  `build_systemd_units` also takes a unit name, a description, and a time limit, and
  raises `ValueError` for a unit name outside that set.
  `build_launchd_plist` also takes a working directory and log paths.
- `stdio_utf8.spell_unsafe` spells each character a terminal would act on (control
  characters, direction marks, embeddings, overrides, isolates, and the line and paragraph
  separators) as its escape.
- `help_formatter` gains `UnsetHidingFormatter` and `RawNoneHidingFormatter`, which leave
  an unset option's default out of `--help`.

### Fixed

- `os_schedule_gen.build_launchd_plist` and `launchd_plist_path` now raise `ValueError` for
  a label outside the set a systemd unit name is held to. A label such as `../../etc/x`
  used to produce a plist path outside `~/Library/LaunchAgents`. The backup was not
  affected: its label is fixed.
- `bounded_run` no longer waits without a bound for an overdue `ps` snapshot to be reaped.
  On a loaded macOS machine `ps` could sit for minutes after being killed, which held up
  the tree kill that asked for the snapshot. It now gives the reap one second and then
  abandons it.
- `pip install '.[test]'` now works: the project declares a build system, and installs its
  metadata and the test extra without trying to package the tool folders.

### Changed

- `git_workspace_backup --help` no longer prints `(default: False)` beside `--if-due` and
  `--dry-run`, nor `(None)` in the help for `--keep`, `--destination`, and `--repo`. When
  either flag is omitted, the parsed value is now `None` rather than `False`, and each mode
  reads it as false.

### Documentation

- The README has an install section that creates a virtual environment and installs the
  test extra, and the test instructions use it.
- The README shows an iCloud Drive `destination`, a command that lists the checkout's
  gitignored paths before the first install so each can go into `extras` or `regenerable`,
  and that `status` exits 2 until the agent is installed and its first run has recorded a
  result.
- Comments, docstrings, the guides, and the vendored libraries' documents were reworded for
  readers new to the project, and the test fixtures use neutral folder names.
- `pyproject.toml` no longer carries configuration for checks that do not ship with this
  repository.

## 0.1.0 - 2026-10-22

- Initial release.
