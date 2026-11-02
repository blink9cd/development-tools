#!/usr/bin/env python3
"""``--mode install`` and ``--mode uninstall`` through the CLI: what the agent is built from.

launchctl never runs for real: FakeLaunchctl answers every verb, and the waiter's clock is a
counter, so a wait that never settles ends in a few reads rather than sleeping.
"""

from __future__ import annotations

import errno
import os
import plistlib
import shutil
import sys
import unittest
from dataclasses import replace
from pathlib import Path

import _cli_case as cc
import _fixtures as fx

import backup_launchd
import backup_run
import os_schedule_gen
import git_workspace_backup as wb
from backup_fs import RunLock

#: The one line install and uninstall print while a backup run holds the lock, restated.
IN_PROGRESS = "a backup run is in progress; retry when it finishes"
#: Lock shapes no run can open, each measured to make the probe raise rather than answer.
_FILE_BASE = "the tool's directory is a file"
_MODE_000 = "the lock has mode 000"
_FIFO = "the lock is a FIFO"
_LOOP = "the lock is a symlink loop"
_UNOPENABLE_LOCKS = (_FILE_BASE, _MODE_000, _FIFO, _LOOP)

# mkfifo(3) and geteuid(2) are POSIX-only. The split is an ``if`` so a win32
# type-check pass still sees both names; the win32 arm exists only for that pass.
if sys.platform == "win32":

    def _make_fifo(path: Path) -> None:  # pragma: only-on win32
        """Refuse: Windows has no FIFOs, and this tool never runs there."""
        raise OSError(errno.ENOSYS, "mkfifo needs POSIX, which Windows lacks")

    def _running_as_root() -> bool:  # pragma: only-on win32
        """No uid 0 on Windows."""
        return False

else:  # pragma: only-on posix
    _make_fifo = os.mkfifo

    def _running_as_root() -> bool:
        """Whether this process is root, whom no file mode refuses."""
        return os.geteuid() == 0


def _clear_state_base(base: Path) -> None:
    """Remove whatever stands where the tool's directory belongs."""
    if base.is_dir():
        shutil.rmtree(base)
    elif base.exists():
        base.unlink()


def _make_unopenable_lock(base: Path, lock: Path, shape: str) -> None:
    """Replace the tool's directory with one whose run lock has *shape*."""
    _clear_state_base(base)
    if shape == _FILE_BASE:
        base.write_text("not a directory", encoding="utf-8", newline="\n")
        return
    base.mkdir()
    if shape == _MODE_000:
        lock.write_text("", encoding="utf-8", newline="\n")
        lock.chmod(0o000)
    elif shape == _FIFO:
        _make_fifo(lock)
    else:
        lock.symlink_to(lock.name)


def _entries(base: Path) -> "list[str] | None":
    """What the tool's directory holds, or None where it is not a directory."""
    return sorted(os.listdir(base)) if base.is_dir() else None


