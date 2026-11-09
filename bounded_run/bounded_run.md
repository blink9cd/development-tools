# `bounded_run`: a captured subprocess bound that actually holds

Shared library for running a child process with a timeout that survives the child spawning
children of its own. Imported by the tools beside it; it has no CLI.

The bound is on time, and only on time: it is measured on `time.monotonic()`, or on `clock=`
when given. Nothing bounds the size of the output. Everything the child writes to stdout and
stderr is read into this process's memory, and no option caps it. It is handed back whole in the
`CompletedProcess`, or in the `TimeoutExpired` an expiry raises; only a failed tree kill drops it
(property 3), and by then it has already been held. A child that writes a gigabyte well inside its
time costs this process more than a gigabyte, and nothing here stops it. *Measured behavior* below
gives the multiple.

## Why it exists

`subprocess.run(..., capture_output=True, timeout=N)` reads like a bound on the whole call. It is
not. On `TimeoutExpired` CPython on Windows kills only the direct child, then goes back to reading
the captured pipes to assemble the exception's `output`, and a surviving *grandchild* that
inherited those handles keeps them open, so that read blocks indefinitely. On POSIX the same call
returns near the bound but leaves the grandchild running.

The failure mode is inverted from the intuition: the bound is reliable for a leaf child and
unreliable for exactly the nested case you added it for.

This module inverts the order: on expiry it kills the whole process tree first, and only then
touches the streams.

## Importing it

Sibling tool folders resolve it from their own location, so nothing depends on where the tree
lives:

```python
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bounded_run"))

import bounded_run  # noqa: E402 - import follows the sys.path bootstrap above
```

It makes the same insert for its own import of `stdio_utf8`, from the folder beside its own
(`<parent>/stdio_utf8`), at the front of `sys.path`. So a caller that puts the libraries on
the path itself, from a list of folders, gets the `stdio_utf8` it named only while the two
folders share a parent: keep them side by side.

## API

### `run_bounded(argv, *, timeout, cwd=None, env=None, stdin_text=None, kill_tree_fn=kill_tree, clock=None, hide_window=False, inherit_stderr=False)`

Runs `argv` capturing text output; returns `subprocess.CompletedProcess[str]`. `argv` is a
sequence of arguments: a `str` or `bytes` raises `TypeError` before anything is spawned, because
listing it would make each character an argument, so `"ls -l"` would run a program named `l`.

Bakes in the four kwargs a capturing call would otherwise retype at every call site
(`capture_output=True`, `encoding=stdio_utf8.ENCODING`, `errors=stdio_utf8.CAPTURE_ERRORS`,
`check=False`), so the stream policy `stdio_utf8` names has one implementation instead of one
convention per site.

`stdin_text` feeds the child on stdin; a pipe is opened only when it is given, and a caller that
passes nothing gives the child `DEVNULL`, an immediate EOF. That is deliberately not inheritance:
the run captures both output streams, so the child cannot be interactive anyway, and a child that
reads stdin would otherwise eat this process's own. Nor is it a pipe nobody writes to, which would
block where an EOF does not.

`clock`, when given, measures `timeout` on that clock instead of `time.monotonic()`. See
*Measuring the bound on a clock that counts sleep* below.

`hide_window=True` starts the child with no console window on Windows (`CREATE_NO_WINDOW`, added
to the `CREATE_SUSPENDED` every child gets there), and is ignored elsewhere. It exists for a
caller running under `pythonw.exe`, which has no console: Windows gives a console-subsystem child
of such a parent a new, visible console of its own. It is off by default, because a child of a
console parent shares that console and should keep doing so. The flags are computed by
`src/boundedrun_spawn_flags.py`.

`inherit_stderr=True` gives the child this process's own stderr instead of a pipe, and the
result's `stderr` is then `""`. It exists for a long child run for its stdout alone, such as a
sweep whose report the caller parses: its progress lines reach the operator as they are written,
instead of waiting in the pipe until it ends. Off by default, because most callers read the
child's stderr, and a child's stray output on the caller's own stream is noise to the rest.
The bytes sibling has no such option, because no caller needs one yet.

