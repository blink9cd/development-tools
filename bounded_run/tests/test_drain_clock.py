#!/usr/bin/env python3
"""bounded_run's opt-in clock: the sliced wait, the clock choice, and the live kill."""

from __future__ import annotations

import contextlib
import itertools
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# live_pid_guard is the shared record/probe/cleanup for live grandchild tests.
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent.parent / "live_pid_guard")
)

import bounded_run
import boundedrun_drain
import live_pid_guard

#: A child that spawns a grandchild holding the capture pipes, and neither exits on its own.
_GRANDCHILD_FIXTURE = Path(__file__).resolve().parent.parent / "samples" / "gc_child.py"

#: The live tests' timeout, and so their deadline on the fake clock. Far beyond any real wait,
#: so only a clock reading can reach it: a drain that ignored the clock would wait an hour.
_FAR_BOUND = 3600.0

#: The time left at every fake-clock reading short of the jump, so each real communicate() in
#: the live tests blocks exactly this long before its slice expires.
_LIVE_SLICE = 1.0

#: Real seconds a live test's clock waits for the child to reach the state the test needs before
#: it jumps past the deadline anyway, failing that test rather than hanging it. Generous, because
#: a heavily loaded host can take many seconds just to start an interpreter.
_READY_CEILING = 120.0

#: Ceiling on how long the call takes to return once the clock has jumped: the tree kill plus
#: the post-kill read, which KILL_GRACE_SECONDS bounds. A drain that missed the jump exceeds it.
_LIVE_CEILING = 30.0

#: A child that writes one line, records that it has, then holds until a release file appears,
#: and only then writes a second line and exits. Held past its own ceiling it exits 3 without the
#: second line, so a drain that never waited in slices fails the test rather than passing it.
_EARLY_THEN_HELD = (
    "import pathlib, sys, time\n"
    "sys.stdout.write('early\\n')\n"
    "sys.stdout.flush()\n"
    "pathlib.Path(sys.argv[1]).touch()\n"
    "release = pathlib.Path(sys.argv[2])\n"
    f"give_up = time.monotonic() + {_READY_CEILING:g}\n"
    "while not release.exists():\n"
    "    if time.monotonic() > give_up:\n"
    "        sys.exit(3)\n"
    "    time.sleep(0.05)\n"
    "sys.stdout.write('late\\n')\n"
)


class _SlicedProc:
    """A Popen stand-in recording each communicate(); the first *expiries* calls time out."""

    def __init__(self, *, expiries: int) -> None:
        self._expiries = expiries
        self.pid = 4242
        self.returncode = 0
        self.timeouts: list[float | None] = []
        self.inputs: list[str | None] = []

    def communicate(  # noqa: A002 - `input` mirrors Popen's parameter name
        self, input: str | None = None, timeout: float | None = None
    ) -> tuple[str, str]:
        self.timeouts.append(timeout)
        self.inputs.append(input)
        if len(self.timeouts) <= self._expiries:
            raise subprocess.TimeoutExpired(["fake"], timeout or 0.0)
        return "out", "err"


def _clock(*readings: float) -> Callable[[], float]:
    """A clock returning *readings* in order, then repeating the last one."""
    values = itertools.chain(readings, itertools.repeat(readings[-1]))
    return lambda: next(values)


class _SlicingClock:
    """A fake clock for a live child: one real _LIVE_SLICE per wait, until *jump* says so.

    Its first reading sets the deadline _FAR_BOUND ahead. Every later reading first calls
    *jump*. While that returns False the reading leaves exactly _LIVE_SLICE. Once it returns
    True, or once _READY_CEILING real seconds have passed, every reading lands past the deadline
    and ``jumped_at`` records when. Polling *jump* rather than jumping on a fixed reading is what
    keeps a slow host from turning a test into one about a child that had not started yet.
    """

    def __init__(self, jump: Callable[[], bool]) -> None:
        self._jump = jump
        self._give_up = time.monotonic() + _READY_CEILING
        self.readings = 0
        self.jumped_at: float | None = None

    def __call__(self) -> float:
        self.readings += 1
        if self.readings == 1:
            return 0.0
        if self.jumped_at is None and (
            self._jump() or time.monotonic() > self._give_up
        ):
            self.jumped_at = time.monotonic()
        return (
            2 * _FAR_BOUND if self.jumped_at is not None else _FAR_BOUND - _LIVE_SLICE
        )


def _recorded(pid_file: Path) -> bool:
    """Whether a live child has written a pid to *pid_file* yet."""
    with contextlib.suppress(FileNotFoundError, ValueError):
        return int(pid_file.read_text(encoding="utf-8")) > 0
    return False


def _release_a_slice_after(marker: Path, release: Path) -> Callable[[], bool]:
    """A *jump* callback that never jumps: it creates *release* a reading after *marker* shows.

    The reading that first sees *marker*, written just after the child flushed its early line,
    only notes it. So the whole slice after that reading runs with the line already in the pipe,
    and expires, because the child is still held. The reading after *that* expiry releases it.
    """
    seen = False

    def step() -> bool:
        nonlocal seen
        if seen:
            release.touch()
        seen = seen or marker.exists()
        return False

    return step


