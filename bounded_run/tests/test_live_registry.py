#!/usr/bin/env python3
"""bounded_run's live-child registry: a caller's signal handler can reach every running tree.

Every child starts in a session of its own, so a SIGTERM to the caller (a logout, a scheduler's
stop) reaches none of them: the caller dies and its children run on unattended, with any lock it
held released. ``kill_live_trees`` is the caller's way out: it kills the tree of every child a
run here has started and not yet finished, and says how many. The fakes pin the registry's
bookkeeping; the live test kills a real child and grandchild mid-run and probes they are gone.

The fakes' drains run under a job API that accepts any pid, on every host. Windows contains each
child in a job before tracking it, and the real API's ``OpenProcess`` refuses a pid no process
holds (``ERROR_INVALID_PARAMETER``), so a fake pid there never reached the registry at all.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# live_pid_guard is the shared record/probe/cleanup for live grandchild tests.
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent.parent / "live_pid_guard")
)

import bounded_run
import boundedrun_live
import live_pid_guard

#: A child that spawns a grandchild holding the capture pipes, and neither exits on its own.
_GRANDCHILD_FIXTURE = Path(__file__).resolve().parent.parent / "samples" / "gc_child.py"

#: The live run's bound: far beyond the test, so only the registry's kill can end the wait.
_FAR_BOUND = 3600.0

#: Real seconds the live test waits for the fixture to record its grandchild, and then for
#: the killed run to return: readiness waits on a possibly loaded host, not bounds under test.
_READY_CEILING = 120.0

#: How often a pid file the fixture has yet to write is re-read.
_RECORD_POLL_SECONDS = 0.05

#: How long the late-tracking test holds a child untracked after its grandchild is
#: recorded: many polls of the readiness check, so one keyed on the record fires inside it.
_LATE_TRACK_SECONDS = 2.0


#: The handle the accepting job API hands out.
_JOB = 7001


class _AcceptingJobApi:
    """A job API that contains any pid, recording the jobs it closes."""

    def __init__(self) -> None:
        self.closed: list[int] = []

    def create_job(self) -> int:
        """The one job handle every run is given."""
        return _JOB

    def assign(self, job: int, pid: int) -> None:
        """Accept *pid*, which no real process holds."""

    def resume(self, pid: int) -> None:
        """Nothing is suspended: the fake was never started."""

    def terminate(self, job: int) -> None:
        """Nothing runs in the job."""

    def close(self, job: int) -> None:
        """Record that *job* was closed."""
        self.closed.append(job)


class _DrainedProc:
    """A Popen stand-in whose drain records what the registry held while it ran."""

    def __init__(self, pid: int, *, fails: bool = False) -> None:
        self.pid = pid
        self.returncode = 0
        self.seen: tuple[int, ...] = ()
        self._fails = fails

    def communicate(  # noqa: A002 - `input` mirrors Popen's parameter name
        self, input: str | None = None, timeout: float | None = None
    ) -> tuple[str, str]:
        """Note the registry's contents, then finish or fail as scripted."""
        self.seen = boundedrun_live.live_pids()
        if self._fails:
            raise RuntimeError("a drain that fails")
        return "", ""


class RegistryTests(unittest.TestCase):
    """The registry holds a child exactly while its drain runs."""

    def setUp(self) -> None:
        """Contain every fake child in a job, as Windows does, and on every host."""
        self.jobs = _AcceptingJobApi()
        patcher = mock.patch.object(bounded_run, "_job_api_factory", lambda: self.jobs)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.assertEqual(boundedrun_live.live_pids(), (), "a pid was left registered")

    @staticmethod
    def _run_with(proc: _DrainedProc) -> None:
        """Run the bounded call with *proc* standing in for the child ``Popen`` starts."""
        with mock.patch.object(subprocess, "Popen", lambda *_a, **_k: proc):
            bounded_run.run_bounded(
                ["fake"], timeout=1.0, kill_tree_fn=lambda _pid: None
            )

    def test_a_child_is_registered_only_while_it_is_drained(self) -> None:
        proc = _DrainedProc(4242)
        self._run_with(proc)
        self.assertEqual(proc.seen, (4242,))
        self.assertEqual(self.jobs.closed, [_JOB])

    def test_a_drain_that_raises_still_forgets_its_child(self) -> None:
        proc = _DrainedProc(4243, fails=True)
        with self.assertRaises(RuntimeError):
            self._run_with(proc)
        self.assertEqual(proc.seen, (4243,))
        self.assertEqual(self.jobs.closed, [_JOB])

    def test_forgetting_an_unknown_pid_is_harmless(self) -> None:
        boundedrun_live.forget(999_999)


