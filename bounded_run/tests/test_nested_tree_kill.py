"""The live boundary for a NESTED bounded run: killing the outer tree must take the inner one too.

``run_bounded`` starts every child in a new session. A child that itself calls ``run_bounded``
therefore starts a grandchild leading a session of its own, which a kill of the outer child's
process group alone never reaches -- measured on a scheduled job of nested tools, where a
deadline kill of the outer one left a nested run writing results for 35 minutes afterward.

Nothing here is faked: the tree is real processes, and survival is polled for, never inferred
from a fixed sleep. On Windows, where this pid is not one ``run_bounded`` started, the kill takes
the ``taskkill /T`` fallback; the nested-job path there is documented in ``bounded_run.md``.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# live_pid_guard records the nested pair's pids and kills any survivor the kill under test missed.
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent.parent / "live_pid_guard")
)

import bounded_run
import live_pid_guard

#: The fixture whose own child runs the grandchild fixture through a nested run_bounded.
_NEST_FIXTURE = Path(__file__).resolve().parent.parent / "samples" / "nest_child.py"

#: How long the whole tree gets to come up: three interpreter starts on a machine that may be
#: heavily loaded. A readiness wait, not a bound under test, so it is generous by design.
_TREE_READY_SECONDS = 120.0

#: How often a pid file the nested pair has yet to write is re-read.
_RECORD_POLL_SECONDS = 0.05


def _await_tree(pid_file: Path) -> None:
    """Return once *pid_file* holds a pid: the deepest process exists, so the whole tree does.

    Raises AssertionError when it never appears, through the same check the guard uses.
    """
    deadline = time.monotonic() + _TREE_READY_SECONDS
    while time.monotonic() < deadline:
        with contextlib.suppress(FileNotFoundError, ValueError):
            int(pid_file.read_text(encoding="utf-8"))
            return
        time.sleep(_RECORD_POLL_SECONDS)
    live_pid_guard.require_recorded_pid(pid_file)


class LiveNestedSessionTreeTests(unittest.TestCase):
    """A kill of the outer child reaches the session its nested run_bounded started."""

    def test_no_process_survives_a_kill_through_a_nested_session(self) -> None:
        with live_pid_guard.record_fixture_pids() as pids:
            # A new session, exactly as run_bounded starts its own child; the nested run inside
            # the fixture then starts another, which is the escape under test.
            outer = subprocess.Popen(
                [sys.executable, str(_NEST_FIXTURE)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                # The deepest process must exist before the kill, or no nesting is tested.
                _await_tree(pids.grandchild)
                bounded_run.kill_tree(outer.pid)
                outer.wait(timeout=live_pid_guard.REAP_DEADLINE_SECONDS)
                # Probed before either assertion, so a failure cannot skip it.
                pids.probe_exits()
                self.assertTrue(
                    pids.child_gone, "the nested run's child outlived the tree kill"
                )
                self.assertTrue(
                    pids.grandchild_gone,
                    "the nested run's grandchild outlived the tree kill",
                )
            finally:
                # Reaped through its own handle, so nothing signals its pid a second time.
                outer.kill()
                outer.wait(timeout=live_pid_guard.REAP_DEADLINE_SECONDS)


if __name__ == "__main__":
    unittest.main()
