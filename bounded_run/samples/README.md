# `bounded_run` sample: see the bound hold by hand

`gc_child.py` is the shape that defeats a bare `subprocess.run(timeout=...)`: it spawns a
grandchild which inherits the captured stdout/stderr handles, then does not exit for an hour.
Killing only the direct child leaves those handles open, so the parent's post-timeout read
blocks until then.

It serves two purposes: live timeout tests drive it (`test_bounded_run.py`'s, and those of the
tools that run it as their own child), and it lets a human confirm the real behavior in one
command rather than trusting a test summary.

When the environment variables `GC_CHILD_PID_FILE` and `GC_GRANDCHILD_PID_FILE` name two files, it
also records its own pid in the first and its grandchild's in the second, so a live test can
confirm both are gone and kill a survivor itself: neither process exits for an hour. The live
tests set them through the `live_pid_guard` helper. The runs below set neither, so they record
nothing. The pid files travel in the environment rather than as arguments because a tool under
test may launch this fixture as its own subprocess and append flags of its own.

Every command below is run from the folder that holds `bounded_run/`.

## The fixed path: returns just after its bound

```bash
python -c "import sys; sys.path.insert(0, 'bounded_run'); import bounded_run, subprocess, time; t=time.monotonic();
try: bounded_run.run_bounded([sys.executable, 'bounded_run/samples/gc_child.py'], timeout=3.0)
except subprocess.TimeoutExpired: print('TimeoutExpired after %.2fs' % (time.monotonic()-t))"
```

Expected: `TimeoutExpired after 3.4s` or thereabouts: the 3.0s bound plus the tree kill.

## A nested run: the kill must reach a session the child started

`nest_child.py` runs `gc_child.py` through a nested `run_bounded`, the shape of an orchestrator
running an orchestrator. That nested run starts `gc_child.py` in a session of its own, so a kill
of the outer child's process group alone leaves `gc_child.py` and its grandchild running.
`tests/test_nested_tree_kill.py` drives it. On macOS or Linux, watch the kill take the whole tree:

```bash
python -c "import sys, subprocess, time; sys.path.insert(0, 'bounded_run'); import bounded_run
child = subprocess.Popen([sys.executable, 'bounded_run/samples/nest_child.py'], start_new_session=True)
time.sleep(3); bounded_run.kill_tree(child.pid); child.wait(); print('killed')"
pgrep -fl gc_child.py || echo 'no survivor'
```

Expected: `killed`, then `no survivor`. Three seconds is only for a person to wait by; on a loaded
machine the nested pair may not have started yet, in which case there was nothing to test, so
run it again. Before the kill walked nested sessions, `pgrep` listed a surviving `gc_child.py`
here, and its grandchild survived beside it unnamed: a bare `python -c` loop, which that pattern
does not match. Both had to be killed by hand.

## The unfixed path: for contrast, and be ready to interrupt it

The same call through the stdlib does not come back. Measured on Windows: still running after
five minutes against the same 3.0s bound. Interrupt it yourself; nothing else will.

```bash
python -c "import subprocess, sys, time; t=time.monotonic();
try: subprocess.run([sys.executable, 'bounded_run/samples/gc_child.py'], capture_output=True, encoding='utf-8', timeout=3.0)
except subprocess.TimeoutExpired: print('returned after %.2fs' % (time.monotonic()-t))"
```

## Cleaning up after the unfixed run

Interrupting the parent does not reap the grandchild: that is the whole defect. Both processes
end on their own after an hour (`_LIFETIME_SECONDS` in `gc_child.py`), so a forgotten pair cannot
run for good, but there is no need to wait: list the survivors, then end each one by its pid. On
macOS or Linux, `gc_child` matches both, because the grandchild's one-line program carries a
comment naming the fixture; end each with `kill <pid>`:

```bash
pgrep -fl gc_child
```

On Windows, list them with the command below and end each with `taskkill /PID <pid> /F`:

```bash
python -c "import subprocess; print(subprocess.run(['tasklist','/FI','IMAGENAME eq python.exe'],capture_output=True,text=True).stdout)"
```

The fixed path needs no cleanup: its tree kill takes the grandchild with it, which the live test
asserts.