Two consequences a caller owns. The child's stderr reaches the terminal raw, escape
sequences included, with none of the sanitizing a caller applies to captured output, so
pass it only for a child whose output you trust. And a parent with no stderr of its own,
such as one run by `pythonw.exe`, has none to hand on, so the child's writes there go
nowhere.

### `run_bounded_bytes(argv, *, timeout, cwd=None, env=None, stdin_bytes=None, kill_tree_fn=kill_tree, clock=None, hide_window=False)`

Binary-mode sibling; returns `subprocess.CompletedProcess[bytes]`. Pass `stdin_bytes` to feed the
child's stdin; without it the child gets `DEVNULL`, as above, and `argv`, `clock` and
`hide_window` mean what they do there.
Kept a separate named function rather than a `text=` switch so each has one reason to exist and
mypy needs no `@overload`.

### `sleep_inclusive_clock()`

Seconds on a clock that keeps counting while the machine sleeps, for `clock=`. See *Measuring the
bound on a clock that counts sleep* below for which clock each platform reads.

### `kill_tree(pid)`

Kills `pid` and every descendant. A child this module started is killed, while its run lasts,
through its job object on Windows: `TerminateJobObject`, which reaches every descendant whether
or not `pid` itself is still alive (property 5). Any other pid gets the platform's own tree
kill: `taskkill /F /T /PID` on Windows, which walks parent-pid links and so reaches only
descendants still linked to a live `pid`. Elsewhere `pid` must lead its own process group, which
every child this module starts does (property 4 says why), and the kill freezes that group and
every group reachable from it before killing them all (property 6). A `pid` below 2 raises
`ValueError` on every platform with nothing signaled: `killpg(0)` would address the caller's own
group, and 1 is init's. On POSIX so does the caller's own group, and a `pid` that the first
process-table snapshot, read before any signal, lists in a group it does not lead: the walk would
collect that whole group, its leader included. Exposed and injectable via
`kill_tree_fn` so tests can drive the expiry branches without spawning a real process or waiting
out a real timeout.

### `kill_live_trees()`

Kills the tree of every child a `run_bounded` or `run_bounded_bytes` call has started and not
yet finished, through `kill_tree`, and returns how many kills completed. It is for a caller's
own signal handler: property 8 says why such a caller needs it. A kill that raises `OSError`,
or the `ValueError` of a root the tree kill refuses with nothing sent, is skipped and not
counted, so one tree that cannot be killed does not spare the rest. With nothing running it
kills nothing and returns 0. The registry behind it is `src/boundedrun_live.py`.

### `live_pids()`

The pids `kill_live_trees` would kill right now, as a tuple in no particular order; empty with
nothing running. Read-only. It is how a caller learns that a child it started has been
registered, which happens on the line after `Popen` returns (property 8): a test that signals
its own process to prove the stop reaches a child polls this for the child's pid first, since
anything the child itself writes can be seen before then.

### Constants

| Constant | Value | Why |
| --- | --- | --- |
| `KILL_GRACE_SECONDS` | 5.0 | Time allowed for the pipes to drain *after* the tree kill. Generous against the 0.94s measured `taskkill` + immediate drain: a safety net, not a tuning knob. Exceeding it means the kill did not work, which is reported rather than waited out. |
| `KILL_TIMEOUT_SECONDS` | 10.0 | Bound on the tree kill itself (the `taskkill` fallback on Windows, the freeze-and-walk on POSIX), so a kill that cannot complete does not become the new hang. |

## The contract

The properties callers depend on:

1. **It raises `subprocess.TimeoutExpired`, carrying partial output**, the same shape
   `subprocess.run` raises. Every existing `except subprocess.TimeoutExpired` handler keeps
   working unchanged, including the ones that bind it and read `exc.output` / `exc.stderr`.
2. **The kill precedes the stream read.** That inversion *is* the fix, and it is asserted directly:
   the fake records the call sequence, so an implementation that re-read first would fail.
3. **A failed tree kill fails honestly rather than blocking.** If the post-kill read also expires,
   the partial output is abandoned and the exception raised without it. A bound that always returns
   beats one that sometimes hangs.
