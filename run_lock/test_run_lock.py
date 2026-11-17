#!/usr/bin/env python3
"""Tests for run_lock: contention and probing in one process, then the kill boundary, live.

The ``private=True`` suites sit in ``tests/`` beside this file, to keep each file a reviewable
size; ``load_tests`` below folds them into every run of this one.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
#: The grouped suites, run through ``load_tests`` below.
_SUITE_DIR = _HERE / "tests"
sys.path.insert(0, str(_SUITE_DIR))
sys.path.insert(0, str(_HERE))

import run_lock  # noqa: E402 - import follows the sys.path bootstrap above
from _run_lock_cases import TempDirCase, load_as_windows  # noqa: E402 - in tests/

#: What each grouped file must yield, floored per file and compared as a key set. An empty
#: or half-failed discovery is otherwise a passing run, and a file added with no entry here
#: fails as unfloored. Restated, not derived: a floor computed from discovery would compare
#: discovery against itself and pass for an empty suite.
_MIN_TESTS_BY_FILE: dict[str, int] = {
    "test_private_refusals.py": 14,
    "test_private_symlink.py": 12,
}

#: Upper bound on waiting for the live holder. A safety net for a loaded machine; the child
#: normally reports in well under a second.
_LIVE_CEILING = 30.0

#: The live holder: take the lock, report it through a marker file written atomically (so the
#: parent never reads it half-written), then wait to be killed long before the sleep ends.
_HOLDER = (
    "import sys, time\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import run_lock\n"
    "lock = run_lock.acquire(Path(sys.argv[2]))\n"
    "tmp = Path(sys.argv[3] + '.tmp')\n"
    "tmp.write_text('held' if lock is not None else 'refused', encoding='utf-8', newline='\\n')\n"
    "tmp.replace(sys.argv[3])\n"
    "time.sleep(600)\n"
)


class AcquireTests(TempDirCase):
    def test_a_second_acquire_is_refused_until_the_first_releases(self) -> None:
        """flock belongs to the open file, so contention shows even inside one process."""
        first = run_lock.acquire(self.lock_path)
        assert first is not None
        self.assertIsNone(run_lock.acquire(self.lock_path))
        first.release()
        second = run_lock.acquire(self.lock_path)
        assert second is not None
        second.release()

    def test_the_with_block_releases_on_exit_even_when_it_raises(self) -> None:
        lock = run_lock.acquire(self.lock_path)
        assert lock is not None
        with self.assertRaises(RuntimeError), lock:
            raise RuntimeError("inside the held block")
        self.assertFalse(run_lock.is_held(self.lock_path))

    def test_release_twice_is_a_no_op(self) -> None:
        lock = run_lock.acquire(self.lock_path)
        assert lock is not None
        lock.release()
        lock.release()
        self.assertFalse(run_lock.is_held(self.lock_path))

    def test_the_lock_file_and_its_parent_are_created_and_kept(self) -> None:
        """Never deleted: unlinking races a concurrent opener into a second 'the' lock."""
        lock = run_lock.acquire(self.lock_path)
        assert lock is not None
        lock.release()
        self.assertTrue(self.lock_path.is_file())

    def test_the_lock_file_is_opened_with_the_mode_asked_for(self) -> None:
        """The mode reaches the open itself, the default and an asked one alike."""
        modes: list[int] = []
        real_open = os.open

        def recording_open(path: Path, flags: int, mode: int = 0o777) -> int:
            modes.append(mode)
            return real_open(path, flags, mode)

        with mock.patch.object(run_lock.os, "open", side_effect=recording_open):
            default = run_lock.acquire(self.lock_path)
            assert default is not None
            default.release()
            private = run_lock.acquire(self.lock_path, mode=0o600)
            assert private is not None
            private.release()
        self.assertEqual(modes, [0o644, 0o600])

    @unittest.skipIf(os.name == "nt", "POSIX mode bits are not meaningful on NTFS")
    def test_a_new_lock_file_takes_the_mode_asked_for(self) -> None:
        """A caller keeping private state makes its lock file private too."""
        old_umask = os.umask(0)
        self.addCleanup(os.umask, old_umask)
        lock = run_lock.acquire(self.lock_path, mode=0o600)
        assert lock is not None
        lock.release()
        self.assertEqual(self.lock_path.stat().st_mode & 0o777, 0o600)

    def test_an_error_other_than_contention_raises_and_closes_the_descriptor(
        self,
    ) -> None:
        closed: list[int] = []
        failure = OSError(errno.EIO, "injected")
        with mock.patch.object(run_lock, "_try_lock", side_effect=failure):
            with mock.patch.object(
                run_lock.os, "close", side_effect=_close_recorder(closed)
            ):
                with self.assertRaises(OSError) as caught:
                    run_lock.acquire(self.lock_path)
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(closed), 1, "a failed attempt must hold nothing open")

    def test_a_refused_acquire_closes_its_descriptor(self) -> None:
        """Contention is the common refusal: a launch that meets a running holder takes it."""
        held = run_lock.acquire(self.lock_path)
        assert held is not None
        self.addCleanup(held.release)
        closed: list[int] = []
        with mock.patch.object(
            run_lock.os, "close", side_effect=_close_recorder(closed)
        ):
            self.assertIsNone(run_lock.acquire(self.lock_path))
        self.assertEqual(len(closed), 1, "a refused attempt must hold nothing open")

    def test_an_unlock_that_raises_still_closes_the_descriptor(self) -> None:
        """Closing the descriptor drops the lock, so the next holder is never shut out."""
        lock = run_lock.acquire(self.lock_path)
        assert lock is not None
        fd = lock._fd  # read now: release clears it
        closed: list[int] = []
        # If release ever skips its close, close here, so neither the fd nor its flock leaks.
        self.addCleanup(lambda: None if fd is None or fd in closed else os.close(fd))
        failure = OSError(errno.EIO, "injected")
        with mock.patch.object(run_lock, "_unlock", side_effect=failure):
            with mock.patch.object(
                run_lock.os, "close", side_effect=_close_recorder(closed)
            ):
                with self.assertRaises(OSError) as caught:
                    lock.release()
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(closed), 1, "a failed unlock must still close")
        after = run_lock.acquire(self.lock_path)
        self.assertIsNotNone(after, "the closed descriptor must have dropped the lock")
        assert after is not None
        after.release()


class WindowsTryLockTests(TempDirCase):
    """The win32 ``_try_lock``: contention is False, and any other error propagates."""

    def try_lock_failing_with(self, code: int) -> bool:
        """``_try_lock`` on a real descriptor while ``msvcrt.locking`` raises *code*."""
        failure = OSError(code, os.strerror(code))
        windows = load_as_windows(mock.Mock(side_effect=failure))
        self.lock_path.parent.mkdir(parents=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        self.addCleanup(os.close, fd)
        result: bool = windows._try_lock(fd)
        return result

    def test_a_free_byte_is_locked_and_then_unlocked(self) -> None:
        """Both calls lock the first byte, so each first rewinds to it."""
        positions: list[int] = []

        def record_position(fd: int, _mode: int, _count: int) -> None:
            # msvcrt.locking acts at the current position, so that is what is recorded.
            positions.append(os.lseek(fd, 0, os.SEEK_CUR))

        locking = mock.Mock(side_effect=record_position)
        windows = load_as_windows(locking)
        self.lock_path.parent.mkdir(parents=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        self.addCleanup(os.close, fd)
        os.write(fd, b"moved")
        self.assertTrue(windows._try_lock(fd))
        os.lseek(fd, 3, os.SEEK_SET)
        windows._unlock(fd)
        self.assertEqual(
            locking.call_args_list, [mock.call(fd, 2, 1), mock.call(fd, 0, 1)]
        )
        self.assertEqual(positions, [0, 0])

    def test_contention_reads_as_not_locked(self) -> None:
        self.assertFalse(self.try_lock_failing_with(errno.EACCES))

    def test_an_error_other_than_contention_propagates(self) -> None:
        """A lock the OS refused for another reason is unknown, never "held elsewhere"."""
        with self.assertRaises(OSError) as caught:
            self.try_lock_failing_with(errno.EBADF)
        self.assertEqual(caught.exception.errno, errno.EBADF)


class IsHeldTests(TempDirCase):
    def test_an_absent_file_is_free_and_is_not_created(self) -> None:
        self.assertFalse(run_lock.is_held(self.lock_path))
        self.assertFalse(self.lock_path.exists())

    def test_a_held_lock_reads_held_and_the_probe_does_not_steal_it(self) -> None:
        lock = run_lock.acquire(self.lock_path)
        assert lock is not None
        self.addCleanup(lock.release)
        self.assertTrue(run_lock.is_held(self.lock_path))
        self.assertIsNone(run_lock.acquire(self.lock_path))

    def test_a_released_lock_reads_free_and_the_probe_leaves_it_free(self) -> None:
        lock = run_lock.acquire(self.lock_path)
        assert lock is not None
        lock.release()
        self.assertFalse(run_lock.is_held(self.lock_path))
        again = run_lock.acquire(self.lock_path)
        assert again is not None, "the probe must release what it took"
        again.release()


class LiveKillBoundaryTests(TempDirCase):
    """The property the library exists for: the kernel frees a killed holder's lock."""

    def test_a_killed_holder_leaves_the_lock_free(self) -> None:
        self.lock_path.parent.mkdir(parents=True)
        marker = self.lock_path.parent / "holder.ready"
        here = str(Path(__file__).resolve().parent)
        holder = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, here, str(self.lock_path), str(marker)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(_reap, holder)
        _wait_for(marker, holder)
        self.assertEqual(marker.read_text(encoding="utf-8"), "held")
        self.assertTrue(run_lock.is_held(self.lock_path))
        self.assertIsNone(run_lock.acquire(self.lock_path))
        # SIGKILL on POSIX, TerminateProcess on Windows: the holder gets no chance to clean up.
        holder.kill()
        holder.wait(timeout=_LIVE_CEILING)
        if sys.platform == "win32":
            # Two lags POSIX does not have, so here the lock is polled for rather than read at
            # once. A venv's python.exe is a launcher running the interpreter as its child, so
            # the kill reaches the real holder only through the launcher's job; and Windows
            # frees a dead process's byte-range locks after it exits, not as it exits.
            _wait_until_free(self.lock_path)
        self.assertFalse(run_lock.is_held(self.lock_path))
        lock = run_lock.acquire(self.lock_path)
        assert lock is not None
        lock.release()


