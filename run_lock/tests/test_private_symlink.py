"""Grouped suite: ``private=True`` refuses a symlink at the lock path, creates privately, and
is refused outright on Windows.

The symlink tests point a symlink at a REAL, separate file: a self-loop symlink fails with
ELOOP whether or not the open asks for O_NOFOLLOW, and would prove nothing.

Entry point assumptions: loaded by the folder's aggregator, or run alone. The insert below
reaches the subject one folder up, never the working directory.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run_lock  # noqa: E402 - import follows the sys.path bootstrap above
from _run_lock_cases import (  # noqa: E402 - beside this module
    USUAL_UMASK,
    TempDirCase,
    load_as_windows,
)

#: A backdated mtime for the symlink's target, so a write that touched it would show.
_OLD_MTIME = 1_000_000_000


@unittest.skipIf(sys.platform == "win32", "private=True is POSIX-only")
class SymlinkTests(TempDirCase):
    """A symlink at the lock path, pointing at a separate regular file."""

    def setUp(self) -> None:
        super().setUp()
        self.target = self.root / "elsewhere" / "victim"
        self.target.parent.mkdir()
        self.lock_path.parent.mkdir()
        self.lock_path.symlink_to(self.target)

    def make_target(self) -> os.stat_result:
        """Create the target with content and a backdated mtime; return its stat."""
        self.target.write_bytes(b"not a lock file")
        os.utime(self.target, (_OLD_MTIME, _OLD_MTIME))
        return self.target.stat()

    def assert_refused(self, call: Callable[[], object]) -> None:
        """*call* raises ELOOP."""
        with self.assertRaises(OSError) as caught:
            call()
        self.assertEqual(caught.exception.errno, errno.ELOOP)

    def assert_untouched(self, before: os.stat_result) -> None:
        """The target's size and mtime are what they were."""
        after = self.target.stat()
        self.assertEqual(after.st_size, before.st_size)
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)

    def test_acquire_refuses_a_symlink_to_a_missing_file_and_creates_nothing(
        self,
    ) -> None:
        self.assert_refused(lambda: run_lock.acquire(self.lock_path, private=True))
        self.assertFalse(self.target.exists())

    def test_acquire_refuses_a_symlink_to_an_existing_file_and_leaves_it(
        self,
    ) -> None:
        before = self.make_target()
        self.assert_refused(lambda: run_lock.acquire(self.lock_path, private=True))
        self.assert_untouched(before)

    def test_is_held_refuses_a_symlink_to_a_missing_file_and_creates_nothing(
        self,
    ) -> None:
        self.assert_refused(lambda: run_lock.is_held(self.lock_path, private=True))
        self.assertFalse(self.target.exists())

    def test_is_held_refuses_a_symlink_to_an_existing_file_and_leaves_it(
        self,
    ) -> None:
        before = self.make_target()
        self.assert_refused(lambda: run_lock.is_held(self.lock_path, private=True))
        self.assert_untouched(before)

    def test_by_default_acquire_follows_the_symlink_and_creates_its_target(
        self,
    ) -> None:
        """Pinned: the default stays byte-for-byte what existing callers rely on."""
        lock = run_lock.acquire(self.lock_path)
        assert lock is not None
        self.addCleanup(lock.release)
        self.assertTrue(self.target.is_file())
        self.assertTrue(run_lock.is_held(self.target))

    def test_by_default_is_held_follows_the_symlink_to_the_target(self) -> None:
        """Pinned: a missing target reads free, and a held one reads held through the link."""
        self.assertFalse(run_lock.is_held(self.lock_path))
        self.make_target()
        lock = run_lock.acquire(self.target)
        assert lock is not None
        self.addCleanup(lock.release)
        self.assertTrue(run_lock.is_held(self.lock_path))


@unittest.skipIf(sys.platform == "win32", "POSIX mode bits are not meaningful on NTFS")
class ModeTests(TempDirCase):
    """What ``private=True`` creates, under the usual umask."""

    def setUp(self) -> None:
        super().setUp()
        old_umask = os.umask(USUAL_UMASK)
        self.addCleanup(os.umask, old_umask)

    def test_the_file_is_0600_and_each_missing_parent_0700(self) -> None:
        root_mode = stat.S_IMODE(self.root.stat().st_mode)
        lock_path = self.root / "outer" / "inner" / "run.lock"
        lock = run_lock.acquire(lock_path, private=True)
        assert lock is not None
        lock.release()
        self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(lock_path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(lock_path.parent.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), root_mode)

    def test_an_existing_parent_keeps_its_mode(self) -> None:
        existing = self.root / "shared"
        existing.mkdir(mode=0o755)
        lock_path = existing / "new" / "run.lock"
        lock = run_lock.acquire(lock_path, private=True)
        assert lock is not None
        lock.release()
        self.assertEqual(stat.S_IMODE(existing.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(lock_path.parent.stat().st_mode), 0o700)

    def test_a_private_lock_is_taken_released_and_probed_like_any_other(self) -> None:
        lock_path = self.root / "run.lock"
        lock = run_lock.acquire(lock_path, private=True)
        assert lock is not None
        self.assertTrue(run_lock.is_held(lock_path, private=True))
        self.assertIsNone(run_lock.acquire(lock_path, private=True))
        lock.release()
        self.assertFalse(run_lock.is_held(lock_path, private=True))

    def test_an_absent_file_probes_free_privately_too(self) -> None:
        self.assertFalse(run_lock.is_held(self.root / "absent.lock", private=True))


class WindowsPrivateTests(TempDirCase):
    """On win32, ``private=True`` is refused with ENOSYS before anything is opened."""

    def setUp(self) -> None:
        super().setUp()
        # The win32 arm must not reference O_NOFOLLOW, which Windows lacks: hide it from os.
        hidden = mock.patch.dict(os.__dict__)
        hidden.start()
        self.addCleanup(hidden.stop)
        os.__dict__.pop("O_NOFOLLOW", None)
        self.windows = load_as_windows(mock.Mock())
        self.opened = mock.Mock(side_effect=AssertionError("os.open was called"))
        patched = mock.patch.object(self.windows.os, "open", self.opened)
        patched.start()
        self.addCleanup(patched.stop)

    def assert_enosys(self, call: Callable[[], object]) -> None:
        """*call* raises ENOSYS, having opened and created nothing."""
        with self.assertRaises(OSError) as caught:
            call()
        self.assertEqual(caught.exception.errno, errno.ENOSYS)
        self.opened.assert_not_called()
        self.assertFalse(self.lock_path.parent.exists())

    def test_acquire_refuses(self) -> None:
        self.assert_enosys(lambda: self.windows.acquire(self.lock_path, private=True))

    def test_is_held_refuses(self) -> None:
        self.assert_enosys(lambda: self.windows.is_held(self.lock_path, private=True))


if __name__ == "__main__":  # pragma: no cover - direct execution entry point
    unittest.main()
