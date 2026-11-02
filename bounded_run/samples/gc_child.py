#!/usr/bin/env python3
"""Test fixture: spawn a grandchild that inherits our pipes, then outlive every test's bound.

This is the shape that defeats a bare ``subprocess.run(timeout=...)``: the grandchild inherits
the captured stdout/stderr handles, so killing only the direct child leaves them open and the
parent's post-timeout read blocks forever. The live timeout tests here drive it.

When the environment names two pid files, it records its own pid in the first (before the
grandchild exists) and the grandchild's in the second. Neither process exits before
``_LIFETIME_SECONDS``, an hour, so a test that cannot see both gone needs the pids to kill a
survivor itself; ``live_pid_guard`` sets the variables and does that cleanup. With neither set,
as ``README.md`` runs it, it records nothing.

The environment and not argv, because a tool under test may launch this fixture as its own
subprocess and append flags of its own. Positional pid files took those flags for paths: the
fixture wrote its pid into a file named ``--path`` beside the caller, then crashed writing to the
directory after it, so the wedged child those tests model had already exited while they passed.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

#: The variables naming the optional pid files: this process's own, then the grandchild's.
#: Restated from live_pid_guard rather than imported, because this runs as a bare script under
#: whatever interpreter a tool hands it; a drift fails every guarded test at its probe.
_CHILD_PID_ENV = "GC_CHILD_PID_FILE"
_GRANDCHILD_PID_ENV = "GC_GRANDCHILD_PID_FILE"

#: How long both processes live unless killed: an hour. Far past any test's bound, so a pair found
#: gone was still ended by the kill under test; but finite, so a pair whose cleanup was lost (a
#: runner killed mid-test) does not run on for good. ``tests/test_fixture_lifetime.py`` rewrites
#: this one line, so keep it a plain literal assignment.
_LIFETIME_SECONDS = 3600.0

#: The grandchild's whole program: sleep out the same ceiling. Its comment names this fixture, so
#: ``pgrep -f gc_child`` finds the grandchild as well as the child. Built by concatenation rather
#: than written as a literal with an escape, so this file needs no embedded newline of its own.
_GRANDCHILD_PROGRAM = (
    "import time  # gc_child.py's grandchild"
    + chr(10)
    + f"time.sleep({_LIFETIME_SECONDS!r})"
)


def _record_pid(env_name: str, pid: int) -> None:
    """Write *pid* to the file the variable *env_name* names, when the caller set it."""
    pid_file = os.environ.get(env_name)
    if pid_file:
        Path(pid_file).write_text(str(pid), encoding="utf-8", newline="\n")


# The grandchild is deliberately abandoned: a process that outlives its parent while holding the
# inherited pipes is the condition under test. The suppression has to sit on the flagged line
# itself; on the lines above it parses but never applies, which is silent and was worth learning.
# The marker silences RL002, a reliability linter's rule against a Popen whose process is
# abandoned. The handle is bound only to read its pid, which that rule does not count as
# managing the process, so the finding stands; the reason is kept short so that line still fits
# the formatter's width.
#: Bound to a name so the Popen call plus its suppression fit inside the formatter's line width.
#: Inlining the argv pushes the line past it, and `ruff format` then explodes the call across
#: lines, which silently moves the pragma off the flagged line and stops it applying.
_GRANDCHILD = [sys.executable, "-c", _GRANDCHILD_PROGRAM]

_record_pid(_CHILD_PID_ENV, os.getpid())
grandchild = subprocess.Popen(_GRANDCHILD)  # reliability: ignore[RL002] left running
_record_pid(_GRANDCHILD_PID_ENV, grandchild.pid)
# One sleep, not a loop: the child, like the grandchild, ends once the ceiling has passed.
time.sleep(_LIFETIME_SECONDS)
