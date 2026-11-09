#!/usr/bin/env python3
"""Tests for bounded_run: the deterministic contract, then the live boundary."""

from __future__ import annotations

import contextlib
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
# live_pid_guard is the shared record/probe/cleanup for the live grandchild tests below.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "live_pid_guard"))

import bounded_run
import live_pid_guard


class _FakeProc:
    """A Popen stand-in whose communicate() can be scripted to expire."""

    def __init__(self, *, expiries: int, returncode: int = 0) -> None:
        self._expiries = expiries
        self.calls = 0
        self.pid = 4242
        self.returncode = returncode
        # Ordered record of every read. The ordering test injects a kill seam that appends to
        # this same list, so the sequence is observable; counting the two events separately
        # cannot distinguish kill-then-reread from reread-then-kill.
        self.call_log: list[str] = []

    def communicate(  # noqa: A002 - `input` mirrors Popen's parameter name
        self, timeout: float | None = None, input: str | None = None
    ) -> tuple[str, str]:
        self.calls += 1
        self.call_log.append("communicate")
        if self.calls <= self._expiries:
            raise subprocess.TimeoutExpired(["fake"], timeout or 0.0)
        return "out", "err"


class CommunicateBoundedTests(unittest.TestCase):
    def test_returns_streams_when_the_child_finishes(self) -> None:
        proc = _FakeProc(expiries=0)
        killed: list[int] = []
        # _FakeProc is a deliberate duck-typed stand-in: it supplies the only two members
        # _communicate_bounded touches (communicate/pid), but is not a real Popen.
        out, err = bounded_run._communicate_bounded(
            proc,  # type: ignore[arg-type]
            ["fake"],
            1.0,
            killed.append,
        )
        self.assertEqual((out, err), ("out", "err"))
        self.assertEqual(killed, [], "no kill when the child finished in time")

    def test_kills_the_tree_then_raises_with_partial_output(self) -> None:
        proc = _FakeProc(expiries=1)
        killed: list[int] = []

        def record_kill(pid: int) -> None:
            """Record the kill into the fake's own read log, so order is what gets asserted."""
            killed.append(pid)
            proc.call_log.append("kill")

        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            bounded_run._communicate_bounded(
                proc,  # type: ignore[arg-type]  # duck-typed Popen stand-in
                ["fake"],
                1.0,
                record_kill,
            )
        self.assertEqual(killed, [4242], "the kill targets the child's own pid")
        self.assertEqual(
            proc.call_log,
            ["communicate", "kill", "communicate"],
            "the tree kill must land between the expiring read and the re-read: an "
            "implementation that re-read first and killed afterward satisfies every "
            "other assertion here, and is the hang this module exists to remove",
        )
        self.assertEqual(caught.exception.output, "out")
        self.assertEqual(caught.exception.stderr, "err")

    def test_abandons_output_when_the_kill_does_not_free_the_pipes(self) -> None:
        proc = _FakeProc(expiries=2)
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            bounded_run._communicate_bounded(
                proc,  # type: ignore[arg-type]  # duck-typed Popen stand-in
                ["fake"],
                1.0,
                lambda _pid: None,
            )
        self.assertIsNone(caught.exception.output, "fail honestly rather than block")


