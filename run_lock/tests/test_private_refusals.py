#!/usr/bin/env python3
"""Grouped suite: ``private=True`` refuses a planted parent, a planted lock file, and a mode.

Another user cannot be conjured in a test, so another user's ownership is simulated by making
``_euid`` report some other user; a hard link and a FIFO are real.

Entry point assumptions: loaded by the folder's aggregator, or run alone. The insert below
reaches the subject one folder up, never the working directory.
"""

from __future__ import annotations

import errno
import os
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run_lock  # noqa: E402 - import follows the sys.path bootstrap above
from _run_lock_cases import USUAL_UMASK, load_as_windows  # noqa: E402 - beside this module

#: ``os.mkdir`` itself, captured before any test patches it: a plant must not recurse.
_REAL_MKDIR = os.mkdir


class _PrivateCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.lock_path = self.root / "state" / "run.lock"
        old_umask = os.umask(USUAL_UMASK)
        self.addCleanup(os.umask, old_umask)

    def as_another_user(self) -> None:
        """From here on, every file this process made reads as someone else's."""
        other = mock.patch.object(run_lock, "_euid", return_value=run_lock._euid() + 1)
        other.start()
        self.addCleanup(other.stop)

    def assert_refused(self, call: Callable[[], object], reason: str) -> None:
        """*call* raises an OSError (not a subclass for a missing file) naming *reason*."""
        with self.assertRaises(OSError) as caught:
            call()
        self.assertNotIsInstance(caught.exception, FileNotFoundError)
        self.assertIn(reason, str(caught.exception))


@unittest.skipIf(sys.platform == "win32", "private=True is POSIX-only")
class PlantedParentTests(_PrivateCase):
    """A parent that appears between the walk's existence check and its own mkdir."""

    def plant_then_mkdir(self, plant: Callable[[Path], None]) -> None:
        """Make ``os.mkdir`` of the lock's parent find *plant* already done there."""
        target = os.fspath(self.lock_path.parent)

        def racing_mkdir(path: "str | os.PathLike[str]", mode: int = 0o777) -> None:
            if os.fspath(path) == target and not os.path.lexists(target):
                plant(Path(target))
            _REAL_MKDIR(path, mode)

        patched = mock.patch.object(run_lock.os, "mkdir", side_effect=racing_mkdir)
        patched.start()
        self.addCleanup(patched.stop)

    def acquire(self) -> object:
        return run_lock.acquire(self.lock_path, private=True)

    def test_a_planted_symlink_to_a_directory_is_refused(self) -> None:
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        self.plant_then_mkdir(lambda target: target.symlink_to(elsewhere))
        self.assert_refused(self.acquire, "not a real directory")
        self.assertFalse((elsewhere / "run.lock").exists())

    def test_a_planted_directory_open_to_others_is_refused(self) -> None:
        self.plant_then_mkdir(lambda target: _REAL_MKDIR(target, 0o755))
        self.assert_refused(self.acquire, "open to other users")
        self.assertFalse(self.lock_path.exists())

    def test_a_planted_directory_owned_by_another_user_is_refused(self) -> None:
        self.plant_then_mkdir(lambda target: _REAL_MKDIR(target, 0o700))
        self.as_another_user()
        self.assert_refused(self.acquire, "owned by another user")
        self.assertFalse(self.lock_path.exists())

    def test_our_own_private_directory_made_concurrently_is_accepted(self) -> None:
        """A second run of the same user creating the same parent is no attack."""
        self.plant_then_mkdir(lambda target: _REAL_MKDIR(target, 0o700))
        lock = run_lock.acquire(self.lock_path, private=True)
        assert lock is not None
        lock.release()