class AgentCliTests(cc.CliCase):
    def test_install_bakes_this_hosts_facts_and_uninstall_removes_the_agent(
        self,
    ) -> None:
        self.launchctl.prints = [backup_launchd.PRINT_ABSENT_RC, 0]
        self.assertEqual(self.main("--mode", "install", "--time", "07:30"), 0)
        plist_file = backup_launchd.plist_path(self.tree.home)
        plist = plistlib.loads(plist_file.read_bytes())
        self.assertEqual(
            plist["ProgramArguments"],
            [
                cc.FIXTURE_PYTHON,
                str(self.script),
                *backup_launchd.AGENT_ARGS,
                "--destination",
                str(self.destination),
            ],
        )
        self.assertEqual(plist["StartCalendarInterval"], {"Hour": 7, "Minute": 30})
        self.assertEqual(
            plist["EnvironmentVariables"]["PATH"],
            backup_launchd.baked_path(cc.FIXTURE_GIT),
        )
        logs = os_schedule_gen.logs_dir(self.tree.home / wb.STATE_BASE_NAME)
        self.assertTrue(logs.is_dir())
        domain = f"gui/{cc.FIXTURE_UID}"
        self.assertIn(
            [backup_launchd.LAUNCHCTL, "bootstrap", domain, str(plist_file)],
            self.launchctl.argvs,
        )
        self.launchctl.settled = backup_launchd.PRINT_ABSENT_RC
        self.assertEqual(self.main("--mode", "uninstall"), 0)
        self.assertFalse(plist_file.exists())

    def test_install_default_time_is_the_documented_default(self) -> None:
        self.launchctl.prints = [backup_launchd.PRINT_ABSENT_RC, 0]
        self.assertEqual(self.main("--mode", "install"), 0)
        plist = plistlib.loads(backup_launchd.plist_path(self.tree.home).read_bytes())
        default = os_schedule_gen.parse_daily_time(backup_launchd.DEFAULT_TIME)
        assert default is not None
        interval = {"Hour": default.hour, "Minute": default.minute}
        self.assertEqual(plist["StartCalendarInterval"], interval)

    def test_install_dry_run_writes_nothing_and_asks_launchd_nothing(self) -> None:
        self.assertEqual(self.main("--mode", "install", "--dry-run"), 0)
        self.assertIn("would run", self.out.getvalue())
        self.assertFalse(backup_launchd.plist_path(self.tree.home).exists())
        self.assertEqual(self.launchctl.argvs, [])

    def test_install_refusals_exit_2_before_anything_is_written(self) -> None:
        cases = {
            "bad time": (["--time", "25:00"], self.host(), "--time"),
            "no git": (
                [],
                replace(self.host(), which=lambda name: None),
                "git is not on PATH",
            ),
            "linked worktree": (
                [],
                replace(self.host(), start=self.tmp / "wt"),
                "outside a linked worktree",
            ),
            # The worktree check's own query, failing after the layout check passed.
            "toplevel failure": (
                [],
                self.host(
                    self.fake_runner(
                        real=fx.failing_on_argv(("rev-parse", "--show-toplevel"))
                    )
                ),
                "git rev-parse --show-toplevel failed",
            ),
            "layout check failure": (
                [],
                self.host(self.fake_runner(real=fx.failing_when("--show-toplevel"))),
                "--show-toplevel",
            ),
        }
        for label, (extra, host, fragment) in cases.items():
            with self.subTest(label):
                self.assertEqual(self.main("--mode", "install", *extra, host=host), 2)
                self.assert_one_error_line(fragment)
                self.assertFalse(backup_launchd.plist_path(self.tree.home).exists())
                self.assertEqual(self.launchctl.argvs, [])

    def test_install_and_uninstall_refuse_while_a_backup_run_holds_the_lock(
        self,
    ) -> None:
        # The lock the run itself takes, spelled from the same constants: bootout would kill
        # that run, and its git children, in sessions of their own, would write on.
        base = self.tree.home / wb.STATE_BASE_NAME
        holder = RunLock(base / backup_run.LOCK_NAME)
        self.assertTrue(holder.acquire())
        self.addCleanup(holder.release)
        plist = backup_launchd.plist_path(self.tree.home)
        for argv in (("install",), ("install", "--dry-run")):
            with self.subTest(argv=argv):
                self.assertEqual(self.main("--mode", *argv), 2)
                self.assert_one_error_line(IN_PROGRESS)
                self.assertEqual(self.launchctl.argvs, [])
                self.assertFalse(plist.exists())
        with self.subTest("uninstall"):
            self.write_plist()
            self.launchctl.settled = backup_launchd.PRINT_ABSENT_RC
            self.assertEqual(self.main("--mode", "uninstall"), 2)
            self.assert_one_error_line(IN_PROGRESS)
            self.assertEqual(self.launchctl.argvs, [])
            self.assertTrue(plist.exists())
        holder.release()
        # Released, the same install goes ahead, and the probe has left the lock free.
        self.launchctl.prints = [backup_launchd.PRINT_ABSENT_RC, 0]
        self.assertEqual(self.main("--mode", "install"), 0)
        self.assertTrue(holder.acquire())

    def test_a_lock_no_run_could_open_refuses_install_but_never_uninstall(
        self,
    ) -> None:
        # A run opens the lock read-write, so one that cannot be opened is one no run holds.
        # Uninstall warns and goes ahead, or a broken state base would make the agent
        # impossible to remove; install still refuses, since every run it set up would fail.
        base = self.tree.home / wb.STATE_BASE_NAME
        lock = base / backup_run.LOCK_NAME
        plist = backup_launchd.plist_path(self.tree.home)
        self.addCleanup(_clear_state_base, base)
        for shape in _UNOPENABLE_LOCKS:
            with self.subTest(shape):
                if (
                    shape == _MODE_000 and _running_as_root()
                ):  # pragma: no cover - root only
                    self.skipTest("root opens a lock whatever its mode")
                _make_unopenable_lock(base, lock, shape)
                # Each shape starts with no plist, whatever an earlier shape's uninstall did.
                plist.unlink(missing_ok=True)
                before = _entries(base)
                self.launchctl = cc.FakeLaunchctl()
                self.assertEqual(self.main("--mode", "install"), 2)
                # A file where the directory belongs fails the state-base check first.
                refusal = (
                    "is not a directory"
                    if shape == _FILE_BASE
                    else "could not check whether a backup run is in progress"
                )
                self.assert_one_error_line(refusal)
                self.assertEqual(self.launchctl.argvs, [])
                self.assertFalse(plist.exists())
                self.write_plist()
                self.launchctl.settled = backup_launchd.PRINT_ABSENT_RC
                self.assertEqual(self.main("--mode", "uninstall"), 0)
                self.assertFalse(plist.exists())
                self.assertIn("bootout", [argv[1] for argv in self.launchctl.argvs])
                [warning] = self.err.getvalue().splitlines()
                self.assertTrue(warning.startswith("WARNING: "), warning)
                self.assertIn(str(lock), warning)
                # The probe created nothing: no directory, no lock file.
                self.assertEqual(_entries(base), before)

    def test_a_lock_symlinked_to_another_file_is_never_probed_through(self) -> None:
        # A self-loop fails with ELOOP whether or not the probe follows links, so it cannot
        # show O_NOFOLLOW at work. A link to a separate file can: followed, a missing target
        # reads as a free lock and an existing one is opened and flocked, and install goes on.
        base = self.tree.home / wb.STATE_BASE_NAME
        lock = base / backup_run.LOCK_NAME
        elsewhere = self.tmp / "fixture-elsewhere"
        elsewhere.mkdir()
        self.addCleanup(_clear_state_base, base)
        for present in (False, True):
            with self.subTest(target_present=present):
                target = elsewhere / f"planted-{present}.lock"
                if present:
                    fx.plant_untouched_file(target)
                _clear_state_base(base)
                base.mkdir()
                lock.symlink_to(target)
                before = fx.file_fingerprint(target)
                self.launchctl = cc.FakeLaunchctl()
                self.assertEqual(self.main("--mode", "install"), 2)
                self.assert_one_error_line(
                    "could not check whether a backup run is in progress"
                )
                self.assertEqual(self.launchctl.argvs, [])
                self.assertEqual(fx.file_fingerprint(target), before)

    def test_uninstall_needs_neither_a_checkout_nor_a_configuration(self) -> None:
        plist = self.write_plist()
        (self.tree.workspace / "pyproject.toml").unlink()
        self.launchctl.settled = backup_launchd.PRINT_ABSENT_RC
        outside = replace(self.host(), start=self.tmp)
        self.assertEqual(self.main("--mode", "uninstall", host=outside), 0)
        self.assertFalse(plist.exists())

    def test_uninstall_refuses_dry_run_rather_than_uninstalling(self) -> None:
        plist = self.write_plist()
        # launchd would confirm the agent gone, so an uninstall that ran would succeed.
        self.launchctl.settled = backup_launchd.PRINT_ABSENT_RC
        self.assertEqual(self.main("--mode", "uninstall", "--dry-run"), 2)
        self.assert_one_error_line("--dry-run")
        self.assertIn("--mode uninstall", self.err.getvalue())
        self.assertEqual(self.runner.calls, [])
        self.assertTrue(plist.exists())

    def test_uninstall_keeps_the_plist_and_exits_2_while_launchd_holds_the_agent(
        self,
    ) -> None:
        plist = self.write_plist()
        self.launchctl.settled = 0
        self.assertEqual(self.main("--mode", "uninstall"), 2)
        self.assertIn("did not unload", self.err.getvalue())
        self.assertTrue(plist.exists())


if __name__ == "__main__":
    unittest.main()