class WaitOnClockTests(unittest.TestCase):
    def test_each_slice_waits_the_smaller_of_a_slice_and_the_time_left(self) -> None:
        """Deadline 100: 100 left -> 30, 80 left -> 30, 25 left -> 25, then its streams return.

        The fake answers only its last call, so output read in an *earlier* slice is not
        exercised here: the live LiveSliceOutputTests below proves that part.
        """
        proc = _SlicedProc(expiries=2)
        result = boundedrun_drain._wait_on_clock(
            proc,  # type: ignore[arg-type]  # duck-typed Popen stand-in
            None,
            100.0,
            _clock(0.0, 0.0, 20.0, 75.0),
        )
        self.assertEqual(result, ("out", "err"))
        self.assertEqual(proc.timeouts, [30.0, 30.0, 25.0])

    def test_input_goes_to_the_first_slice_only(self) -> None:
        """communicate() refuses input on a retry, so only the first slice carries it.

        Whether a retry still writes the rest depends on the CPython version, which is why
        the entry points refuse clock= with non-empty stdin: see ClockWithStdinTests.
        """
        proc = _SlicedProc(expiries=2)
        boundedrun_drain._wait_on_clock(
            proc,  # type: ignore[arg-type]  # duck-typed Popen stand-in
            "payload",
            100.0,
            _clock(0.0),
        )
        self.assertEqual(proc.inputs, ["payload", None, None])

    def test_a_clock_at_the_deadline_raises_without_another_wait(self) -> None:
        proc = _SlicedProc(expiries=5)
        with self.assertRaises(subprocess.TimeoutExpired):
            boundedrun_drain._wait_on_clock(
                proc,  # type: ignore[arg-type]  # duck-typed Popen stand-in
                None,
                100.0,
                _clock(0.0, 0.0, 100.0),
            )
        self.assertEqual(proc.timeouts, [30.0])


class CommunicateBoundedClockTests(unittest.TestCase):
    def test_the_clock_deadline_kills_the_tree_then_rereads(self) -> None:
        proc = _SlicedProc(expiries=1)
        killed: list[int] = []
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            boundedrun_drain._communicate_bounded(
                proc,  # type: ignore[arg-type]  # duck-typed Popen stand-in
                ["fake"],
                100.0,
                killed.append,
                clock=_clock(0.0, 0.0, 500.0),
            )
        self.assertEqual(killed, [4242])
        self.assertEqual(caught.exception.cmd, ["fake"])
        self.assertEqual(caught.exception.output, "out")
        self.assertEqual(proc.timeouts, [30.0, boundedrun_drain.KILL_GRACE_SECONDS])

    def test_without_a_clock_the_single_wait_is_unchanged(self) -> None:
        proc = _SlicedProc(expiries=0)
        result = boundedrun_drain._communicate_bounded(
            proc,  # type: ignore[arg-type]  # duck-typed Popen stand-in
            ["fake"],
            7.0,
            lambda pid: None,
        )
        self.assertEqual(result, ("out", "err"))
        self.assertEqual(proc.timeouts, [7.0])


class SleepInclusiveClockTests(unittest.TestCase):
    def test_darwin_reads_clock_monotonic_which_counts_sleep_there(self) -> None:
        if sys.platform == "win32":
            # skipTest never returns, so mypy's win32 pass stops here and the POSIX-only
            # clock_gettime below still type-checks on that platform.
            self.skipTest("clock_gettime is POSIX-only")
        before = time.clock_gettime(time.CLOCK_MONOTONIC)
        reading = boundedrun_drain.clock_reader("darwin")()
        after = time.clock_gettime(time.CLOCK_MONOTONIC)
        self.assertLessEqual(before, reading)
        self.assertLessEqual(reading, after)

    def test_linux_reads_clock_boottime_which_counts_suspend(self) -> None:
        calls: list[int] = []

        def fake_gettime(clock_id: int) -> float:
            calls.append(clock_id)
            return 12.5

        with mock.patch.object(time, "CLOCK_BOOTTIME", 77, create=True):
            with mock.patch.object(time, "clock_gettime", fake_gettime, create=True):
                reading = boundedrun_drain.clock_reader("linux")()
        self.assertEqual((reading, calls), (12.5, [77]))

    def test_a_platform_without_one_falls_back_to_time_time(self) -> None:
        self.assertIs(boundedrun_drain.clock_reader("win32"), time.time)

    def test_a_named_clock_this_host_lacks_falls_back_to_time_time(self) -> None:
        with mock.patch.dict(
            boundedrun_drain._SLEEP_INCLUSIVE_CLOCKS, {"linux": "CLOCK_NOT_A_REAL_ONE"}
        ):
            self.assertIs(boundedrun_drain.clock_reader("linux"), time.time)

    def test_the_public_clock_is_reexported_and_reads_seconds(self) -> None:
        self.assertIs(
            bounded_run.sleep_inclusive_clock, boundedrun_drain.sleep_inclusive_clock
        )
        self.assertIsInstance(bounded_run.sleep_inclusive_clock(), float)


