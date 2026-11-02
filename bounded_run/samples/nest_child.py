#!/usr/bin/env python3
"""Test fixture: run ``gc_child.py`` through a nested ``run_bounded``, then wait on it.

This is the shape an orchestrator has (one tool running another, which runs a third) and the
shape a single process-group kill misses. ``run_bounded`` starts every child in a new
session, so ``gc_child.py`` and its grandchild lead a session of their own, apart from this
process's. Killing this process's group alone leaves both running, still at work, with nothing
left to wait on them.

``gc_child.py`` records its own and its grandchild's pids when the environment names the two
pid files, as ``live_pid_guard`` does; this process passes its environment on unchanged, so the
nested pair records them exactly as it would when run directly. It records nothing of its own:
a test that starts this fixture holds its handle, and so its pid, already.
"""

import sys
from pathlib import Path

# bounded_run is the tool this fixture sits beside, resolved from this file's own location.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bounded_run

#: The nested bound. Far longer than any test using this fixture runs, so the kill under test,
#: and never this bound expiring, is what ends the nested pair.
_NESTED_TIMEOUT_SECONDS = 600.0

#: The fixture the nested run starts: a child and a grandchild that do not exit for an hour.
_GC_CHILD = Path(__file__).resolve().parent / "gc_child.py"

bounded_run.run_bounded(
    [sys.executable, str(_GC_CHILD)], timeout=_NESTED_TIMEOUT_SECONDS
)