4. **The POSIX kill reaches the tree even after the direct child has exited.** Every child starts
   with `start_new_session=True`, so its process-group id *is* its pid, and `kill_tree` signals
   `killpg(pid)` with no lookup. It must never go through `os.getpgid`: macOS refuses that lookup
   (`ESRCH`) for a child that has exited but is not yet reaped, which is exactly when a grandchild
   can still hold the pipes. Through the lookup, that case raised `ProcessLookupError` in place of
   `TimeoutExpired`, killed nothing, and left the grandchild running. `ESRCH` and `EPERM` are
   tolerated: macOS reports `ESRCH` once the group is gone and `EPERM` while the unreaped leader is
   its only member. `EPERM` also means some member could not be signaled (one running under
   another uid, say); those members are left running, and the caller gets property 3's output-less
   `TimeoutExpired` once the bounded re-read expires. Linux answers `getpgid` for an unreaped
   child, which is why a run under Linux never showed the defect.
5. **The Windows kill is built to reach the tree after the direct child has exited, by design but
   not yet by measurement.** `taskkill /T` rebuilds the tree from parent-pid links at the moment
   of the kill, so it cannot start from a leader that has exited, and it misses the orphans of any
   intermediate process that exited. So every child is created with `CREATE_SUSPENDED`, assigned
   to a job object of its own, and only then resumed; every descendant is born inside the job, and
   `kill_tree` terminates the job. A failure anywhere in that sequence kills the still-suspended
   child and raises the `OSError` Windows reported, because a suspended child would otherwise sit
   out the whole timeout and be reported as a hang. A member the job kill may not terminate
   (`ERROR_ACCESS_DENIED`, surfacing as `PermissionError`) is tolerated exactly as property 4
   tolerates `EPERM`. *Proving the Windows branch* says what has and has not run.
