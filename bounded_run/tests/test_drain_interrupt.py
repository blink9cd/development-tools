#!/usr/bin/env python3
"""bounded_run's Ctrl-C path: an interrupted drain kills the child's tree before re-raising.

Every child starts in a session of its own, so the terminal's SIGINT reaches only the caller.
Without this path a Ctrl-C during a long run left the child running unattended: an interactive
tool that said "stopped" could still be writing files or making changes unseen. The fakes pin the
order (kill, bounded reap, re-raise); the live test interrupts a real tree and probes it is gone.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# live_pid_guard is the shared record/probe/cleanup for live grandchild tests.
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent.parent / "live_pid_guard")
)

import bounded_run
import boundedrun_drain
import boundedrun_live
import live_pid_guard

#: A child that spawns a grandchild holding the capture pipes, and neither exits on its own.
_GRANDCHILD_FIXTURE = Path(__file__).resolve().parent.parent / "samples" / "gc_child.py"

#: The live run's bound: far beyond the test, so only the interrupt can end the wait.
_FAR_BOUND = 3600.0

#: The time left at every clock reading before the interrupt, so each real wait lasts this long.
_LIVE_SLICE = 0.5

#: Real seconds the live clock waits for the fixture to record its grandchild before it
#: interrupts anyway, failing the test rather than hanging it on a heavily loaded host.
_READY_CEILING = 120.0

#: A leaf child that sleeps far beyond the test, so only a kill ends it.
_SLEEPER = [sys.executable, "-c", "import time; time.sleep(3600)"]

#: The reap's grace in the kill-count tests. A do-nothing kill makes every reap expire, so the
#: real KILL_GRACE_SECONDS would spend five seconds per reap for nothing the test reads.
_SHORT_GRACE = 0.2

#: Real seconds allowed for a killed child to be gone; a ceiling, spent only by a red run.
_GONE_CEILING = 10.0


class _InterruptedProc:
    """A Popen stand-in whose drain is interrupted; records the reap that follows."""

    def __init__(self, *, reap_expires: bool = False) -> None:
        self.pid = 4242
        self.events: list[str] = []
        self._reap_expires = reap_expires

    def communicate(  # noqa: A002 - `input` mirrors Popen's parameter name
        self, input: str | None = None, timeout: float | None = None
    ) -> tuple[str, str]:
        """The first call is interrupted; a later one is the reap, which may expire."""
        if not self.events:
            self.events.append("communicate")
            raise KeyboardInterrupt
        self.events.append(f"reap {timeout}")
        if self._reap_expires:
            raise subprocess.TimeoutExpired(["fake"], timeout or 0.0)
        return "", ""


class InterruptedDrainTests(unittest.TestCase):
    """A KeyboardInterrupt in the drain kills the tree, reaps it bounded, then re-raises."""

    def _interrupt(self, proc: _InterruptedProc) -> None:
        def kill(pid: int) -> None:
            proc.events.append(f"kill {pid}")

        with self.assertRaises(KeyboardInterrupt):
            boundedrun_drain._communicate_bounded(
                proc,  # type: ignore[arg-type]  # duck-typed Popen stand-in
                ["fake"],
                7.0,
                kill,
            )

    def test_the_tree_is_killed_and_reaped_before_the_interrupt_goes_on(self) -> None:
        proc = _InterruptedProc()
        self._interrupt(proc)
        grace = boundedrun_drain.KILL_GRACE_SECONDS
        self.assertEqual(proc.events, ["communicate", "kill 4242", f"reap {grace}"])

    def test_a_reap_that_expires_still_raises_the_interrupt(self) -> None:
        proc = _InterruptedProc(reap_expires=True)
        self._interrupt(proc)
        self.assertEqual(proc.events[1:], ["kill 4242", "reap 5.0"])

    def test_an_interrupt_during_a_clocked_wait_kills_too(self) -> None:
        killed: list[int] = []

        def interrupting_clock() -> float:
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            boundedrun_drain._communicate_bounded(
                _InterruptedProc(),  # type: ignore[arg-type]  # duck-typed Popen stand-in
                ["fake"],
                7.0,
                killed.append,
                clock=interrupting_clock,
            )
        self.assertEqual(killed, [4242])


def _interrupting_clock() -> float:
    """A clock whose first reading is the interrupt: it lands inside the drain's own wait."""
    raise KeyboardInterrupt