class KillEachTests(unittest.TestCase):
    """`kill_each` kills every registered tree, past one it cannot kill, and counts."""

    def setUp(self) -> None:
        for pid in (11, 12, 13):
            boundedrun_live.track(pid)
            self.addCleanup(boundedrun_live.forget, pid)

    def test_every_live_tree_is_killed_and_counted(self) -> None:
        killed: list[int] = []
        self.assertEqual(boundedrun_live.kill_each(killed.append), 3)
        self.assertEqual(sorted(killed), [11, 12, 13])

    def test_a_tree_that_cannot_be_killed_does_not_spare_the_rest(self) -> None:
        killed: list[int] = []

        def kill(pid: int) -> None:
            if pid == 12:
                raise OSError("ps could not run")
            killed.append(pid)

        self.assertEqual(boundedrun_live.kill_each(kill), 2)
        self.assertEqual(sorted(killed), [11, 13])

    def test_a_refused_root_does_not_spare_the_rest(self) -> None:
        # The tree kill raises ValueError, sending nothing, for a root it refuses (one in a
        # group it does not lead, say); the other trees must still be killed.
        killed: list[int] = []

        def kill(pid: int) -> None:
            if pid == 12:
                raise ValueError("refusing to kill the tree of pid 12")
            killed.append(pid)

        self.assertEqual(boundedrun_live.kill_each(kill), 2)
        self.assertEqual(sorted(killed), [11, 13])

    def test_the_public_function_kills_through_kill_tree(self) -> None:
        with mock.patch.object(bounded_run, "kill_tree") as kill_tree:
            self.assertEqual(bounded_run.kill_live_trees(), 3)
        self.assertEqual(
            sorted(call.args[0] for call in kill_tree.call_args_list), [11, 12, 13]
        )

    def test_nothing_running_kills_nothing(self) -> None:
        for pid in (11, 12, 13):
            boundedrun_live.forget(pid)
        with mock.patch.object(bounded_run, "kill_tree") as kill_tree:
            self.assertEqual(bounded_run.kill_live_trees(), 0)
        kill_tree.assert_not_called()


class LivePidsTests(unittest.TestCase):
    """`live_pids` is the public, read-only view of the registry `kill_live_trees` reads."""

    def test_every_tracked_child_is_listed(self) -> None:
        for pid in (21, 22):
            boundedrun_live.track(pid)
            self.addCleanup(boundedrun_live.forget, pid)
        self.assertEqual(sorted(bounded_run.live_pids()), [21, 22])

    def test_nothing_running_lists_nothing(self) -> None:
        self.assertEqual(bounded_run.live_pids(), ())


def _recorded(pid_file: Path) -> bool:
    """Whether a live child has written a pid to *pid_file* yet."""
    with contextlib.suppress(FileNotFoundError, ValueError):
        return int(pid_file.read_text(encoding="utf-8")) > 0
    return False


def _child_tracked(pids: live_pid_guard.FixturePids) -> bool:
    """Whether the grandchild is recorded and the child is in `bounded_run`'s live set.

    Only then can `kill_live_trees` reach the whole tree. The grandchild's record alone
    proves neither: the child is tracked a few bytecodes after `Popen` returns, and a
    loaded host can run the whole fixture in between. The child's pid file is written
    before the grandchild's, so it is whole by then.
    """
    if not _recorded(pids.grandchild):
        return False
    if sys.platform == "win32":
        # A venv's python.exe there is a launcher, so the pid the fixture records is its
        # interpreter child's, never the one Popen holds; the registry starts empty
        # (setUp), so any tracked pid is this test's child.
        return bool(bounded_run.live_pids())  # pragma: only-on win32
    recorded = live_pid_guard.require_recorded_pid(pids.child)  # pragma: only-on posix
    return recorded in bounded_run.live_pids()  # pragma: only-on posix


