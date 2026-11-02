#!/usr/bin/env python3
"""bounded_run's spawn-to-track window: an interrupt there must not orphan the new child.

Every child starts in a session of its own, so a stop signal sent to the caller reaches none of
them; the caller's handler reaches them only through ``kill_live_trees``, which kills the
children the registry holds. A child is registered on the line right after ``Popen`` returns.
An interrupt raised before that registration completes finds the child untracked, so
``bounded_run`` itself must kill it before the interrupt propagates (``bounded_run.md``,
property 8).

The window is provoked deterministically, never by timing, at two points where CPython can run
a signal handler once ``Popen`` has returned. One is the registering call itself: the registry's
``track`` is patched to raise ``KeyboardInterrupt``, which is what a handler does when it runs at
that call's entry. The other is the first trace event after ``Popen.__init__`` returns, under a
Python-level tracer of the kind a debugger or a pure-Python coverage tracer installs: the tracer
is Python code, so the interpreter checks for a pending signal inside it, and the interrupt the
handler raises there propagates out of whichever frame was being traced. A frame that returns
the ``Popen`` object to its caller emits such an event before the object is registered. The
live tests then prove by pid, with a bounded wait, that no child survives. The unit test pins
the arm where ``Popen`` itself is interrupted and so returned nothing to kill.

Once registered, the child is reachable through ``kill_live_trees``, and what remains is that its
pid is forgotten however the run ends. An interrupt raised at the drain's frame entry (a point
CPython checks for a pending signal with no tracer installed) or at the first trace event after
the registration, must still leave the registry empty.
"""

from __future__ import annotations

import dis
import signal
import subprocess
import sys
import unittest
from collections.abc import Callable
from types import FrameType
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
import boundedrun_spawn
import live_pid_guard

#: A child that would run far longer than any test: only a kill ends it.
_SLEEPER = [sys.executable, "-c", "import time; time.sleep(3600)"]

#: The run's own bound, far beyond the test, so the bound cannot be what ends the child.
_FAR_BOUND = 3600.0

#: Real seconds allowed for the interrupted child to be gone. A ceiling on a possibly loaded
#: host, not an estimate: the fix kills and reaps it before the interrupt propagates, so the
#: first probe already finds it gone, and only a survivor spends the wait.
_GONE_CEILING = 10.0


def _release(proc: subprocess.Popen[Any]) -> None:
    """Close the pipes of a child the run left unreaped, and reap it within the ceiling.

    An interrupt that bounded_run only forgets hands the ``Popen`` object to nobody: the run
    neither closes its pipes nor waits on it, so the test that provoked it does both, or the
    pipes and the child's exit status leak past the test as a ``ResourceWarning``.
    """
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()
    proc.wait(timeout=_GONE_CEILING)


class _Interrupt(KeyboardInterrupt):
    """The stop a signal handler raises; a subclass, so the test can tell its own apart."""


class SpawnWindowLiveTests(unittest.TestCase):
    """A real child interrupted before or during its registration is killed, then re-raised."""

    def setUp(self) -> None:
        """Start from an empty registry, so a leftover pid can only be this test's child."""
        self.assertEqual(boundedrun_live.live_pids(), ())
        self.spawned: list[int] = []

    def tearDown(self) -> None:
        """Kill any child the run left running, so a red run leaks nothing past the test."""
        for pid in self.spawned:
            if live_pid_guard.is_running(pid):
                bounded_run.kill_tree(pid)
            boundedrun_live.forget(pid)

    def _run_interrupted(self, *, registers_first: bool) -> int:
        """Run the sleeper with its registration interrupted; return the child's pid."""
        real_track = boundedrun_live.track

        def interrupted_track(pid: int) -> None:
            self.spawned.append(pid)
            if registers_first:
                real_track(pid)
            raise _Interrupt

        with (
            mock.patch.object(boundedrun_live, "track", interrupted_track),
            self.assertRaises(_Interrupt),
        ):
            bounded_run.run_bounded(_SLEEPER, timeout=_FAR_BOUND)
        self.assertEqual(len(self.spawned), 1, "the child was not started exactly once")
        return self.spawned[0]

    def test_an_interrupt_before_registration_kills_the_child(self) -> None:
        pid = self._run_interrupted(registers_first=False)
        self.assertTrue(
            live_pid_guard.gone_within(pid, _GONE_CEILING),
            "the child interrupted before its registration was orphaned",
        )
        self.assertEqual(boundedrun_live.live_pids(), ())

    def test_an_interrupt_during_registration_kills_and_forgets_the_child(self) -> None:
        pid = self._run_interrupted(registers_first=True)
        self.assertTrue(
            live_pid_guard.gone_within(pid, _GONE_CEILING),
            "the child interrupted during its registration was orphaned",
        )
        self.assertEqual(
            boundedrun_live.live_pids(), (), "a dead child stayed registered"
        )


