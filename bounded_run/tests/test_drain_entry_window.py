#!/usr/bin/env python3
"""bounded_run's registration-to-drain window: an interrupt there must kill the child's tree.

Once the spawn registers a child, the drain runs, and only inside the drain's wait is its own
interrupt path in force (``_communicate_bounded``). A ``KeyboardInterrupt`` raised between the
two (at the drain's frame entry, or in the job containment that precedes the wait) found the
child registered but nothing in ``bounded_run`` killing it: only a caller whose handler calls
``kill_live_trees`` stopped it, and every other caller orphaned it.

The window is provoked deterministically, never by timing: ``_start_contained``, the first call
the drain makes, is patched to raise the interrupt, and no handler calls ``kill_live_trees``.
The child is a parent that starts a grandchild in its own process group, so the test shows the
whole tree is gone, by pid, with a bounded wait. On Windows the child is still suspended when
the containment that opens the drain runs, so it starts no grandchild there: the Windows test
interrupts at once and shows the child itself is gone.
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# live_pid_guard is the shared probe for live child tests: it never signals the pid it probes.
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent.parent / "live_pid_guard")
)

import bounded_run
import boundedrun_live
import live_pid_guard

#: A parent that starts a long-sleeping grandchild, writes its pid to the file named by its one
#: argument, then sleeps itself: only a kill of the tree ends both.
_TREE_SCRIPT = (
    "import subprocess, sys, time\n"
    "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3600)'])\n"
    "open(sys.argv[1], 'w').write(str(g.pid))\n"
    "time.sleep(3600)\n"
)

#: The run's own bound, far beyond the test, so the bound cannot be what ends the child.
_FAR_BOUND = 3600.0

#: Real seconds allowed for a process to appear or be gone. A ceiling on a possibly loaded host,
#: not an estimate: only a red run or a slow start spends it.
_CEILING = 10.0

#: Seconds between polls of the grandchild's pid file.
_POLL_SECONDS = 0.05

#: A leaf child for Windows, where it never runs: it is interrupted while still suspended.
_SLEEPER = [sys.executable, "-c", "import time; time.sleep(3600)"]


class _Interrupt(KeyboardInterrupt):
    """The stop a signal handler raises; a subclass, so the test can tell its own apart."""


class DrainEntryInterruptLiveTests(unittest.TestCase):
    """An interrupt after registration, before the drain's own guard, kills the whole tree."""

    def setUp(self) -> None:
        """Start from an empty registry, with a scratch pid file for the grandchild."""
        self.assertEqual(boundedrun_live.live_pids(), ())
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.pid_file = Path(scratch.name) / "grandchild.pid"
        self.pids: list[int] = []

    def tearDown(self) -> None:
        """Kill whatever the run left running, so a red run leaks nothing past the test."""
        # The child leads the tree's process group, so its tree kill reaches the grandchild.
        if any(live_pid_guard.is_running(pid) for pid in self.pids):
            bounded_run.kill_tree(self.pids[0])
        for pid in self.pids:
            boundedrun_live.forget(pid)

    def _grandchild_pid(self) -> int:
        """The grandchild's pid, once the child has written it; polled under the ceiling."""
        for _ in range(int(_CEILING / _POLL_SECONDS)):
            text = self.pid_file.read_text() if self.pid_file.exists() else ""
            if text:
                return int(text)
            time.sleep(_POLL_SECONDS)
        self.fail("the child never reported its grandchild")

    def _interrupt_at_containment(self, child: Any) -> None:
        """Stand-in for ``_start_contained``: wait for the tree to exist, then interrupt."""
        self.pids.append(child.pid)
        self.pids.append(self._grandchild_pid())
        raise _Interrupt

    def _assert_tree_killed(self, run: Callable[..., object]) -> None:
        """Run *run* on the tree, interrupted at the containment that opens the drain.

        Every pid of the tree must be gone, and the registry empty.
        """
        argv = [sys.executable, "-c", _TREE_SCRIPT, str(self.pid_file)]
        with (
            mock.patch.object(
                bounded_run, "_start_contained", self._interrupt_at_containment
            ),
            self.assertRaises(_Interrupt),
        ):
            run(argv, timeout=_FAR_BOUND)
        self.assertEqual(len(self.pids), 2, "the tree was not started")
        for pid in self.pids:
            self.assertTrue(
                live_pid_guard.gone_within(pid, _CEILING),
                f"pid {pid} of the tree interrupted at the containment was orphaned",
            )
        self.assertEqual(boundedrun_live.live_pids(), ())

    @unittest.skipIf(
        sys.platform == "win32",
        "the child stays suspended until containment, so it starts no tree",
    )
    def test_run_bounded_kills_the_tree_interrupted_at_the_containment_that_opens_the_drain(
        self,
    ) -> None:
        self._assert_tree_killed(bounded_run.run_bounded)

    @unittest.skipIf(
        sys.platform == "win32",
        "the child stays suspended until containment, so it starts no tree",
    )
    def test_run_bounded_bytes_kills_the_tree_interrupted_at_the_containment_that_opens_the_drain(
        self,
    ) -> None:
        self._assert_tree_killed(bounded_run.run_bounded_bytes)

    def _interrupt_suspended(self, child: Any) -> None:
        """Stand-in for ``_start_contained`` on Windows: record the child, interrupt at once."""
        self.pids.append(child.pid)
        raise _Interrupt

    @unittest.skipUnless(
        sys.platform == "win32", "the suspended-child variant of the POSIX tests above"
    )
    def test_run_bounded_kills_the_suspended_child_interrupted_at_the_containment(  # pragma: only-on win32
        self,
    ) -> None:
        with (
            mock.patch.object(
                bounded_run, "_start_contained", self._interrupt_suspended
            ),
            self.assertRaises(_Interrupt),
        ):
            bounded_run.run_bounded(_SLEEPER, timeout=_FAR_BOUND)
        self.assertEqual(len(self.pids), 1, "the child was not started")
        self.assertTrue(
            live_pid_guard.gone_within(self.pids[0], _CEILING),
            "the suspended child interrupted at the containment was orphaned",
        )
        self.assertEqual(boundedrun_live.live_pids(), ())


if __name__ == "__main__":
    unittest.main()