def _await_the_untracked_childs_grandchild() -> None:
    """Wait, from inside the held-back tracking, for the grandchild where it can appear there.

    On POSIX the child runs before it is tracked, so its grandchild's record lands inside
    the gap, and waiting for it makes the gap certain. On Windows the child is created
    suspended and is resumed only once it is in its job, after it is tracked, so no
    grandchild exists yet: waiting here would hold the tracking until the ready ceiling, the
    readiness check would give up first, and the kill would find nothing (CI saw exactly
    that, ``0 != 1``). There the gap cannot open on any record, since the suspended child
    writes none until it is tracked, so on Windows the test proves only that a child tracked
    late is still killed; telling the gap apart is proven on POSIX, the one host it exists on.
    """
    if sys.platform == "win32":
        return  # pragma: only-on win32
    _await_the_grandchilds_record()  # pragma: only-on posix


def _await_the_grandchilds_record() -> None:  # pragma: only-on posix
    """Poll until the fixture records its grandchild's pid, or the ready ceiling passes."""
    grandchild = Path(os.environ[live_pid_guard.GRANDCHILD_PID_ENV])
    give_up = time.monotonic() + _READY_CEILING
    while not _recorded(grandchild) and time.monotonic() < give_up:
        time.sleep(_RECORD_POLL_SECONDS)


class LiveKillTests(unittest.TestCase):
    """A real child and grandchild, mid-run, are both gone once `kill_live_trees` returns."""

    def setUp(self) -> None:
        """Start from an empty registry, so a tracked pid can only be this test's child."""
        self.assertEqual(boundedrun_live.live_pids(), ())

    def test_a_running_tree_is_killed_and_its_run_returns(self) -> None:
        self.kill_a_running_tree()

    def test_a_child_tracked_late_is_killed_only_once_tracked(self) -> None:
        # A loaded host can deschedule the run's thread between `Popen` returning and
        # the child being tracked: the grandchild is recorded, yet `kill_live_trees`
        # finds nothing (bounded_run.md, property 8). Holding the tracking back past the
        # grandchild's record makes that gap certain, so a readiness check that fires
        # inside it fails here every time, not only under load.
        real_track = boundedrun_live.track

        def late_track(pid: int) -> None:
            _await_the_untracked_childs_grandchild()
            window_ends = time.monotonic() + _LATE_TRACK_SECONDS
            while time.monotonic() < window_ends:
                time.sleep(_RECORD_POLL_SECONDS)
            real_track(pid)

        with mock.patch.object(boundedrun_live, "track", late_track):
            self.kill_a_running_tree()

    def kill_a_running_tree(self) -> None:
        """Kill a real fixture tree mid-run once it is ready; assert nothing outlives it."""
        argv = [sys.executable, str(_GRANDCHILD_FIXTURE)]
        results: list[subprocess.CompletedProcess[str]] = []
        with live_pid_guard.record_fixture_pids() as pids:
            runner = threading.Thread(
                target=lambda: results.append(
                    bounded_run.run_bounded(argv, timeout=_FAR_BOUND)
                ),
                daemon=True,
            )
            runner.start()
            give_up = time.monotonic() + _READY_CEILING
            while not _child_tracked(pids) and time.monotonic() < give_up:
                time.sleep(_RECORD_POLL_SECONDS)
            killed = bounded_run.kill_live_trees()
            runner.join(timeout=_READY_CEILING)
            pids.probe_exits()
        self.assertEqual(killed, 1)
        self.assertFalse(runner.is_alive(), "the killed run never returned")
        self.assertNotEqual(results[0].returncode, 0)
        self.assertTrue(pids.child_gone, "the child outlived the kill")
        self.assertTrue(pids.grandchild_gone, "the grandchild outlived the kill")
        self.assertEqual(boundedrun_live.live_pids(), ())


if __name__ == "__main__":
    unittest.main()
