#!/usr/bin/env python3
"""Tests for live_pid_guard, against REAL processes wherever a pid is probed or signaled.

A faked ``os.kill`` could prove only that calls were made. What the module promises is that a
process seen gone is left alone and a survivor is really killed, and only a real process shows
either -- so those cases run on every platform, each against that platform's own probe and kill.
The choice of signal is additionally pinned under a patched platform, because an exit code
alone does not show which signal a kill sent.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import live_pid_guard as lpg  # noqa: E402 - import follows the sys.path bootstrap above

#: A child that records its own pid in the file the named variable points at, then sleeps well
#: past every deadline here -- so one found gone was killed, not finished.
_RECORDING_SLEEPER = (
    "import os, pathlib, sys, time; "
    "pathlib.Path(os.environ[sys.argv[1]]).write_text(str(os.getpid()), encoding='utf-8'); "
    "time.sleep(60)"
)

#: A child that sleeps past every deadline here, recording nothing.
_SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]

#: A reap bound for tests that WANT a live pid to be reported not-gone, so they do not spend
#: the module's full deadline polling it.
_SHORT_DEADLINE_SECONDS = 0.3

#: How long to wait for a child this suite started to exit, or to record its pid.
_CHILD_WAIT_SECONDS = 10.0
_RECORD_POLL_SECONDS = 0.02


def _killed_exit_code() -> int:
    """The exit status a process killed by kill_unless_seen_gone reports on this platform."""
    if sys.platform == "win32":
        # os.kill(pid, SIGTERM) is TerminateProcess(handle, SIGTERM): the exit code is 15.
        return int(signal.SIGTERM)
    return -signal.SIGKILL


def _start(test: unittest.TestCase, argv: list[str]) -> subprocess.Popen[bytes]:
    """Start *argv* and register a cleanup that reaps it, so no case leaks a process.

    ``Popen.kill`` is safe against pid reuse where a bare ``os.kill`` is not: it polls first,
    and a child it has already reaped is never signaled.
    """
    proc = subprocess.Popen(argv)

    def reap() -> None:
        proc.kill()
        proc.wait(timeout=_CHILD_WAIT_SECONDS)

    test.addCleanup(reap)
    return proc


def _reaped_pid(test: unittest.TestCase) -> int:
    """The pid of a child that has already exited and been reaped."""
    proc = _start(test, [sys.executable, "-c", "pass"])
    proc.wait(timeout=_CHILD_WAIT_SECONDS)
    return proc.pid


def _write_pid(pid_file: Path, text: str) -> None:
    """Write a pid record exactly as the fixture does: UTF-8, LF, no translation."""
    pid_file.write_text(text, encoding="utf-8", newline="\n")


def _wait_for_record(pid_file: Path) -> None:
    """Poll until *pid_file* holds a whole pid; the child writes it only once it is running."""
    for _ in range(int(_CHILD_WAIT_SECONDS / _RECORD_POLL_SECONDS)):
        if pid_file.is_file() and pid_file.read_text(encoding="utf-8").isdigit():
            return
        # The poll interval of a loop bounded by its iteration count, not a settle delay.
        time.sleep(_RECORD_POLL_SECONDS)
    raise AssertionError(f"no pid was recorded in {pid_file}")


class RequireRecordedPidTests(unittest.TestCase):
    def test_returns_the_recorded_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "child.pid"
            _write_pid(pid_file, "4242")
            self.assertEqual(lpg.require_recorded_pid(pid_file), 4242)

    def test_a_missing_or_partial_record_is_an_assertion_naming_the_cause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.pid"
            partial = Path(tmp) / "partial.pid"
            _write_pid(partial, "")
            for pid_file in (missing, partial):
                with self.subTest(pid_file=pid_file.name):
                    with self.assertRaises(AssertionError) as caught:
                        lpg.require_recorded_pid(pid_file)
                    message = str(caught.exception)
                    self.assertIn(
                        "did not start the grandchild before the bound", message
                    )
                    self.assertIn(pid_file.name, message)

    def test_a_record_below_two_is_refused_as_no_child_of_ours(self) -> None:
        """Regression: ``int(" 0\\n")`` read as pid 0, which ``os.kill`` takes as the caller's
        own process group, and 1 is init/launchd. Neither can be a fixture this suite started."""
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "child.pid"
            for text in (" 0\n", "1", "-1", "-4242"):
                with self.subTest(text=text):
                    _write_pid(pid_file, text)
                    with self.assertRaises(AssertionError) as caught:
                        lpg.require_recorded_pid(pid_file)
                    self.assertIn("not a pid a fixture records", str(caught.exception))
                    self.assertIn(pid_file.name, str(caught.exception))


class ProbeTests(unittest.TestCase):
    def test_a_live_process_is_running_and_the_probe_leaves_it_alone(self) -> None:
        proc = _start(self, _SLEEPER)
        self.assertTrue(lpg.is_running(proc.pid))
        self.assertFalse(lpg.gone_within(proc.pid, _SHORT_DEADLINE_SECONDS))
        self.assertIsNone(proc.poll(), "probing must not have disturbed the process")

    def test_a_reaped_process_is_not_running_and_is_gone(self) -> None:
        pid = _reaped_pid(self)
        self.assertFalse(lpg.is_running(pid))
        self.assertTrue(lpg.gone_within(pid, _CHILD_WAIT_SECONDS))

    @unittest.skipIf(
        sys.platform == "darwin" and sys.version_info < (3, 13),
        "os.waitid reached macOS only in Python 3.13; below that a zombie reads as running",
    )
    def test_an_exited_unreaped_child_is_gone_and_left_to_its_owner(self) -> None:
        # A killed child of this process holds its pid as a zombie until it is reaped,
        # and kill(pid, 0) succeeds on a zombie: the probe must not wait for a reap that
        # only the probing process itself would ever do.
        proc = _start(self, _SLEEPER)
        proc.kill()
        self.assertTrue(lpg.gone_within(proc.pid, _CHILD_WAIT_SECONDS))
        if sys.platform == "win32":
            # Popen.kill there is TerminateProcess(handle, 1).
            killed = 1  # pragma: only-on win32
        else:  # pragma: only-on posix
            killed = -signal.SIGKILL
        # The probe saw the exit without reaping it, so the owner still collects the
        # real status rather than Popen's stand-in 0 for a child someone else reaped.
        self.assertEqual(proc.wait(timeout=_CHILD_WAIT_SECONDS), killed)


class SeenGoneTests(unittest.TestCase):
    def test_the_pid_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(AssertionError):
                lpg.seen_gone(Path(tmp) / "missing.pid")

    def test_a_reaped_recorded_pid_is_seen_gone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "child.pid"
            _write_pid(pid_file, str(_reaped_pid(self)))
            self.assertTrue(lpg.seen_gone(pid_file))


class KillUnlessSeenGoneTests(unittest.TestCase):
    def test_a_survivor_not_seen_gone_is_killed(self) -> None:
        proc = _start(self, _SLEEPER)
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "child.pid"
            _write_pid(pid_file, str(proc.pid))
            lpg.kill_unless_seen_gone(pid_file, seen_gone=False)
        self.assertEqual(proc.wait(timeout=_CHILD_WAIT_SECONDS), _killed_exit_code())

    def test_a_pid_seen_gone_is_never_signaled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "child.pid"
            _write_pid(pid_file, "4242")
            with mock.patch.object(os, "kill") as kill:
                lpg.kill_unless_seen_gone(pid_file, seen_gone=True)
            kill.assert_not_called()

    def test_windows_terminates_with_sigterm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "child.pid"
            _write_pid(pid_file, "4242")
            with (
                mock.patch.object(sys, "platform", "win32"),
                mock.patch.object(os, "kill") as kill,
            ):
                lpg.kill_unless_seen_gone(pid_file, seen_gone=False)
            kill.assert_called_once_with(4242, signal.SIGTERM)

    def test_a_missing_record_or_a_pid_already_gone_is_tolerated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(os, "kill") as kill:
                lpg.kill_unless_seen_gone(Path(tmp) / "missing.pid", seen_gone=False)
            kill.assert_not_called()
            gone = Path(tmp) / "gone.pid"
            _write_pid(gone, "4242")
            # ESRCH as POSIX reports a vanished pid, and the bare OSError Windows raises.
            for error in (ProcessLookupError(), OSError(22, "invalid parameter")):
                with self.subTest(error=type(error).__name__):
                    with mock.patch.object(os, "kill", side_effect=error) as kill:
                        lpg.kill_unless_seen_gone(gone, seen_gone=False)
                    kill.assert_called_once()

    def test_a_record_below_two_is_never_signaled(self) -> None:
        """Regression: a record of 0 sent SIGKILL to the runner's own process group."""
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "child.pid"
            for text in (" 0\n", "1", "-1"):
                with self.subTest(text=text):
                    _write_pid(pid_file, text)
                    with mock.patch.object(os, "kill") as kill:
                        lpg.kill_unless_seen_gone(pid_file, seen_gone=False)
                    kill.assert_not_called()