6. **The POSIX kill reaches the tree of a nested run.** A descendant that itself calls
   `run_bounded` starts its child in a new session, which no `killpg` of this child's group
   reaches. Orchestrators nest exactly so: one scheduled job of nested tools was seen with five
   nested groups under the one its kill addressed, and after a deadline kill of its outer tool, a
   nested run that tool had started kept running for 35 minutes. So the kill repeats: snapshot the
   process table with `ps`, collect every group reachable from the child's through a parent pid or
   a shared group, and `SIGSTOP` them all, until a snapshot finds no new group and shows every
   collected process stopped or a zombie. Only then does it `SIGKILL` each group. The first
   snapshot is read before any signal, so a root that does not lead its own group is refused with
   nothing sent (the `kill_tree` entry above). It freezes before killing because parent-pid links
   are the only way down, and they break the moment a parent dies; a stopped process can neither
   die nor fork, so a fork racing the freeze costs one more round rather than an escape. The
   caller's own group is never stopped. When `ps` cannot be read (missing, exiting non-zero, as a
   BusyBox `ps` refusing `-A` does, or past `KILL_TIMEOUT_SECONDS`), the groups collected so far
   are killed as they stand, which is at least the child's own: the kill this replaced. An overdue
   `ps` is killed and given one second to be reaped, then abandoned unreaped: under heavy load
   macOS `ps -A` was measured sitting for minutes in an uninterruptible kernel wait, where
   `SIGKILL` cannot end it, and an unbounded reap there hung the stop handler that called the
   kill. Still out of reach: a descendant whose own parent exited before the kill and which left
   that parent's group, since no link of either kind leads to it; a member under another uid
   (property 4's `EPERM`); and, should the killer itself die between the freeze and the kill, the
   tree is left stopped rather than running. Windows needs none of this: a nested run's job nests
   inside the outer one (Windows 8 and later), and `TerminateJobObject` ends every child job's
   processes too. That is Microsoft's documented behavior, not a measurement here.
7. **Ctrl-C kills the child's tree, then propagates.** Because every child leads a session of
   its own, the terminal's `SIGINT` reaches only the caller, never the child. So a
   `KeyboardInterrupt` that arrives while the run waits is caught (that exception only; a
   `SystemExit` is not an interruption of the child), the tree is killed exactly as on expiry,
   the child is reaped under `KILL_GRACE_SECONDS`, and the same `KeyboardInterrupt` is
   re-raised. Before this, an interactive tool that said "stopped" on Ctrl-C left its child
   running unattended, still able to finish what it was doing. A reap that expires means the
   kill did not work; the interrupt is still what the caller sees. The kill happens once: this
   path marks the interrupt handled once its kill and reap have run, and property 8's guard
   further out skips a handled one, so the bound stays one `KILL_GRACE_SECONDS` even when the
   reap expired. `tests/test_drain_interrupt.py` pins that a kill that does not work is called
   once, and that a working kill is called once, with the child's pid, by either entry point.
8. **A caller told to stop can reach every running child.** The new session that keeps a child
   out of the terminal's `SIGINT` keeps it out of every signal sent to the caller: a logout's
   `SIGTERM`, or a scheduler stopping a job, ends the caller and leaves its children running,
   with any lock the caller held now free. So each child's pid is registered on the line right
   after `Popen` returns, with nothing between, and removed when its drain ends, however it ends;
   `kill_live_trees` kills each registered tree. A caller's handler that then raises a
   `KeyboardInterrupt` subclass also gets property 7's kill and reap of the child it interrupted,
   wherever after `Popen` returns the interrupt lands (below). A handler can run after a drain
   has reaped its child but before the pid is removed; the kill then finds a group with no
   leader, kills any member left in it, and otherwise finds nothing. Threads running children
   concurrently share the registry safely: each update is a single set operation, and the kill
   walks a snapshot.

   The spawn-to-track window, and what remains of it: `Popen` and the registration run in
   one frame, on consecutive statements, so no frame hands the new object back to a caller first.
   A frame return is where a debugger's or a pure-Python coverage tracer's code runs, and with it
   any pending signal's handler. A handler that runs once `Popen` has handed the object back, at
   the registering call itself or at a tracer's first event after `Popen`, finds the child not yet
   tracked, so its `kill_live_trees` misses it. The `KeyboardInterrupt` (or subclass) it then
   raises is caught there: the new child's tree is killed through `kill_tree_fn`, the child is
   reaped under `KILL_GRACE_SECONDS` and forgotten, and the same interrupt is re-raised. Should
   `kill_tree_fn` itself raise there, its exception replaces the interrupt, which it keeps as
   `__context__`, exactly as on the drain's interrupt path. The default `kill_tree`'s root refusal
   cannot fire there: the child `Popen` just returned leads the new session it was started in, and
   a session leader cannot leave its group, so its pid is at least 2 and leads a group that is not
   the caller's; unreaped, it keeps that pid.

   Before this, `Popen` returned to the run function, which called the drain helper, which called
   the containment helper, and only then registered the pid: two frame entries between `Popen` and
   the registration. A frame entry is a point where stock CPython checks for a pending signal,
   tracer or no tracer, so a stop landing on either found the child untracked and left it running;
   the one-frame shape removes both, and with them the frame returns a Python-level tracer or
   profiler would add. The residual at `Popen`'s own call completion, below, is unchanged. A
   `finally` in that same frame forgets the pid around both the registration and the drain, and the
   same frame's interrupt guard spans the drain too. An interrupt after the registration but before
   the drain's own interrupt path is in force (at the drain's entry, which CPython checks with no
   tracer, or in the job containment) kills the child's tree and reaps it before re-raising, so a
   caller that never calls `kill_live_trees` orphans nothing. An interrupt the drain's own path
   already acted on, which it marks handled before re-raising, is not acted on again, so the kill
   happens once even when that path's reap expired (property 7); and a child already reaped is not
   signaled again, since its pid may have been reused. Blocking the stop signals around `Popen` was
   declined, because a child inherits a blocked signal mask and would be deaf to the very signals
   that stop it.

   What is still out of reach: an interrupt delivered inside `Popen`'s own call, before that
   call hands the object back: in `Popen`'s code after the child has started, at the
   pending-signal check CPython makes as the call instruction completes, or in a hook run on every
   instruction. No object reaches this module, so it has no child to name, and that child is left
   running in its own session; CPython at most reaps it after it exits on its own. And, as in
   property 7, a `SystemExit` is not caught, since it is the caller's own decision.
   `tests/test_spawn_window.py` provokes the window twice, by making the registering call raise
   and by raising the stop signal from a tracer at its first event after `Popen.__init__` returns,
   proves by pid that no child survives, and pins structurally that nothing is called between
   `Popen` and the registration. It also interrupts at the drain's entry and at the first trace
   event after the registration, and requires an empty registry. `tests/test_drain_entry_window.py`
   interrupts at the containment that opens the drain, with no handler calling `kill_live_trees`,
   and proves by pid that the child and its grandchild are gone (on Windows, where the child is
   still suspended there and starts no grandchild, that the child is gone).