class RunBoundedTests(unittest.TestCase):
    def test_returns_a_completed_process_for_a_leaf_child(self) -> None:
        done = bounded_run.run_bounded(
            [sys.executable, "-c", "print('hi')"], timeout=30.0
        )
        self.assertEqual(done.returncode, 0)
        self.assertIn("hi", done.stdout)

    def test_bytes_variant_feeds_stdin_and_returns_bytes(self) -> None:
        done = bounded_run.run_bounded_bytes(
            [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
            timeout=30.0,
            stdin_bytes=b"payload",
        )
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout, b"payload")

    def test_bytes_variant_opens_no_stdin_pipe_when_given_none(self) -> None:
        # Covers the false arm of run_bounded_bytes' `stdin=PIPE if ... else None`. Without
        # this, branch coverage misses it: every other bytes-path test passes stdin_bytes.
        done = bounded_run.run_bounded_bytes(
            [sys.executable, "-c", "print('no stdin')"], timeout=30.0
        )
        self.assertEqual(done.returncode, 0)
        self.assertIn(b"no stdin", done.stdout)

    def test_feeds_text_stdin_and_decodes_the_echo(self) -> None:
        # A hook driven as `bash <script>` reading its transaction from stdin is a real shape,
        # and its input is text: forcing such a caller through the bytes variant would make it
        # hand-roll encode/decode, which is the duplication this seam exists to avoid.
        done = bounded_run.run_bounded(
            [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
            timeout=30.0,
            stdin_text="a b c\n",
        )
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout, "a b c\n")

    def test_opens_no_stdin_pipe_when_text_stdin_is_none(self) -> None:
        # The false arm of run_bounded's own `stdin=PIPE if ... else DEVNULL`: a child given no
        # stdin must not get a pipe nobody writes to, which would leave it blocking on a read
        # that never completes. DEVNULL is what avoids that; see the leak test below for why
        # inheriting is not the alternative it once looked like.
        done = bounded_run.run_bounded(
            [sys.executable, "-c", "print('no stdin')"], timeout=30.0
        )
        self.assertEqual(done.returncode, 0)
        self.assertIn("no stdin", done.stdout)

    def test_does_not_leak_the_callers_stdin_to_the_child(self) -> None:
        # The EL9 failure this guards: `dnf` reads whatever stdin it inherits. A probe launched
        # from a script delivered on stdin (`ssh host "bash -s" < script`, `curl ... | bash`)
        # therefore swallowed the rest of that script, and the shell exited 0: a silent
        # truncation with no error anywhere. A capturing run pipes both stdout and stderr, so the
        # child cannot be interactive regardless; DEVNULL hands it an immediate EOF, which is
        # neither inheritance nor a pipe nobody writes to.
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"THE CALLERS NEXT SCRIPT LINE")
        os.close(write_fd)
        saved_stdin = os.dup(0)
        try:
            os.dup2(read_fd, 0)
            done = bounded_run.run_bounded(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.write(sys.stdin.read())",
                ],
                timeout=30.0,
            )
        finally:
            os.dup2(saved_stdin, 0)
            os.close(saved_stdin)
            os.close(read_fd)
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout, "", "the child consumed the caller stdin")


#: Bound for the live test. Small enough to keep the suite quick, large enough that a loaded
#: machine does not expire it spuriously before the child is even up.
_LIVE_TIMEOUT = 3.0

#: Ceiling asserted against. The measured fixed path took 3.97s for a 3.0s bound; the unfixed
#: path did not return within 300s. Anything under this proves the tree kill worked.
_LIVE_CEILING = 30.0

#: How often a pid file the child has yet to write is re-read.
_RECORD_POLL_SECONDS = 0.05

#: The shared fixture whose child and grandchild do not exit for an hour.
_GRANDCHILD_FIXTURE = Path(__file__).resolve().parent / "samples" / "gc_child.py"


def _await_recorded_pid(pid_file: Path) -> int:
    """The pid a live child records in *pid_file*, polled for until the live bound passes."""
    deadline = time.monotonic() + _LIVE_TIMEOUT
    while time.monotonic() < deadline:
        with contextlib.suppress(FileNotFoundError, ValueError):
            return int(pid_file.read_text(encoding="utf-8"))
        time.sleep(_RECORD_POLL_SECONDS)
    return live_pid_guard.require_recorded_pid(pid_file)


class LiveGrandchildBoundaryTests(unittest.TestCase):
    """The real boundary: a grandchild holding the pipes must not outlive the bound."""

    def test_bound_holds_when_a_grandchild_inherits_the_pipes(self) -> None:
        argv = [sys.executable, str(_GRANDCHILD_FIXTURE)]
        # The fixture's child and grandchild do not exit for an hour, so the guard records both
        # pids: a tree kill that regressed would otherwise leave the pair running for good.
        with live_pid_guard.record_fixture_pids() as pids:
            start = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                bounded_run.run_bounded(argv, timeout=_LIVE_TIMEOUT)
            elapsed = time.monotonic() - start
            # Probed before the assertion, so its failure cannot skip the probe and leave
            # cleanup signaling a pid that is already gone.
            pids.probe_exits()
        self.assertLess(
            elapsed,
            _LIVE_CEILING,
            f"the bound did not hold: {elapsed:.1f}s for a "
            f"{_LIVE_TIMEOUT}s timeout. "
            "A surviving grandchild is holding the capture pipes.",
        )


class GrandchildFixtureArgvTests(unittest.TestCase):
    """The fixture must ignore an argv it did not choose.

    A tool under test may launch it as its own subprocess and append flags of its own, so its
    command line reads ``gc_child.py --path <dir>``. While its pid files were positional it took
    those flags for them: it wrote its pid into a file named ``--path`` beside the caller, then
    crashed writing to ``<dir>``, so the wedged direct child those tests model had already
    exited, and every one of their assertions still passed.
    """

    def test_an_appended_argv_is_neither_written_to_nor_fatal(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            live_pid_guard.record_fixture_pids() as pids,
        ):
            cwd = Path(tmp)
            argv = [sys.executable, str(_GRANDCHILD_FIXTURE), "--path", tmp]
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                bounded_run.run_bounded(argv, timeout=_LIVE_TIMEOUT, cwd=cwd)
            stray = sorted(entry.name for entry in cwd.iterdir())
            pids.probe_exits()
        self.assertEqual(stray, [], "the fixture wrote a file beside its caller")
        self.assertFalse(
            caught.exception.stderr,
            "the fixture wrote to stderr; a traceback means it crashed before the bound",
        )


