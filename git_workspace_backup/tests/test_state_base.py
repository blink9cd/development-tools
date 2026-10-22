#!/usr/bin/env python3
"""The tool's own directory under the home: owner-only, refused when unsafe, never followed.

Staging holds a build in progress, so a state base another account can read exposes every
backed-up file, and one it can write lets it plant what a run reads or writes. So a run and
install create the base and every directory beneath it owner-only, and make an existing base
so; run, install and status refuse one that is not a directory, not this user's, or writable
by anyone else. The three files a run opens there by name -- the lock, the identity and the
ledger -- are never opened through a symlink, and the ledger is read within a bound.
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import _cli_case as cc

import backup_launchd
import backup_preflight
import backup_ledger
import schedule_artifacts
import git_workspace_backup as wb
from backup_checkout import Refused
from backup_machine import IDENTITY_NAME
from backup_run import LOCK_NAME

#: Owner-only, for a directory.
_PRIVATE_DIR = 0o700
#: Group and other bits, none of which a state file may carry.
_FOREIGN = 0o077


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


class StateBaseTests(cc.CliCase):
    def setUp(self) -> None:
        super().setUp()
        self.base = self.tree.home / wb.STATE_BASE_NAME

    def install(self, *extra: str, host: "wb.Host | None" = None) -> int:
        self.launchctl.prints = [backup_launchd.PRINT_ABSENT_RC, 0]
        return self.main("--mode", "install", *extra, host=host)

    def test_a_run_makes_the_base_and_everything_beneath_it_owner_only(self) -> None:
        self.assertEqual(self.main("--mode", "run"), 0, self.err.getvalue())
        directories = [self.base, *(p for p in self.base.rglob("*") if p.is_dir())]
        self.assertGreater(len(directories), 3)
        for directory in directories:
            with self.subTest(directory=directory.name):
                self.assertEqual(_mode(directory), _PRIVATE_DIR)
        state = self.state()
        for path in (
            self.base / LOCK_NAME,
            self.base / IDENTITY_NAME,
            state / backup_ledger.LEDGER_NAME,
            state / backup_ledger.LAST_RUN_NAME,
        ):
            with self.subTest(file=path.name):
                self.assertEqual(_mode(path) & _FOREIGN, 0)

    def test_install_makes_the_base_and_its_logs_owner_only(self) -> None:
        self.assertEqual(self.install(), 0, self.err.getvalue())
        self.assertEqual(_mode(self.base), _PRIVATE_DIR)
        self.assertEqual(_mode(schedule_artifacts.logs_dir(self.base)), _PRIVATE_DIR)

    def test_an_existing_base_others_can_read_is_made_owner_only(self) -> None:
        for argv in (("--mode", "run"), ("--mode", "install")):
            with self.subTest(argv=argv):
                self.base.mkdir(exist_ok=True)
                self.base.chmod(0o755)
                self.launchctl.prints = [backup_launchd.PRINT_ABSENT_RC, 0]
                self.assertEqual(self.main(*argv), 0, self.err.getvalue())
                self.assertEqual(_mode(self.base), _PRIVATE_DIR)

    def test_dry_runs_and_status_create_and_change_nothing(self) -> None:
        for argv in (
            ("--mode", "run", "--dry-run"),
            ("--mode", "install", "--dry-run"),
            ("--mode", "status"),
        ):
            with self.subTest(argv=argv):
                self.main(*argv)
                self.assertFalse(self.base.exists())
        self.base.mkdir()
        self.base.chmod(0o755)
        self.main("--mode", "status")
        self.assertEqual(_mode(self.base), 0o755)

    def _unsafe_bases(self) -> "dict[str, tuple[wb.Host, str]]":
        """Each unsafe shape, made in turn by the caller, with the host and refusal it gets."""
        return {
            "group-writable": (self.host(), "writable by other users"),
            "world-writable": (self.host(), "writable by other users"),
            "a symlink to a directory": (self.host(), "not a directory"),
            "a file": (self.host(), "not a directory"),
            "another user's": (
                replace(self.host(), euid=backup_preflight.effective_uid() + 1),
                "not owned by this user",
            ),
        }

    def _make(self, shape: str) -> None:
        """Put *shape* where the base belongs."""
        if shape == "a symlink to a directory":
            real = self.tmp / "fixture-real-base"
            real.mkdir(mode=_PRIVATE_DIR, exist_ok=True)
            self.base.symlink_to(real)
        elif shape == "a file":
            self.base.write_text("fixture", encoding="utf-8", newline="\n")
        else:
            self.base.mkdir()
            mode = {"group-writable": 0o770, "world-writable": 0o703}
            self.base.chmod(mode.get(shape, _PRIVATE_DIR))

    def _clear(self) -> None:
        if self.base.is_dir() and not self.base.is_symlink():
            for path in sorted(self.base.rglob("*"), reverse=True):
                path.rmdir() if path.is_dir() else path.unlink()
            self.base.rmdir()
        else:
            self.base.unlink(missing_ok=True)

    def test_run_install_and_status_refuse_an_unsafe_base(self) -> None:
        modes = (
            ("--mode", "run"),
            ("--mode", "run", "--dry-run"),
            ("--mode", "install"),
            ("--mode", "install", "--dry-run"),
            ("--mode", "status"),
        )
        for shape, (host, refusal) in self._unsafe_bases().items():
            for argv in modes:
                with self.subTest(shape=shape, argv=argv):
                    self._make(shape)
                    self.addCleanup(self._clear)
                    self.launchctl = cc.FakeLaunchctl()
                    self.assertEqual(self.main(*argv, host=host), 2)
                    self.assert_one_error_line(refusal)
                    self.assertIn(str(self.base), self.err.getvalue())
                    self.assertEqual(self.launchctl.argvs, [])
                    self.assertFalse(self.destination.exists())
                    self._clear()


class GuardEdgeTests(unittest.TestCase):
    """The guard's own failures: each is a refusal line, never a traceback."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()

    def test_the_effective_uid_owns_what_this_process_makes(self) -> None:
        made = self.tmp / "fixture-made"
        made.mkdir()
        self.assertEqual(backup_preflight.effective_uid(), made.stat().st_uid)

    def test_a_base_that_cannot_be_examined_is_refused(self) -> None:
        blocker = self.tmp / "fixture-file"
        blocker.write_text("fixture", encoding="utf-8", newline="\n")
        with self.assertRaisesRegex(Refused, "cannot be examined"):
            backup_preflight.refuse_unsafe_state_base(
                blocker / wb.STATE_BASE_NAME, backup_preflight.effective_uid()
            )

    def test_a_base_that_cannot_be_made_is_refused(self) -> None:
        if backup_preflight.effective_uid() == 0:  # pragma: no cover - root only
            self.skipTest("root makes a directory whatever its parent's mode")
        parent = self.tmp / "fixture-read-only"
        parent.mkdir(mode=0o500)
        self.addCleanup(parent.chmod, _PRIVATE_DIR)
        with self.assertRaisesRegex(Refused, "cannot be made private"):
            backup_preflight.secure_state_base(
                parent / wb.STATE_BASE_NAME, backup_preflight.effective_uid()
            )

    def test_securing_changes_the_mode_through_the_opened_directory(self) -> None:
        base = self.tmp / wb.STATE_BASE_NAME
        base.mkdir(mode=0o755)
        base.chmod(0o755)
        # A path-based chmod is a second lookup a swap can redirect; the one open is not.
        with mock.patch.object(os, "chmod", side_effect=AssertionError("by path")):
            backup_preflight.secure_state_base(base, backup_preflight.effective_uid())
        self.assertEqual(_mode(base), _PRIVATE_DIR)

    def test_a_base_swapped_for_a_symlink_after_its_check_is_refused(self) -> None:
        base = self.tmp / wb.STATE_BASE_NAME
        base.mkdir(mode=_PRIVATE_DIR)
        victim = self.tmp / "fixture-victim"
        victim.mkdir()
        victim.chmod(0o755)
        real_mkdir = os.mkdir

        def mkdir_then_swap(path: object, *args: object, **kwargs: object) -> None:
            # The swap lands between whatever check ran first and the mode change.
            try:
                real_mkdir(path, *args, **kwargs)  # type: ignore[arg-type]
            finally:
                if Path(str(path)) == base and not base.is_symlink():
                    base.rmdir()
                    base.symlink_to(victim)

        with mock.patch.object(os, "mkdir", mkdir_then_swap):
            with self.assertRaisesRegex(Refused, "not a directory"):
                backup_preflight.secure_state_base(
                    base, backup_preflight.effective_uid()
                )
        self.assertEqual(_mode(victim), 0o755)

    def test_a_base_that_cannot_be_opened_or_changed_is_refused(self) -> None:
        base = self.tmp / wb.STATE_BASE_NAME
        base.mkdir(mode=_PRIVATE_DIR)
        euid = backup_preflight.effective_uid()
        failing = mock.patch.object(
            os, "fchmod", side_effect=PermissionError("fixture")
        )
        with failing, self.assertRaisesRegex(Refused, "cannot be made private"):
            backup_preflight.secure_state_base(base, euid)
        if euid == 0:  # pragma: no cover - root only
            self.skipTest("root opens a directory whatever its mode")
        base.chmod(0)
        self.addCleanup(base.chmod, _PRIVATE_DIR)
        with self.assertRaisesRegex(Refused, "cannot be examined"):
            backup_preflight.secure_state_base(base, euid)