class ClockDeadlineRegressionTests(unittest.TestCase):
    """A bound measured on a clock that jumps past it must kill the whole tree.

    Without a clock, run_bounded waits on time.monotonic(), which stops while a Mac sleeps: a
    two-hour bound never fired across a 15-hour lid-shut night. Compressed here:
    the first reading sets a deadline an hour out, each later one leaves one real second until
    the fixture has recorded its grandchild, and the reading after that jumps past the deadline.
    """

    def test_a_clock_jumping_past_the_deadline_reaps_the_whole_tree(self) -> None:
        argv = [sys.executable, str(_GRANDCHILD_FIXTURE)]
        with live_pid_guard.record_fixture_pids() as pids:
            clock = _SlicingClock(jump=lambda: _recorded(pids.grandchild))
            with self.assertRaises(subprocess.TimeoutExpired):
                bounded_run.run_bounded(argv, timeout=_FAR_BOUND, clock=clock)
            returned_at = time.monotonic()
            # Probed before any assertion, so a failure cannot skip the probe and leave cleanup
            # signaling a pid it never saw.
            pids.probe_exits()
        jumped_at = clock.jumped_at
        if jumped_at is None:
            self.fail("the call returned before the clock jumped past the deadline")
        lag = returned_at - jumped_at
        self.assertLess(lag, _LIVE_CEILING, f"the jump did not kill: {lag:.1f}s")
        self.assertTrue(pids.child_gone, "the child outlived the tree kill")
        self.assertTrue(pids.grandchild_gone, "the grandchild outlived the tree kill")


#: What the clock-with-stdin refusal says: matched so an unrelated ValueError cannot pass for it.
_REFUSAL = "clock= cannot be combined with stdin"


def _spawn_forbidden() -> mock._patch[mock.MagicMock]:
    """Patch Popen so a spawn raises, instead of a stand-in whose result fails some other way."""
    return mock.patch.object(
        subprocess, "Popen", side_effect=AssertionError("a process was spawned")
    )


class ClockWithStdinTests(unittest.TestCase):
    """clock= refuses non-empty stdin input, before anything is spawned.

    A sliced wait retries communicate(), and a retry may not pass the input again. On CPython
    3.12 and earlier a retry then never writes the rest of it, because the POSIX drain arms the
    stdin pipe only for a truthy ``input`` argument. Input larger than the pipe buffer would stall
    the child until the deadline, and the kill would read as a spurious timeout.
    """

    def test_run_bounded_refuses_a_clock_with_stdin_text(self) -> None:
        with _spawn_forbidden() as popen:
            with self.assertRaisesRegex(ValueError, _REFUSAL):
                bounded_run.run_bounded(
                    ["fake"], timeout=1.0, stdin_text="x", clock=time.monotonic
                )
        popen.assert_not_called()

    def test_run_bounded_bytes_refuses_a_clock_with_stdin_bytes(self) -> None:
        with _spawn_forbidden() as popen:
            with self.assertRaisesRegex(ValueError, _REFUSAL):
                bounded_run.run_bounded_bytes(
                    ["fake"], timeout=1.0, stdin_bytes=b"x", clock=time.monotonic
                )
        popen.assert_not_called()

    def test_empty_stdin_with_a_clock_is_allowed_and_reads_as_eof(self) -> None:
        done = bounded_run.run_bounded(
            [sys.executable, "-c", "import sys; print(len(sys.stdin.read()))"],
            timeout=_FAR_BOUND,
            stdin_text="",
            clock=bounded_run.sleep_inclusive_clock,
        )
        self.assertEqual((done.returncode, done.stdout), (0, "0\n"))


class LiveSliceOutputTests(unittest.TestCase):
    """Output a real child wrote during a slice that expired is still in the result.

    Python documents that retrying communicate() after its TimeoutExpired loses no output. This
    is that claim measured through the drain: the child's first line is in the pipe for a whole
    slice that expires, and the second arrives only in a later slice.
    """

    def test_output_read_in_an_expired_slice_survives_to_the_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker, release = Path(tmp) / "early.written", Path(tmp) / "release"
            argv = [sys.executable, "-c", _EARLY_THEN_HELD, str(marker), str(release)]
            clock = _SlicingClock(jump=_release_a_slice_after(marker, release))
            done = bounded_run.run_bounded(argv, timeout=_FAR_BOUND, clock=clock)
        self.assertIsNone(
            clock.jumped_at, "the child was never released: see its ceiling"
        )
        self.assertEqual((done.returncode, done.stdout), (0, "early\nlate\n"))