#: Beyond every platform's pid ceiling, so even a kill that escaped its patch signals nothing.
_UNSIGNALLABLE_PID = 2**31 - 1


class KillTreePosixTests(unittest.TestCase):
    """The POSIX kill addresses the group by the leader's own pid, never through a lookup.

    The pid is beyond any real one, so the live process table, read before any signal, lists
    nothing in its group: there is nothing to freeze, and the kill is the only call.
    """

    def test_signals_the_group_by_pid_and_tolerates_a_group_already_gone(self) -> None:
        if sys.platform == "win32":
            # skipTest never returns, so mypy's win32 pass stops here and the POSIX-only
            # signal.SIGKILL below still type-checks on that platform.
            self.skipTest("the process-group kill is POSIX; win32 kills through a job")
        # Both errors measured on macOS once no live member remains: killpg reports ESRCH
        # for a reaped group and EPERM for one whose only member is the exited, unreaped
        # leader. The getpgid stub raises what macOS raises for that leader, so a kill that
        # still went through the lookup fails here.
        for gone in (ProcessLookupError, PermissionError):
            with (
                self.subTest(error=gone.__name__),
                mock.patch.object(
                    os, "getpgid", side_effect=ProcessLookupError
                ) as getpgid,
                mock.patch.object(os, "killpg", side_effect=gone) as killpg,
            ):
                bounded_run.kill_tree(_UNSIGNALLABLE_PID)
                self.assertEqual(
                    killpg.call_args_list,
                    [mock.call(_UNSIGNALLABLE_PID, signal.SIGKILL)],
                )
                getpgid.assert_not_called()


class KillTreeGuardTests(unittest.TestCase):
    """``kill_tree`` refuses a pid no child can have, before either platform's kill."""

    def test_a_pid_below_two_is_refused_with_nothing_killed(self) -> None:
        """Regression: pid 0 reached ``killpg(0)``, the caller's own group, and 1 is init."""
        for platform in ("darwin", "linux", "win32"):
            for pid in (0, 1, -1):
                with (
                    self.subTest(platform=platform, pid=pid),
                    mock.patch.object(sys, "platform", platform),
                    mock.patch.object(
                        bounded_run.boundedrun_posix_tree, "kill_tree_posix"
                    ) as posix_kill,
                    mock.patch.object(bounded_run.subprocess, "run") as taskkill,
                ):
                    with self.assertRaises(ValueError):
                        bounded_run.kill_tree(pid)
                    posix_kill.assert_not_called()
                    taskkill.assert_not_called()


class ArgvTypeTests(unittest.TestCase):
    """A string argv is refused: ``list("ls -l")`` would run a program named ``l``."""

    def test_a_str_or_bytes_argv_raises_type_error_before_spawning(self) -> None:
        for argv in ("echo hi", b"echo hi"):
            with (
                self.subTest(argv=argv),
                mock.patch.object(bounded_run.subprocess, "Popen") as popen,
            ):
                with self.assertRaises(TypeError):
                    bounded_run.run_bounded(argv, timeout=1.0)  # type: ignore[arg-type]
                with self.assertRaises(TypeError):
                    bounded_run.run_bounded_bytes(argv, timeout=1.0)  # type: ignore[arg-type]
                popen.assert_not_called()


#: How long the exited-leader grandchild sleeps. Every assertion lands well inside it, so a
#: grandchild found gone was killed rather than having finished on its own.
_GRANDCHILD_LIFETIME = 20.0

#: The exited-leader bound. The child exits as soon as the grandchild is started, but its
#: interpreter start, imports and Popen must all come first (a kill that lands earlier leaves
#: no grandchild to test), so the bound leaves a loaded machine room for them.
_EXITED_LEADER_TIMEOUT = 3.0

#: Above the bound plus the kill plus the post-kill drain, below the grandchild's lifetime.
_EXITED_LEADER_CEILING = 10.0

#: The child starts a grandchild that inherits its stdout/stderr, writes the grandchild's pid
#: to the file named by argv[1] (Popen knows it at once), and exits.
_EXITING_LEADER = (
    "import pathlib, subprocess, sys; "
    "grandchild = subprocess.Popen("
    f"[sys.executable, '-c', 'import time; time.sleep({_GRANDCHILD_LIFETIME:g})']); "
    "pathlib.Path(sys.argv[1]).write_text("
    "str(grandchild.pid), encoding='utf-8', newline='\\n')"
)


