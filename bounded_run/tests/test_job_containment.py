"""Containment wiring: each child joins its job before it runs, and the job is what gets killed.

These run on every host against a fake job API, so they pin the ORDER and the failure handling
around the Windows calls. The calls themselves are pinned by test_kernel_job_api.py.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import unittest
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bounded_run

#: An arbitrary job handle. The fake only echoes it back.
_JOB = 7001

#: The pid the duck-typed child reports.
_CHILD_PID = 4242

#: Bound for a child that finishes at once; generous so a loaded host never expires it.
_QUICK_BOUND = 30.0

#: Bound for the sleeper, which outlives it. The sleep stays under KILL_GRACE_SECONDS, so the
#: post-kill drain completes even though the fake job's terminate kills nothing.
_SHORT_BOUND = 0.5
_SLEEPER_SECONDS = 1.5


class _FakeJobApi:
    """A job API that records each call in order and can make one named call fail."""

    def __init__(self, fail: str | None = None, error: type[OSError] = OSError) -> None:
        self.calls: list[str] = []
        self._fail = fail
        self._error = error

    def _record(self, call: str) -> None:
        self.calls.append(call)
        if call.split()[0] == self._fail:
            raise self._error(f"fake failure: {call}")

    def create_job(self) -> int:
        self._record("create_job")
        return _JOB

    def assign(self, job: int, pid: int) -> None:
        self._record(f"assign {job} {pid}")

    def resume(self, pid: int) -> None:
        self._record(f"resume {pid}")

    def terminate(self, job: int) -> None:
        self._record(f"terminate {job}")

    def close(self, job: int) -> None:
        self._record(f"close {job}")

    def verbs(self) -> list[str]:
        return [call.split()[0] for call in self.calls]


class _FakeChild:
    """The two Popen members containment touches."""

    def __init__(self) -> None:
        self.pid = _CHILD_PID
        self.killed = False

    def kill(self) -> None:
        self.killed = True


@contextlib.contextmanager
def _faked_containment(api: _FakeJobApi) -> Iterator[None]:
    """Route every spawn through *api*; start children unsuspended, as the fake resumes nothing."""
    with (
        mock.patch.object(bounded_run, "_job_api_factory", lambda: api),
        mock.patch.object(
            bounded_run, "spawn_creationflags", lambda **_flags_inputs: 0
        ),
    ):
        yield


class ContainTests(unittest.TestCase):
    def test_assigns_the_suspended_child_before_letting_it_run(self) -> None:
        api, child = _FakeJobApi(), _FakeChild()
        job = bounded_run._contain(child, api)  # type: ignore[arg-type]  # duck-typed Popen
        self.assertEqual(job, _JOB)
        self.assertEqual(
            api.calls,
            ["create_job", f"assign {_JOB} {_CHILD_PID}", f"resume {_CHILD_PID}"],
            "a child resumed before it joins the job can start a grandchild outside it",
        )
        self.assertFalse(child.killed)

    def test_a_failed_step_kills_the_child_and_closes_what_it_made(self) -> None:
        expected = {
            "create_job": ["create_job"],
            "assign": ["create_job", f"assign {_JOB} {_CHILD_PID}", f"close {_JOB}"],
            "resume": [
                "create_job",
                f"assign {_JOB} {_CHILD_PID}",
                f"resume {_CHILD_PID}",
                f"close {_JOB}",
            ],
        }
        for step, calls in expected.items():
            with self.subTest(failing=step):
                api, child = _FakeJobApi(fail=step), _FakeChild()
                with self.assertRaises(OSError):
                    bounded_run._contain(child, api)  # type: ignore[arg-type]  # duck-typed Popen
                self.assertEqual(api.calls, calls)
                self.assertTrue(
                    child.killed,
                    "a child left suspended would sit out the whole timeout",
                )


class JobRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(bounded_run._JOBS, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_platform_without_a_job_api_registers_nothing(self) -> None:
        with mock.patch.object(bounded_run, "_job_api_factory", None):
            bounded_run._start_contained(_FakeChild())  # type: ignore[arg-type]  # duck-typed Popen
        self.assertEqual(bounded_run._JOBS, {})

    def test_kill_tree_terminates_a_registered_job_instead_of_signaling(self) -> None:
        api = _FakeJobApi()
        bounded_run._JOBS[_CHILD_PID] = (api, _JOB)
        with (
            mock.patch.object(os, "killpg", create=True) as killpg,
            mock.patch.object(subprocess, "run") as run,
        ):
            bounded_run.kill_tree(_CHILD_PID)
        self.assertEqual(api.calls, [f"terminate {_JOB}"])
        killpg.assert_not_called()
        run.assert_not_called()

    def test_kill_tree_tolerates_a_job_member_it_may_not_terminate(self) -> None:
        # The counterpart of the POSIX arm's EPERM: the survivor keeps the pipes, and the bounded
        # re-read then expires without output instead of the kill raising past TimeoutExpired.
        api = _FakeJobApi(fail="terminate", error=PermissionError)
        bounded_run._JOBS[_CHILD_PID] = (api, _JOB)
        bounded_run.kill_tree(_CHILD_PID)
        self.assertEqual(api.calls, [f"terminate {_JOB}"])

    def test_release_closes_and_forgets_the_job_exactly_once(self) -> None:
        api = _FakeJobApi()
        bounded_run._JOBS[_CHILD_PID] = (api, _JOB)
        bounded_run._release(_CHILD_PID)
        bounded_run._release(_CHILD_PID)
        self.assertEqual(api.calls, [f"close {_JOB}"])
        self.assertNotIn(_CHILD_PID, bounded_run._JOBS)


class ContainmentWiringTests(unittest.TestCase):
    """Real children, fake jobs: the entry points must actually call the containment path."""

    def test_each_entry_point_contains_its_child_and_releases_the_job(self) -> None:
        argv = [sys.executable, "-c", "pass"]
        entry_points: dict[str, Callable[[], object]] = {
            "run_bounded": lambda: bounded_run.run_bounded(argv, timeout=_QUICK_BOUND),
            "run_bounded_bytes": lambda: bounded_run.run_bounded_bytes(
                argv, timeout=_QUICK_BOUND
            ),
        }
        for name, run in entry_points.items():
            with self.subTest(entry_point=name):
                api = _FakeJobApi()
                with _faked_containment(api):
                    run()
                self.assertEqual(
                    api.verbs(), ["create_job", "assign", "resume", "close"]
                )
                self.assertEqual(
                    api.calls[1].split()[2],
                    api.calls[2].split()[1],
                    "the pid resumed is not the pid assigned",
                )
                self.assertEqual(bounded_run._JOBS, {}, "the job outlived its run")

    def test_a_timed_out_run_is_killed_through_the_job_it_registered(self) -> None:
        api = _FakeJobApi()
        argv = [sys.executable, "-c", f"import time; time.sleep({_SLEEPER_SECONDS})"]
        with _faked_containment(api), self.assertRaises(subprocess.TimeoutExpired):
            bounded_run.run_bounded(argv, timeout=_SHORT_BOUND)
        self.assertEqual(
            api.verbs(),
            ["create_job", "assign", "resume", "terminate", "close"],
            "terminate proves the job was registered before the kill looked it up; close "
            "last proves the release still ran after TimeoutExpired was raised",
        )
        self.assertEqual(bounded_run._JOBS, {})


if __name__ == "__main__":
    unittest.main()