class _InterruptAfterSpawn:
    """A tracer that raises a stop signal at the first event in ``Popen``'s caller after it returns.

    Installed with ``sys.settrace``, it is what a debugger or a pure-Python coverage tracer is: a
    Python function the interpreter calls on each call, line and return. Its own bytecode is
    where CPython runs a pending signal's handler, so the signal is raised from inside it, and
    the handler's interrupt propagates out of the traced frame. Arming on the return of
    ``Popen.__init__`` means the child exists and nothing before it is touched. It fires only in
    the frame that called ``Popen``: on Windows the first event after the return is the dropped
    thread handle's finalizer, where an interrupt is only reported as ignored.
    """

    def __init__(self) -> None:
        """Start disarmed; :attr:`pid` is set once ``Popen.__init__`` returns."""
        self.pid: int | None = None
        self.caller: FrameType | None = None
        self.fired = False

    def __call__(self, frame: FrameType, event: str, _arg: object) -> Any:
        """Arm on ``Popen.__init__``'s return; raise the stop signal back in its caller."""
        if self.fired:
            return None
        if self.pid is not None and frame is self.caller:
            self.fired = True
            signal.raise_signal(signal.SIGINT)
        if event == "return" and frame.f_code is subprocess.Popen.__init__.__code__:
            self.pid = frame.f_locals["self"].pid
            self.caller = frame.f_back
        return self


class InterruptAfterSpawnTracerTests(unittest.TestCase):
    """The tracer fires in the frame that called ``Popen``, never in a frame between."""

    def test_an_event_in_another_frame_does_not_fire_it(self) -> None:
        # On Windows the first event after Popen returns is the call of the dropped thread
        # handle's finalizer, Handle.Close, where a raised interrupt is only reported as
        # "Exception ignored": the run went on, and the live test hung (toolkit 182).
        caller = mock.Mock(f_back=None)
        popen = mock.Mock(f_code=subprocess.Popen.__init__.__code__, f_back=caller)
        popen.f_locals = {"self": mock.Mock(pid=4321)}
        finalizer = mock.Mock(f_back=caller)
        tracer = _InterruptAfterSpawn()
        with mock.patch.object(signal, "raise_signal") as raise_signal:
            tracer(popen, "return", None)
            tracer(finalizer, "call", None)
            raise_signal.assert_not_called()
            tracer(caller, "line", None)
        raise_signal.assert_called_once_with(signal.SIGINT)
        self.assertEqual((tracer.pid, tracer.fired), (4321, True))


class SpawnReturnLiveTests(unittest.TestCase):
    """An interrupt at the first trace event after ``Popen`` returns kills the new child."""

    def setUp(self) -> None:
        """Start from an empty registry, and route SIGINT to this test's own interrupt."""
        self.assertEqual(boundedrun_live.live_pids(), ())
        self.tracer = _InterruptAfterSpawn()
        previous = signal.signal(signal.SIGINT, self._raise_interrupt)
        self.addCleanup(signal.signal, signal.SIGINT, previous)

    def tearDown(self) -> None:
        """Kill a child the run left running, so a red run leaks nothing past the test."""
        pid = self.tracer.pid
        if pid is not None:
            if live_pid_guard.is_running(pid):
                bounded_run.kill_tree(pid)
            boundedrun_live.forget(pid)

    @staticmethod
    def _raise_interrupt(_signum: int, _frame: FrameType | None) -> None:
        """The handler a caller installs: turn the stop signal into an interrupt."""
        raise _Interrupt

    def _assert_killed(self, run: Any) -> None:
        """Run *run* under the tracer; the interrupt propagates and its child is gone."""
        previous = sys.gettrace()
        sys.settrace(self.tracer)
        try:
            with self.assertRaises(_Interrupt):
                run()
        finally:
            sys.settrace(previous)
        self.assertTrue(self.tracer.fired, "the tracer never raised its signal")
        assert self.tracer.pid is not None  # narrows for mypy; fired implies a pid
        self.assertTrue(
            live_pid_guard.gone_within(self.tracer.pid, _GONE_CEILING),
            "the child interrupted as Popen returned was orphaned",
        )
        self.assertEqual(boundedrun_live.live_pids(), ())

    def test_run_bounded_kills_the_child_interrupted_as_popen_returns(self) -> None:
        self._assert_killed(
            lambda: bounded_run.run_bounded(_SLEEPER, timeout=_FAR_BOUND)
        )

    def test_run_bounded_bytes_kills_the_child_interrupted_as_popen_returns(
        self,
    ) -> None:
        self._assert_killed(
            lambda: bounded_run.run_bounded_bytes(_SLEEPER, timeout=_FAR_BOUND)
        )