class LiveExitedLeaderTests(unittest.TestCase):
    """The leader exits at once, leaving a grandchild holding the pipes in its group or job.

    This is the regression test for both platforms' exited-leader defect: through a
    ``getpgid`` lookup on macOS, and through ``taskkill /T`` on Windows, whose parent-pid walk
    cannot start from a leader that no longer exists.
    """

    def test_bound_holds_when_the_leader_exits_before_its_grandchild(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "grandchild.pid"
            argv = [sys.executable, "-c", _EXITING_LEADER, str(pid_file)]
            grandchild_gone = False
            try:
                start = time.monotonic()
                with self.assertRaises(subprocess.TimeoutExpired):
                    bounded_run.run_bounded(argv, timeout=_EXITED_LEADER_TIMEOUT)
                elapsed = time.monotonic() - start
                grandchild_pid = live_pid_guard.require_recorded_pid(pid_file)
                # Probed before either assertion, for the same reason as the tests above.
                grandchild_gone = live_pid_guard.gone_within(
                    grandchild_pid, live_pid_guard.REAP_DEADLINE_SECONDS
                )
                self.assertLess(elapsed, _EXITED_LEADER_CEILING)
                self.assertTrue(
                    grandchild_gone, "the grandchild outlived the tree kill"
                )
            finally:
                live_pid_guard.kill_unless_seen_gone(
                    pid_file, seen_gone=grandchild_gone
                )


class LiveUnregisteredTreeTests(unittest.TestCase):
    """kill_tree on a pid this module did not start falls back to the platform's own tree kill.

    On Windows this is the only path that still reaches ``taskkill``: every child run_bounded
    starts is killed through its job instead.
    """

    def test_kills_a_live_tree_it_did_not_start(self) -> None:
        with live_pid_guard.record_fixture_pids() as pids:
            # A new session makes the child a group leader, which the POSIX fallback requires;
            # the Windows fallback, taskkill /T, needs the child alive instead, as it is here.
            child = subprocess.Popen(
                [sys.executable, str(_GRANDCHILD_FIXTURE)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                # The grandchild must exist before the kill, or nothing about a tree is tested.
                _await_recorded_pid(pids.grandchild)
                bounded_run.kill_tree(child.pid)
                child.wait(timeout=live_pid_guard.REAP_DEADLINE_SECONDS)
                pids.probe_exits()
                self.assertTrue(
                    pids.grandchild_gone, "the fallback left the grandchild running"
                )
            finally:
                # Reaped through its own handle (Popen never signals a child it has already
                # reaped), so the guard's cleanup must not signal that pid a second time.
                child.kill()
                child.wait(timeout=live_pid_guard.REAP_DEADLINE_SECONDS)
                pids.child_gone = True


#: The grouped suites, split out to keep this file to a reviewable length. They run through
#: load_tests below, so the coverage run of this file covers them.
_TESTS = Path(__file__).resolve().parent / "tests"

#: Per-file floors. A test deleted from a grouped suite drops its file below its floor and fails
#: here, rather than the suite shrinking unnoticed. Raise a floor when adding tests.
_MIN_GROUPED_TESTS_BY_FILE = {
    "test_drain_clock.py": 15,
    "test_drain_entry_window.py": 3,
    "test_drain_interrupt.py": 6,
    "test_fixture_lifetime.py": 2,
    "test_inherit_stderr.py": 2,
    "test_job_containment.py": 8,
    "test_kernel_job_api.py": 13,
    "test_live_registry.py": 11,
    "test_nested_tree_kill.py": 1,
    "test_posix_tree.py": 29,
    "test_spawn_flags.py": 8,
    "test_spawn_window.py": 10,
}


def _grouped_suite(pattern: str = "test_*.py") -> unittest.TestSuite:
    """Discover the grouped suites under tests/, imported as top-level modules."""
    return unittest.TestLoader().discover(
        str(_TESTS), pattern=pattern, top_level_dir=str(_TESTS)
    )


class GroupedSuiteTests(unittest.TestCase):
    def test_every_grouped_file_is_discovered_and_meets_its_floor(self) -> None:
        found = sorted(path.name for path in _TESTS.glob("test_*.py"))
        self.assertEqual(found, sorted(_MIN_GROUPED_TESTS_BY_FILE))
        for filename, floor in _MIN_GROUPED_TESTS_BY_FILE.items():
            with self.subTest(file=filename):
                self.assertGreaterEqual(
                    _grouped_suite(filename).countTestCases(), floor
                )


def load_tests(
    _loader: unittest.TestLoader, tests: unittest.TestSuite, _pattern: str | None
) -> unittest.TestSuite:
    """unittest's hook: run this module's own tests plus every grouped suite."""
    return unittest.TestSuite([tests, _grouped_suite()])


if __name__ == "__main__":
    unittest.main()