def _close_recorder(closed: list[int]) -> Callable[[int], None]:
    """An ``os.close`` stand-in that records each descriptor into *closed*, then closes it.

    Call it before the patch takes effect, so the real ``os.close`` is the one captured.
    """
    real_close = run_lock.os.close

    def record_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    return record_close


def _wait_for(marker: Path, holder: subprocess.Popen[bytes]) -> None:
    """Poll for the holder's marker; fail loudly if it dies first or never reports."""
    deadline = time.monotonic() + _LIVE_CEILING
    while not marker.exists():
        if holder.poll() is not None:
            raise AssertionError(f"the lock holder exited early ({holder.returncode})")
        if time.monotonic() > deadline:
            raise AssertionError("the lock holder never reported holding the lock")
        time.sleep(0.05)


def _wait_until_free(lock_path: Path) -> None:
    """Poll until no holder has *lock_path*; fail loudly if it stays held past the ceiling."""
    deadline = time.monotonic() + _LIVE_CEILING
    while run_lock.is_held(lock_path):
        if time.monotonic() > deadline:
            raise AssertionError("the killed holder's lock was never freed")
        time.sleep(0.05)


def _reap(holder: subprocess.Popen[bytes]) -> None:
    """Kill and wait for the holder if a failed assertion left it running."""
    if holder.poll() is None:
        holder.kill()
    holder.wait(timeout=_LIVE_CEILING)


