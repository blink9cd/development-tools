# live_pid_guard

The cleanup every live grandchild-fixture test owes, in the one shape that is safe against pid
reuse: record both pids, probe for their exit right after the call under test, and on the way out
kill only what was not seen gone.

`bounded_run`'s fixture (`../bounded_run/samples/gc_child.py`, in the `bounded_run` folder
beside this one) starts a grandchild, and neither process exits for an hour. A test proving that
a bound holds asserts elapsed time and a verdict, and both still pass when the tree kill has
regressed, because the bounded drain returns regardless. The processes would then run on for that
hour, one pair per test run, with the suite green. This module is what makes such a test leave
nothing behind.

## You do not run this

This is an imported library, not a command. It has no CLI and no `__main__`. The live test
suites import it:

```python
import live_pid_guard

with live_pid_guard.record_fixture_pids() as pids:
    with self.assertRaises(subprocess.TimeoutExpired):
        bounded_run.run_bounded([sys.executable, str(fixture)], timeout=3.0)
    pids.probe_exits()  # before any assertion
self.assertLess(elapsed, ceiling)
```

What you do run is this folder's own suite, which exercises it:

```text
python live_pid_guard/test_live_pid_guard.py
```

## Why the pid files travel in the environment

A tool under test may launch the fixture as its own subprocess, standing in for a command the
tool runs, and append flags of its own, so that its command line reads
`gc_child.py --path <dir> …`. A fixture reading its pid files from positional arguments would take
those flags for them: it would write its pid into a file named `--path` beside the caller, then
crash writing to `<dir>`, so the wedged process the test models would already have exited while
every assertion still passed. The environment passes unchanged through every layer between the
test and the fixture (the tool, `run_bounded`, a `sh` or `cmd.exe` shim) and occupies no argv
slot.

The variable names are defined here (`CHILD_PID_ENV`, `GRANDCHILD_PID_ENV`) and restated in the
fixture, which runs as a bare script and imports nothing from this module. A drift between the
two cannot pass quietly: every guarded test then fails at its probe, reporting a pid that was
never recorded.

## Why cleanup skips every pid it already saw gone

Once a process has exited and been reaped, its pid is free for the OS to hand to an unrelated
process, and a probe made at cleanup time cannot tell the two apart. The protection is an
observation made earlier: `probe_exits()` polls each pid right after the call, before any
assertion can raise and skip it, and cleanup leaves alone every pid that was seen gone.
`run_bounded` has already reaped the direct child by the time it returns, so that pid is the one a
blind cleanup would poke on every *passing* run.

A block that ends normally without calling `probe_exits()` fails after its cleanup: that
cleanup had to signal pids it never saw, which is the hazard itself, so it is reported rather than
allowed to pass. A block that raises propagates its own exception unchanged. A test that reaps a
recorded process through its own `Popen` handle can say so by setting `child_gone` or
`grandchild_gone` itself, so cleanup leaves that pid alone.

## API

| Name | Kind | Meaning |
| --- | --- | --- |
| `record_fixture_pids()` | context manager → `FixturePids` | Exports fresh pid-file paths in both variables for the block, kills survivors on the way out, restores the environment, and fails a block that never probed. |
| `FixturePids.probe_exits()` | method | Polls both recorded pids for exit, child first. Raises `AssertionError` when a pid was never recorded. |
| `FixturePids.kill_survivors()` | method | Kills each recorded process not seen gone. Called by the context manager. |
| `require_recorded_pid(pid_file)` | `-> int` | The recorded pid, or an `AssertionError` saying the child did not start its grandchild before the bound, or that the record is below 2 (0 would name the runner's own process group, 1 init). |
| `is_running(pid)` | `-> bool` | Whether *pid* is still running, probed without signaling it. |
| `gone_within(pid, deadline_seconds)` | `-> bool` | Whether *pid* stops running before the deadline. |
| `seen_gone(pid_file)` | `-> bool` | The recorded pid is required; whether it exits within `REAP_DEADLINE_SECONDS`. |
| `kill_unless_seen_gone(pid_file, *, seen_gone)` | `-> None` | Kills the recorded pid unless it was seen gone; tolerates no record and a process already gone, and never signals a record below 2. |
| `CHILD_PID_ENV` · `GRANDCHILD_PID_ENV` | `str` | The variables the fixture reads. |
| `REAP_DEADLINE_SECONDS` | `float` | How long a probe polls at most: a killed process is re-parented and reaped asynchronously. A ceiling, spent in full only by a real survivor, since the poll returns the moment the exit is seen. |

## Platform behavior

The probe never signals. On POSIX it is `kill(pid, 0)`, which delivers nothing. That call also
succeeds on a zombie, a process that has exited but whose parent has not yet reaped it, so a killed
child of the probing process itself would read as running until that same process waited on it.
For a child of the caller the probe therefore first asks `waitid` with `WNOWAIT`, which sees the
exit without reaping it, and counts the exit as gone; the owner (a `Popen`, say) still collects the
real status. Any other pid is reaped by its own parent, or by init once re-parented, so
`kill(pid, 0)` stays the answer for it. `os.waitid` reached macOS only in Python 3.13, so below
that, there, a zombie child still reads as running. On Windows that call is no probe at all,
because every signal but the two console events becomes `TerminateProcess`. So a handle opened
for `SYNCHRONIZE` is polled with a zero-timeout `WaitForSingleObject` instead, and an
`OpenProcess` that fails means the pid is gone.

Cleanup kills with SIGKILL on POSIX, because an ignored disposition survives `fork` and `exec`, so a
survivor can inherit an ignored SIGTERM from whatever launched the suite. On Windows
`os.kill(pid, SIGTERM)` is `TerminateProcess` itself, which nothing can ignore.

## Requirements

- Python 3.10+ (standard library only).