## Implementation decisions that look wrong and are not

`start_new_session=True` is passed unconditionally. The stdlib documents it as POSIX-only, but
it is *accepted* on `win32` (verified). On POSIX it makes the child a process-group leader whose
group id is its own pid, which is what lets `killpg` reach that group with no lookup. Passing it
unconditionally (like `creationflags`, whose value a pure helper derives from the platform) keeps
the spawn itself free of a platform branch. A nested run passes it too, and so leaves the outer
group: joining the parent's group instead was considered and declined, because the inner kill
could then no longer signal its own child's group without signaling itself, and would need the
walk anyway (property 6).

The POSIX walk reads `ps`, not `psutil`, `sysctl` or `/proc`. `ps -A -o pid=,ppid=,pgid=,stat=`
is one command that BSD/macOS `ps` and procps both understand, and like `taskkill` it spawns
nothing, so its own capturing read is safe. It reads through `Popen` and a bounded
`communicate`, not `subprocess.run`, whose timeout path kills the child and then waits for it
with no bound: a wait that never ends while `ps` cannot yet die (property 6 above). `psutil` was
rejected: it would put a compiled dependency under a library that otherwise needs only the
standard library, and its recursive listing is the same parent-pid walk with no freeze, so it
would not close the race. `sysctl` through `ctypes` is a macOS-only structure layout, and `/proc`
is Linux-only.

A SIGTERM cascade was declined. Asking the tree to stop itself (each nesting level trapping
`SIGTERM` and killing its own in-flight run) needs every level to opt in, and the chain runs
through programs that never will, test suites and shell scripts among them; one silent level
breaks it. It would also add a grace period to every expiry. The walk needs nothing from the
processes it kills.