def _tests_by_module(suite: unittest.TestSuite) -> dict[str, int]:
    """A per-file test count for a suite, however deeply discovery nested it."""
    counts: dict[str, int] = {}
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            for module, count in _tests_by_module(item).items():
                counts[module] = counts.get(module, 0) + count
        else:
            filename = f"{type(item).__module__}.py"
            counts[filename] = counts.get(filename, 0) + 1
    return counts


class GroupedSuiteTest(unittest.TestCase):
    """Fail loudly when a grouped suite is lost from a run of this file."""

    def test_loading_this_module_brings_in_every_grouped_suite(self) -> None:
        # Through ``loadTestsFromModule``, which honors ``load_tests`` as a real run does: a
        # count taken from discovery directly would stay green with the hook deleted.
        this = sys.modules[__name__]
        loaded = _tests_by_module(unittest.TestLoader().loadTestsFromModule(this))
        self.assertGreater(loaded.pop(f"{__name__}.py"), 0)
        self.assertEqual(loaded.keys(), _MIN_TESTS_BY_FILE.keys())
        for filename, floor in _MIN_TESTS_BY_FILE.items():
            with self.subTest(filename=filename):
                self.assertGreaterEqual(loaded[filename], floor)


def load_tests(
    loader: unittest.TestLoader,
    standard_tests: unittest.TestSuite,
    pattern: "str | None",
) -> unittest.TestSuite:
    """unittest's own hook: fold the grouped suites in beside this file's tests."""
    standard_tests.addTests(
        loader.discover(str(_SUITE_DIR), top_level_dir=str(_SUITE_DIR))
    )
    return standard_tests


if __name__ == "__main__":  # pragma: no cover - direct execution entry point
    unittest.main()