class _InterruptAfterRegistration:
    """A tracer that raises a stop signal at the first chosen event after ``track`` returns.

    The child is registered by then, so the handler's ``kill_live_trees`` reaches it; what is
    under test is the other half of property 8, that its pid is forgotten however the run
    ends. A frame's entry is a point CPython checks for a pending signal with no tracer at all,
    so the ``call`` event of the drain's frame stands for an interrupt landing at that entry,
    before the drain's own interrupt path is in force. The ``Popen`` object is kept as
    ``Popen.__init__`` returns: an interrupt there leaves it unreaped, and the test reaps it.
    """

    def __init__(self, events: tuple[str, ...]) -> None:
        """Start disarmed; fire at the first event in *events* once ``track`` has returned."""
        self.events = events
        self.pid: int | None = None
        self.proc: subprocess.Popen[Any] | None = None
        self.fired_at = ""

    def __call__(self, frame: FrameType, event: str, _arg: object) -> Any:
        """Arm on ``track``'s return; raise the stop signal at the next chosen event."""
        if self.fired_at:
            return None
        if self.pid is not None and event in self.events:
            self.fired_at = f"{event} {frame.f_code.co_qualname}"
            signal.raise_signal(signal.SIGINT)
        if event == "return" and frame.f_code is subprocess.Popen.__init__.__code__:
            self.proc = frame.f_locals["self"]
        if event == "return" and frame.f_code is _REAL_TRACK.__code__:
            self.pid = frame.f_locals["pid"]
        return self


#: The registry's own ``track``, whose return arms :class:`_InterruptAfterRegistration`.
_REAL_TRACK = boundedrun_live.track


class RegisteredInterruptLiveTests(unittest.TestCase):
    """An interrupt after registration, before the drain's guard, still forgets the child."""

    def setUp(self) -> None:
        """Start from an empty registry; SIGINT runs the handler a caller installs."""
        self.assertEqual(boundedrun_live.live_pids(), ())
        self.tracer: _InterruptAfterRegistration | None = None
        previous = signal.signal(signal.SIGINT, self._kill_then_interrupt)
        self.addCleanup(signal.signal, signal.SIGINT, previous)

    def tearDown(self) -> None:
        """Kill a child the run left running, forget it, and release its ``Popen``.

        Needed on a pass as well as a red run: the handler kills the child, but nothing in the
        run closes its pipes or reaps it.
        """
        pid = self.tracer.pid if self.tracer is not None else None
        if pid is not None:
            if live_pid_guard.is_running(pid):
                bounded_run.kill_tree(pid)
            boundedrun_live.forget(pid)
        if self.tracer is not None and self.tracer.proc is not None:
            _release(self.tracer.proc)

    @staticmethod
    def _kill_then_interrupt(_signum: int, _frame: FrameType | None) -> None:
        """The handler property 8 describes: kill every registered tree, then interrupt."""
        bounded_run.kill_live_trees()
        raise _Interrupt

    def _assert_forgotten(self, run: Any, events: tuple[str, ...]) -> None:
        """Run *run* interrupted at the first of *events* after registration; check the pid."""
        self.tracer = _InterruptAfterRegistration(events)
        previous = sys.gettrace()
        sys.settrace(self.tracer)
        try:
            with self.assertRaises(_Interrupt):
                run()
        finally:
            sys.settrace(previous)
        self.assertTrue(self.tracer.fired_at, "the tracer never raised its signal")
        assert self.tracer.pid is not None  # narrows for mypy; firing implies a pid
        self.assertTrue(
            live_pid_guard.gone_within(self.tracer.pid, _GONE_CEILING),
            f"the child interrupted at {self.tracer.fired_at} survived",
        )
        self.assertEqual(
            boundedrun_live.live_pids(),
            (),
            f"interrupted at {self.tracer.fired_at}, the pid stayed registered",
        )

    def test_run_bounded_forgets_a_child_interrupted_at_the_drain_entry(self) -> None:
        self._assert_forgotten(
            lambda: bounded_run.run_bounded(_SLEEPER, timeout=_FAR_BOUND), ("call",)
        )

    def test_run_bounded_bytes_forgets_a_child_interrupted_at_the_drain_entry(
        self,
    ) -> None:
        self._assert_forgotten(
            lambda: bounded_run.run_bounded_bytes(_SLEEPER, timeout=_FAR_BOUND),
            ("call",),
        )

    def test_run_bounded_forgets_a_child_interrupted_right_after_registering(
        self,
    ) -> None:
        self._assert_forgotten(
            lambda: bounded_run.run_bounded(_SLEEPER, timeout=_FAR_BOUND),
            ("call", "line", "return"),
        )