class NoFollowTests(cc.CliCase):
    def setUp(self) -> None:
        super().setUp()
        self.base = self.tree.home / wb.STATE_BASE_NAME
        self.elsewhere = self.tmp / "fixture-elsewhere"
        self.elsewhere.mkdir()

    def test_a_run_lock_that_is_a_symlink_is_never_opened_through(self) -> None:
        self.base.mkdir(mode=_PRIVATE_DIR)
        target = self.elsewhere / "planted.lock"
        (self.base / LOCK_NAME).symlink_to(target)
        self.assertEqual(self.main("--mode", "run"), 2)
        self.assertIn("run lock", self.err.getvalue())
        self.assertFalse(target.exists())

    def test_an_identity_file_that_is_a_symlink_is_refused(self) -> None:
        self.base.mkdir(mode=_PRIVATE_DIR)
        target = self.elsewhere / "machine"
        target.write_text(f"{cc.MACHINE}\n", encoding="utf-8", newline="\n")
        (self.base / IDENTITY_NAME).symlink_to(target)
        self.assertEqual(self.main("--mode", "run"), 2)
        self.assertIn("identity", self.err.getvalue())

    def test_a_ledger_that_is_a_symlink_is_refused_by_run_and_status(self) -> None:
        self.assertEqual(self.main("--mode", "run"), 0, self.err.getvalue())
        ledger = self.state() / backup_ledger.LEDGER_NAME
        moved = self.elsewhere / backup_ledger.LEDGER_NAME
        ledger.rename(moved)
        ledger.symlink_to(moved)
        self.write_plist()
        for mode in ("run", "status"):
            with self.subTest(mode=mode):
                self.assertEqual(self.main("--mode", mode), 2)
                self.assertIn(backup_ledger.LEDGER_NAME, self.err.getvalue())

    def test_a_ledger_past_its_bound_is_refused_unread(self) -> None:
        self.assertEqual(self.main("--mode", "run"), 0, self.err.getvalue())
        ledger = self.state() / backup_ledger.LEDGER_NAME
        ledger.write_bytes(b" " * (backup_ledger.STATE_READ_LIMIT + 1))
        self.write_plist()
        for mode in ("run", "status"):
            with self.subTest(mode=mode):
                self.assertEqual(self.main("--mode", mode), 2)
                self.assertIn("more than", self.err.getvalue())


if __name__ == "__main__":
    unittest.main()