class RecordFixturePidsTests(unittest.TestCase):
    def _snapshot(self) -> dict[str, str | None]:
        """Both variables as they stand now."""
        return {
            name: os.environ.get(name)
            for name in (lpg.CHILD_PID_ENV, lpg.GRANDCHILD_PID_ENV)
        }

    def test_a_block_that_never_probes_fails_loudly_and_restores_the_environment(
        self,
    ) -> None:
        before = self._snapshot()
        with self.assertRaises(AssertionError) as caught:
            with lpg.record_fixture_pids() as pids:
                self.assertEqual(os.environ[lpg.CHILD_PID_ENV], str(pids.child))
                self.assertEqual(
                    os.environ[lpg.GRANDCHILD_PID_ENV], str(pids.grandchild)
                )
        self.assertIn("probe_exits", str(caught.exception))
        self.assertEqual(self._snapshot(), before)

    def test_a_probed_block_ends_quietly(self) -> None:
        with (
            mock.patch.object(lpg, "seen_gone", return_value=True),
            mock.patch.object(os, "kill") as kill,
        ):
            with lpg.record_fixture_pids() as pids:
                pids.probe_exits()
        self.assertTrue(pids.child_gone and pids.grandchild_gone)
        kill.assert_not_called()

    def test_an_error_in_the_block_propagates_unchanged(self) -> None:
        with self.assertRaises(KeyError):
            with lpg.record_fixture_pids():
                raise KeyError("the call under test failed")

    def test_cleanup_kills_the_survivor_and_leaves_the_pid_seen_gone_alone(
        self,
    ) -> None:
        before = self._snapshot()
        with mock.patch.object(os, "kill", wraps=os.kill) as kill:
            with (
                mock.patch.object(
                    lpg, "REAP_DEADLINE_SECONDS", _SHORT_DEADLINE_SECONDS
                ),
                lpg.record_fixture_pids() as pids,
            ):
                # Each child reads the variable the guard exported, as the real fixture does.
                exits = _start(
                    self, [sys.executable, "-c", _RECORDING_SLEEPER, lpg.CHILD_PID_ENV]
                )
                survivor = _start(
                    self,
                    [sys.executable, "-c", _RECORDING_SLEEPER, lpg.GRANDCHILD_PID_ENV],
                )
                _wait_for_record(pids.child)
                _wait_for_record(pids.grandchild)
                # The pid the guard reads, which is not always Popen's: on Windows a venv's
                # python.exe is a launcher, and the pid recorded is its interpreter child's.
                survivor_pid = lpg.require_recorded_pid(pids.grandchild)
                if sys.platform != "win32":
                    # Off Windows there is no launcher, so the recorded pid must be the
                    # very process this test started: the guard signals nothing else.
                    self.assertEqual(survivor_pid, survivor.pid)
                exits.kill()
                exits.wait(timeout=_CHILD_WAIT_SECONDS)
                # The spy also saw the kill just above; only what follows is the guard's.
                guard_calls_start = len(kill.call_args_list)
                pids.probe_exits()
                self.assertTrue(pids.child_gone)
                self.assertFalse(pids.grandchild_gone)
        self.assertEqual(
            survivor.wait(timeout=_CHILD_WAIT_SECONDS), _killed_exit_code()
        )
        guard_calls = kill.call_args_list[guard_calls_start:]
        killed = [call.args[0] for call in guard_calls if call.args[1] != 0]
        self.assertEqual(killed, [survivor_pid], "only the survivor may be signaled")
        self.assertEqual(self._snapshot(), before)


if __name__ == "__main__":
    unittest.main()