`Popen` is deliberately not used as a context manager. `Popen.__exit__` calls an unbounded
`wait()`, which would reintroduce this module's own bug on the failed-kill path. `with
subprocess.Popen(...)` reads like the more careful choice and is the less safe one here.

The Windows child is resumed through a thread snapshot. Assigning a *running* child to a job
races it: anything it starts before the assignment is born outside the job and survives the kill.
`CREATE_SUSPENDED` closes that window, but `Popen` closes the thread handle `CreateProcess`
returned, so the child is resumed by finding its one thread in a `CreateToolhelp32Snapshot` walk.
`NtResumeProcess` would be shorter and is undocumented, so it was not used.

The job sets `JOB_OBJECT_LIMIT_BREAKAWAY_OK`, and not `KILL_ON_JOB_CLOSE`. Without
breakaway-ok, a descendant that explicitly asks for `CREATE_BREAKAWAY_FROM_JOB` would have its
`CreateProcess` *fail* inside the job, where it succeeded before containment existed; with it, that
process leaves the job and escapes the kill, which is what it asked for. Kill-on-close stays off so
that closing the job when a run ends leaves a process the run deliberately detached still running,
just as the POSIX path never signals a finished run's group.

The job is found by pid, through a registry. `kill_tree(pid)` keeps its signature, so
`kill_tree_fn` stays `pid -> None` and no caller or fake changes. Each run registers its child's
job at spawn and removes it when the run ends, however it ends; a pid cannot be reused meanwhile,
because the child's `Popen` holds its process handle.

The kernel32 calls sit behind a seam that runs on every host. `_KernelJobApi` takes the loaded
library and the last-error reader as arguments, and its test fakes are C function pointers built
from the same `_KERNEL32_SIGNATURES` table the Windows binding applies. The call order, the
argument marshaling and the structure sizes are therefore checked on macOS and Linux too; only the
binding itself is Windows-only.

The Windows half was not split into a `src/` module. That split was weighed and declined,
because every project that type-checks an importer would need a new `mypy_path` entry for it.
The drain later moved to `src/` anyway, when the opt-in clock changed how it waits (below), and
those entries were added then. The Windows half stays in `bounded_run.py`: the drain split is a
seam between waiting and spawning, while the Windows half is part of the spawning. Only its
transcription of the Win32 headers moved, to `src/boundedrun_kernel32.py`: the structure layouts
and the kernel32 function table, which have no behavior of their own. `bounded_run` re-exports
them under their old names.

## Measuring the bound on a clock that counts sleep

`Popen.communicate(timeout=...)` measures on `time.monotonic()`. On macOS that clock stops while
the machine sleeps, so a bound on a laptop that sleeps mid-run is a bound on awake time, and a
child that sleeps with the machine is never killed. Pass `clock=` to measure the bound on another
clock instead:

```python
bounded_run.run_bounded(argv, timeout=7200, clock=bounded_run.sleep_inclusive_clock)
```

- **`sleep_inclusive_clock()`** reads `CLOCK_MONOTONIC` on macOS, which counts sleep there, and
  `CLOCK_BOOTTIME` on Linux, where `CLOCK_MONOTONIC` stops in suspend. Elsewhere it falls back to
  `time.time()`, which counts sleep but steps when the system clock is set.
- **With a clock,** the drain waits in slices of at most 30 s and re-reads the clock between them.
  The kill lands within one slice of *awake* time after the deadline, because nothing runs while
  the machine sleeps, the kill included.
- **Without a clock,** behavior is unchanged. It stays opt-in: an orchestrator's per-stage bound
  that counted sleep would kill stages on a laptop that dozed mid-run.
- **Not with stdin:** `clock=` with non-empty `stdin_text` or `stdin_bytes` raises `ValueError`
  before anything is spawned, because a sliced wait retries `communicate()`, and on CPython 3.12
  and earlier a retry arms the stdin pipe only for a non-empty `input` argument, which a retry may
  not pass, so input larger than the pipe buffer would stall until the deadline and be killed as
  a spurious timeout.

The wait lives in `src/boundedrun_drain.py`, a private module: how long to wait, on which clock,
and the kill-then-reread order. The POSIX tree kill lives in `src/boundedrun_posix_tree.py`, a
second one, and the registry of running children `kill_live_trees` reads in
`src/boundedrun_live.py`, a third. The `Popen` call itself, and the registration on the statement
after it (property 8), live in `src/boundedrun_spawn.py`, a fourth. Each was split out to keep
`bounded_run.py` to a reviewable length. `bounded_run.py` keeps the spawn's flags, Windows job
containment, and `kill_tree`, which picks between them. `tests/test_live_registry.py` pins the
registry's bookkeeping with fakes, and kills a real child and grandchild mid-run, probing that
both are gone.

## Proving the POSIX branch

`kill_tree`'s POSIX arm, and `kill_tree_posix` behind it, carry `# pragma: only-on posix`, not
`# pragma: no cover`: they cannot execute on Windows, so a coverage run configured for that
pragma excludes them there and *measures* them on every POSIX host. On macOS or Linux the suite
covers them natively. The live grandchild test kills a leader that is still running, the
exited-leader test kills a group whose leader has already exited, and
`tests/test_nested_tree_kill.py` kills a real tree that crosses a nested run's session, polling
for survivors rather than sleeping. A stubbed test pins the tolerated `ESRCH` and `EPERM`
outcomes, which a live run cannot produce on demand. `tests/test_posix_tree.py` pins the
freeze-and-walk loop's order (the first snapshot before any signal, every stop before any kill,
another round for a member still running) and that each refused root sends nothing, against
scripted process tables, and runs on every host because the loop takes its signals as seams.

Run on Linux, where `sys.platform` is `linux`, the live alive-leader grandchild test takes the
`killpg` path for real, and `ps` afterward shows zero surviving `gc_child` processes; the
exited-leader test has not been run there, and neither has any of property 6's walk. That run
alone could not have caught property 4's defect, because Linux answers the lookup that macOS
refuses.

## Proving the Windows branch

None of the job-object path has run on Windows yet. It was written on a macOS host with no
Windows machine available, and the first Windows run of this folder's suite, under coverage, is
its verification. What is established, and where:

| Check | Runs on | Establishes |
| --- | --- | --- |
| `mypy --platform win32` | any host | the Windows-only binding type-checks |
| `tests/test_job_containment.py` | every host, fake job API | create → assign → resume order; a failed step kills the child; `kill_tree` and every entry point go through the registered job |
| `tests/test_kernel_job_api.py` | every host, ctypes-marshaled fakes | kernel32 call order and arguments, handle closing, error read before close, the end of a thread walk told apart from a failed one, structure sizes against the documented x64 size (the x86 size is only computed under a 32-bit interpreter, which no run here has used, so it is asserted, not measured) |
| The exited-leader live test | Windows: not yet run | the grandchild is gone after the timeout; the regression test for property 5 |
| The unregistered-tree live test | Windows: not yet run | the `taskkill` fallback, now reached only for a pid this module did not start |
| The nested-session live test | Windows: not yet run | the `taskkill` fallback across a nested run's job, since the test starts the outer child itself; a registered job holding a nested one is documented behavior, not a run |
| Coverage of the `only-on win32` lines | Windows: not yet run | the real binding, resume and last-error reader execute |

## Measured behavior

| Path | Result against a 3.0s bound |
| --- | --- |
| Stdlib `subprocess.run(capture_output=True, timeout=3.0)`, Windows | Never returned within 5 minutes |
| `run_bounded`, Windows (`taskkill /F /T`, before the job object) | Raised `TimeoutExpired` after ~3.4s |
| `run_bounded`, Linux under WSL2 (`killpg`) | Full suite (then 7 tests) in 3.056s, no orphans |

The stdlib hang is the Windows shape: CPython's POSIX `subprocess.run` only `wait()`s the direct
child after its timeout kill instead of re-reading the pipes, so on macOS the same call returns
near the bound but leaves any grandchild running. That leak is what `kill_tree` exists to close.

On macOS, with the direct child exiting at once and its grandchild holding the pipes, a 1.0s bound
through the `getpgid` lookup raised `ProcessLookupError` at the bound and left the grandchild alive.
Signaling the group by pid raises `TimeoutExpired` and leaves no survivor.

On macOS, killing a live tree that crosses a nested run's session: with the single `killpg` the
nested run's child and grandchild both outlived the kill, as they did in the scheduled job that
found the defect. With the freeze-and-walk, neither survives, and the whole live test took 0.55s.

The Windows exited-leader case has not been measured, with `taskkill` or with the job object.
The analysis that motivated the job, stated as analysis: an exited process is absent from the
process list even while `Popen` holds its handle, so `taskkill /T /PID <leader>` finds no root
(its exit code is ignored) and kills nothing; the grandchild then keeps the pipes until the 5s
post-kill read expires, giving property 3's output-less `TimeoutExpired` with the grandchild still
running.

`samples/README.md` reproduces both the fixed and unfixed paths by hand, so the difference can be
confirmed in one command rather than taken on trust.

The output is held in memory at a multiple of its size, not at its size. On macOS under CPython
3.14, a child writing 100 MiB to stdout and exiting well inside a 60s bound raised this process's
peak RSS by about 250 MiB through `run_bounded_bytes` and about 350 MiB through `run_bounded`, the
same with `clock=` as without. The multiple is the standard library's: `communicate()` keeps the
chunks it reads in a list, joins them into one `bytes` while that list is still alive, and in text
mode decodes the result into a `str` as well, so a bytes capture holds two full copies at its peak
and a text capture three.

## Adopting it at a call site

The substitution keeps every read and every handler:

```python
# before
proc = subprocess.run(
    argv, cwd=cwd, capture_output=True,
    encoding=stdio_utf8.ENCODING, errors=stdio_utf8.CAPTURE_ERRORS, timeout=timeout,
)
return proc.returncode, proc.stdout + proc.stderr

# after
proc = bounded_run.run_bounded(argv, cwd=cwd, timeout=timeout)
return proc.returncode, proc.stdout + proc.stderr
```

The legitimate exceptions, where a bare capturing `subprocess.run` or `Popen` with a timeout
stays, are the tree kill's own two commands, `taskkill` and the `ps` snapshot: neither spawns
anything, so the grandchild precondition cannot arise. Each carries a marker comment that
silences a reliability linter's rule against a capturing run with a timeout, with that reason.