def _is_call(instruction: dis.Instruction) -> bool:
    """Whether *instruction* calls something: ``CALL``, ``CALL_KW`` and their kin."""
    return instruction.opname.startswith(("CALL", "PRECALL"))


class SpawnAdjacencyTests(unittest.TestCase):
    """Structurally, nothing is called between ``Popen`` returning and the registration.

    The live tests prove one injection point; this pins the shape that closes them all: the
    registering call is the first call after the ``Popen`` call, and the only instruction
    between that call and the registration's own loads is the store of its result.
    """

    def _assert_adjacent(self, spawn: Callable[..., object]) -> None:
        """*spawn* stores ``Popen``'s result, then calls ``track`` before anything else."""
        instructions = list(dis.get_instructions(spawn))
        popen_line = next(
            i.positions.lineno
            for i in instructions
            if i.argval == "Popen" and i.positions is not None
        )
        track_at = next(
            i for i, ins in enumerate(instructions) if ins.argval == "track"
        )
        calls = [i for i in range(track_at) if _is_call(instructions[i])]
        popen_call = instructions[calls[-1]]
        self.assertIsNotNone(popen_call.positions)
        assert popen_call.positions is not None  # narrows for mypy
        self.assertEqual(
            popen_call.positions.lineno, popen_line, "Popen is not the last call"
        )
        store = instructions[calls[-1] + 1]
        self.assertTrue(store.opname.startswith("STORE_FAST"), store.opname)
        self.assertIn("proc", str(store.argval))
        self._assert_track_calls_nothing_first(instructions, track_at)

    def _assert_track_calls_nothing_first(
        self, instructions: list[dis.Instruction], track_at: int
    ) -> None:
        """The first call after the ``track`` load is ``track``'s own: none in its arguments.

        ``track``'s own ``CALL`` spans the whole ``boundedrun_live.track(...)`` expression, so it
        starts where the attribute load does; a call nested in its argument list starts later.
        """
        track_load = instructions[track_at].positions
        first_call = next(
            ins for ins in instructions[track_at:] if _is_call(ins)
        ).positions
        assert track_load is not None and first_call is not None  # narrows for mypy
        self.assertEqual(
            (first_call.lineno, first_call.col_offset),
            (track_load.lineno, track_load.col_offset),
            "something is called inside track's argument list, before track itself",
        )

    def test_run_text_registers_right_after_popen(self) -> None:
        self._assert_adjacent(boundedrun_spawn.run_text)

    def test_run_bytes_registers_right_after_popen(self) -> None:
        self._assert_adjacent(boundedrun_spawn.run_bytes)


class SpawnInterruptedTests(unittest.TestCase):
    """An interrupt inside ``Popen`` itself returns no child, so there is nothing to kill."""

    def test_the_interrupt_propagates_and_nothing_is_killed(self) -> None:
        kills: list[int] = []
        with (
            mock.patch.object(bounded_run.subprocess, "Popen", side_effect=_Interrupt),
            self.assertRaises(_Interrupt),
        ):
            bounded_run.run_bounded(["x"], timeout=1, kill_tree_fn=kills.append)
        self.assertEqual(kills, [])
        self.assertEqual(boundedrun_live.live_pids(), ())


if __name__ == "__main__":
    unittest.main()