@unittest.skipIf(sys.platform == "win32", "private=True is POSIX-only")
class PlantedLockFileTests(_PrivateCase):
    """A lock file already at the path that is not this user's own, single-named file."""

    def setUp(self) -> None:
        super().setUp()
        self.lock_path.parent.mkdir(mode=0o700)

    def calls(self) -> "list[Callable[[], object]]":
        return [
            lambda: run_lock.acquire(self.lock_path, private=True),
            lambda: run_lock.is_held(self.lock_path, private=True),
        ]

    def test_a_hard_link_is_refused_by_both_calls(self) -> None:
        """Another user's file, hard-linked here, is one they can open and hold forever."""
        theirs = self.root / "theirs"
        theirs.write_bytes(b"")
        os.link(theirs, self.lock_path)
        for call in self.calls():
            self.assert_refused(call, "more than one link")

    def test_a_file_owned_by_another_user_is_refused_by_both_calls(self) -> None:
        self.lock_path.write_bytes(b"")
        self.as_another_user()
        for call in self.calls():
            self.assert_refused(call, "owned by another user")

    def test_a_fifo_is_refused_by_both_calls(self) -> None:
        # The check is ``sys.platform`` itself, so the type check's win32 pass reads the
        # ``mkfifo`` below as unreachable; the class skip already keeps it off Windows.
        if sys.platform == "win32":
            self.skipTest("Windows has no named pipes")  # pragma: only-on win32
        os.mkfifo(self.lock_path, 0o600)  # pragma: only-on posix
        for call in self.calls():
            self.assert_refused(call, "not a regular file")

    def test_a_directory_is_refused_by_the_probe(self) -> None:
        """The probe opens read-only, which a directory allows; acquire's open refuses it."""
        self.lock_path.mkdir()
        self.assert_refused(
            lambda: run_lock.is_held(self.lock_path, private=True),
            "not a regular file",
        )

    def test_a_refused_file_leaves_no_descriptor_open(self) -> None:
        theirs = self.root / "theirs"
        theirs.write_bytes(b"")
        os.link(theirs, self.lock_path)
        closed: list[int] = []
        real_close = os.close

        def record_close(fd: int) -> None:
            closed.append(fd)
            real_close(fd)

        with mock.patch.object(run_lock.os, "close", side_effect=record_close):
            for call in self.calls():
                with self.assertRaises(OSError):
                    call()
        self.assertEqual(len(closed), 2, "each refusal must close what it opened")

    def test_our_own_existing_lock_file_is_accepted(self) -> None:
        self.lock_path.write_bytes(b"")
        lock = run_lock.acquire(self.lock_path, private=True)
        assert lock is not None
        self.assertTrue(run_lock.is_held(self.lock_path, private=True))
        lock.release()


class ModeAndPrivateTests(unittest.TestCase):
    """``private=True`` sets the mode itself, so ANY mode passed beside it is refused."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.lock_path = Path(tmp.name) / "state" / "run.lock"

    def assert_value_error(self, mode: int) -> None:
        with self.assertRaises(ValueError):
            run_lock.acquire(self.lock_path, mode=mode, private=True)
        self.assertFalse(
            self.lock_path.parent.exists(), "refused before creating anything"
        )

    def test_a_non_default_mode_with_private_is_a_value_error(self) -> None:
        self.assert_value_error(0o640)

    def test_the_default_mode_passed_explicitly_is_a_value_error_too(self) -> None:
        """0o644 asked for beside private=True must not be silently narrowed to 0o600."""
        self.assert_value_error(0o644)


class WindowsEuidTests(unittest.TestCase):
    """The win32 ``_euid`` is never reached (private=True is refused first) and says so."""

    def test_it_raises_enosys(self) -> None:
        hidden = mock.patch.dict(os.__dict__)
        hidden.start()
        self.addCleanup(hidden.stop)
        os.__dict__.pop("O_NOFOLLOW", None)
        windows = load_as_windows(mock.Mock())
        with self.assertRaises(OSError) as caught:
            windows._euid()
        self.assertEqual(caught.exception.errno, errno.ENOSYS)


@unittest.skipIf(sys.platform == "win32", "POSIX ownership is not meaningful on NTFS")
class EuidTests(unittest.TestCase):
    def test_it_owns_what_this_process_creates(self) -> None:
        made = tempfile.NamedTemporaryFile()
        self.addCleanup(made.close)
        self.assertEqual(run_lock._euid(), os.stat(made.name).st_uid)


if __name__ == "__main__":  # pragma: no cover - direct execution entry point
    unittest.main()