class DrainInterruptKillCountTests(unittest.TestCase):
    """The drain's interrupt path kills once; the spawn frame skips the interrupt it handled.

    Without the skip, a reap that expired on the drain's path left ``returncode`` unset, so the
    spawn frame killed and reaped a second time, doubling the bound to two grace periods
    (property 7 promises one). With a real kill the reap succeeds, and ``returncode`` alone
    keeps the spawn frame from signaling a pid that may since have been reused.
    """

    def setUp(self) -> None:
        """Start from an empty registry, recording each child as the drain contains it."""
        self.assertEqual(boundedrun_live.live_pids(), ())
        self.children: list[int] = []
        real_start = bounded_run._start_contained

        def record_then_contain(child: Any) -> None:
            self.children.append(child.pid)
            real_start(child)

        patcher = mock.patch.object(
            bounded_run, "_start_contained", record_then_contain
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._kill_survivors)

    def _kill_survivors(self) -> None:
        """Kill a child a do-nothing kill left running, so no run outlives the test."""
        for pid in self.children:
            if live_pid_guard.is_running(pid):
                bounded_run.kill_tree(pid)

    def _interrupted_run(
        self, run: Callable[..., object], kill_tree_fn: Callable[[int], None]
    ) -> None:
        """Run the sleeper through *run*, interrupted inside the drain's wait."""
        with self.assertRaises(KeyboardInterrupt):
            run(
                _SLEEPER,
                timeout=_FAR_BOUND,
                kill_tree_fn=kill_tree_fn,
                clock=_interrupting_clock,
            )
        self.assertEqual(len(self.children), 1, "the child was not started")
        self.assertEqual(boundedrun_live.live_pids(), ())

    def test_a_kill_that_does_not_work_is_not_repeated_by_the_spawn_frame(
        self,
    ) -> None:
        killed: list[int] = []
        with mock.patch.object(boundedrun_drain, "KILL_GRACE_SECONDS", _SHORT_GRACE):
            self._interrupted_run(bounded_run.run_bounded, killed.append)
        self.assertEqual(killed, self.children)

    def test_a_reaped_child_is_killed_once_by_either_entry_point(self) -> None:
        for run in (bounded_run.run_bounded, bounded_run.run_bounded_bytes):
            with self.subTest(run=run.__name__):
                self.children.clear()
                killed: list[int] = []

                def record_then_kill(pid: int, killed: list[int] = killed) -> None:
                    killed.append(pid)
                    bounded_run.kill_tree(pid)

                self._interrupted_run(run, record_then_kill)
                self.assertEqual(killed, self.children)
                self.assertTrue(
                    live_pid_guard.gone_within(self.children[0], _GONE_CEILING),
                    "the interrupted child outlived its kill",
                )


def _recorded(pid_file: Path) -> bool:
    """Whether a live child has written a pid to *pid_file* yet."""
    with contextlib.suppress(FileNotFoundError, ValueError):
        return int(pid_file.read_text(encoding="utf-8")) > 0
    return False


def _interrupt_once(ready: Callable[[], bool]) -> Callable[[], float]:
    """A clock that leaves `_LIVE_SLICE` per wait until *ready*, then raises KeyboardInterrupt.

    The interrupt arrives exactly where a Ctrl-C would: between two waits of the drain, while
    the child is running. Polling *ready* keeps a slow host from interrupting before the
    grandchild exists; past `_READY_CEILING` it interrupts anyway, failing the probe instead.
    """
    give_up = time.monotonic() + _READY_CEILING
    readings = 0

    def clock() -> float:
        nonlocal readings
        readings += 1
        if readings > 1 and (ready() or time.monotonic() > give_up):
            raise KeyboardInterrupt
        return _FAR_BOUND - _LIVE_SLICE if readings > 1 else 0.0

    return clock


class LiveInterruptTests(unittest.TestCase):
    """A real child and grandchild are both gone once an interrupted run_bounded returns."""

    def test_an_interrupted_run_leaves_no_process_behind(self) -> None:
        argv = [sys.executable, str(_GRANDCHILD_FIXTURE)]
        with live_pid_guard.record_fixture_pids() as pids:
            clock = _interrupt_once(lambda: _recorded(pids.grandchild))
            with self.assertRaises(KeyboardInterrupt):
                bounded_run.run_bounded(argv, timeout=_FAR_BOUND, clock=clock)
            # Probed before any assertion, so a failure cannot skip the probe and leave cleanup
            # signaling a pid it never saw.
            pids.probe_exits()
        self.assertTrue(pids.child_gone, "the child outlived the interrupt")
        self.assertTrue(pids.grandchild_gone, "the grandchild outlived the interrupt")


if __name__ == "__main__":
    unittest.main()
