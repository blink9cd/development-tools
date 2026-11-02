#!/usr/bin/env python3
"""The subprocess seam and the filesystem primitives every other module builds on."""

from __future__ import annotations

import errno
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable, Sequence
from pathlib import Path
from unittest import mock

import _fixtures as fx

import backup_fs
import atomic_write  # after backup_fs, whose bootstrap puts it on sys.path
import backup_proc


class RunCmdTests(unittest.TestCase):
    """The real runner, exercised against real processes (no host state is touched)."""

    def test_a_finished_command_reports_code_and_both_streams(self) -> None:
        result = backup_proc.run_cmd(
            ["sh", "-c", "printf out; printf err >&2; exit 3"], None, 30.0
        )
        self.assertEqual(result, (3, "out", "err"))

    def test_a_command_past_its_bound_reports_the_timeout_code(self) -> None:
        code, _, err = backup_proc.run_cmd(["sleep", "5"], None, 0.2)
        self.assertEqual(code, backup_proc.TIMEOUT_RC)
        self.assertIn("timed out", err)

    def test_a_missing_executable_reports_not_started(self) -> None:
        code, _, err = backup_proc.run_cmd(["no-such-binary-fixture"], None, 5.0)
        self.assertEqual(code, backup_proc.NOT_STARTED_RC)
        self.assertIn("could not start", err)

    def test_cwd_is_honored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, out, _ = backup_proc.run_cmd(["pwd", "-P"], Path(tmp), 5.0)
        self.assertEqual(Path(out.strip()), Path(tmp).resolve())


class GitRunnerTests(unittest.TestCase):
    def test_prefixes_git_and_forwards_cwd_and_timeout(self) -> None:
        calls: list[tuple[list[str], "Path | None", float]] = []

        def fake(
            argv: Sequence[str], cwd: "Path | None", timeout: float
        ) -> "tuple[int, str, str]":
            calls.append((list(argv), cwd, timeout))
            return 0, "ok", ""

        run_git = backup_proc.git_runner(fake, timeout=7.0)
        self.assertEqual(run_git(["status"], Path("/opt/fixture-repo")), (0, "ok", ""))
        self.assertEqual(calls, [(["git", "status"], Path("/opt/fixture-repo"), 7.0)])


class AtomicWriteTests(unittest.TestCase):
    def test_text_creates_parents_leaves_no_temp_and_writes_lf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "a" / "b.json"
            backup_fs.atomic_write_text(target, "x\ny\n")
            self.assertEqual(target.read_bytes(), b"x\ny\n")
            self.assertEqual(
                sorted(p.name for p in target.parent.iterdir()), ["b.json"]
            )

    def test_replacing_an_existing_file_swaps_in_a_new_inode(self) -> None:
        """An in-place rewrite keeps the inode a concurrent reader holds; a replace cannot.

        The two tests beside this one pass for a plain truncate-and-refill write too, so
        this is the one that fails if either writer stops being temp-then-replace.
        """
        writers = {
            "text": lambda p: backup_fs.atomic_write_text(p, "new\n"),
            "bytes": lambda p: backup_fs.atomic_write_bytes(p, b"new\n"),
        }
        for kind, write in writers.items():
            with self.subTest(kind), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "state.json"
                target.write_bytes(b"old\n")
                before = target.stat().st_ino
                write(target)
                self.assertNotEqual(target.stat().st_ino, before)
                self.assertEqual(target.read_bytes(), b"new\n")

    def test_bytes_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "c" / "d.plist"
            backup_fs.atomic_write_bytes(target, b"\x00\x01")
            self.assertEqual(target.read_bytes(), b"\x00\x01")


#: Both writers, each writing the same four bytes.
_WRITERS: "dict[str, Callable[[Path], None]]" = {
    "text": lambda path: backup_fs.atomic_write_text(path, "new\n"),
    "bytes": lambda path: backup_fs.atomic_write_bytes(path, b"new\n"),
}


class AtomicWriteSafetyTests(unittest.TestCase):
    """Both writers stage through a temp file nothing can pre-plant, then rename it."""

    def scratch(self) -> Path:
        """A fresh directory for one writer's case, removed after the test."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        return Path(holder.name)

    def test_a_symlink_at_the_old_fixed_temp_name_is_never_touched(self) -> None:
        # The temp file used to be the fixed <name>.tmp: a symlink planted there was
        # written through into its target, and the rename then put the link itself in
        # the state file's place, or the snapshot manifest's on its way to iCloud.
        for kind, write in _WRITERS.items():
            with self.subTest(kind):
                root = self.scratch()
                victim = root / "victim.txt"
                victim.write_bytes(b"untouched\n")
                target = root / "state" / "last.json"
                target.parent.mkdir()
                planted = target.with_name("last.json.tmp")
                planted.symlink_to(victim)
                write(target)
                self.assertEqual(victim.read_bytes(), b"untouched\n")
                self.assertTrue(planted.is_symlink())
                self.assertFalse(target.is_symlink())
                self.assertEqual(target.read_bytes(), b"new\n")

    def test_every_write_is_0644_whatever_the_umask(self) -> None:
        # What a plain open gave under the usual umask, now independent of it: launchd
        # refuses a plist anyone but its owner can write, and a snapshot's MANIFEST and
        # RESTORE.md match the bundles beside them. A restrictive umask shows whether
        # the writer leaves mkstemp's 0600 or the caller's umask to chance.
        self.addCleanup(os.umask, os.umask(0o077))
        for kind, write in _WRITERS.items():
            with self.subTest(kind):
                target = self.scratch() / "agent.plist"
                write(target)
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_the_temp_file_sits_beside_its_target_under_an_unpredictable_name(
        self,
    ) -> None:
        # Pinned through the rename's source: its own directory keeps the rename on one
        # filesystem and so atomic, and it is never the fixed name a link could await.
        real_replace = os.replace
        for kind, write in _WRITERS.items():
            with self.subTest(kind):
                renames: "list[tuple[Path, Path]]" = []

                def recording_replace(
                    source: "str | Path", target: "str | Path"
                ) -> None:
                    renames.append((Path(source), Path(target)))
                    real_replace(source, target)

                target = self.scratch() / "agent.plist"
                with mock.patch.object(os, "replace", side_effect=recording_replace):
                    write(target)
                self.assertEqual([dest for _, dest in renames], [target])
                source = renames[0][0]
                self.assertEqual(source.parent, target.parent)
                self.assertTrue(source.name.startswith(".agent.plist."), source.name)
                suffix = atomic_write.STAGING_SUFFIX
                self.assertTrue(source.name.endswith(suffix), source.name)
                self.assertNotEqual(source.name, f".agent.plist{suffix}")

    def test_a_failed_rename_leaves_no_temp_file_and_the_target_intact(self) -> None:
        # The temp file is the writer's own to remove: a rename that fails must not
        # strand it beside the target, and the previous content must survive untouched.
        for kind, write in _WRITERS.items():
            with self.subTest(kind):
                target = self.scratch() / "agent.plist"
                target.write_bytes(b"old\n")
                failing = OSError("simulated rename")
                with mock.patch.object(os, "replace", side_effect=failing):
                    with self.assertRaisesRegex(OSError, "simulated rename"):
                        write(target)
                self.assertEqual(
                    {path.name: path.read_bytes() for path in target.parent.iterdir()},
                    {"agent.plist": b"old\n"},
                )


class HashTests(unittest.TestCase):
    def test_file_and_bytes_digests_agree_across_a_chunk_boundary(self) -> None:
        # One byte past a whole chunk: every real bundle is far larger, and a digest of the
        # first chunk alone would agree with the whole file on anything smaller.
        data = os.urandom(backup_fs._HASH_CHUNK + 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f"
            path.write_bytes(data)
            self.assertEqual(backup_fs.sha256_file(path), backup_fs.sha256_bytes(data))


class BoundedReadTests(unittest.TestCase):
    def test_a_readable_file_returns_its_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m"
            path.write_bytes(b"manifest")
            self.assertEqual(backup_fs.read_bytes_bounded(path, 5.0), b"manifest")

    def test_a_missing_file_returns_none(self) -> None:
        # Under the case's own temp dir, so the probe never depends on the host's tree.
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing" / "m"
            self.assertIsNone(backup_fs.read_bytes_bounded(missing, 5.0))

    def test_a_read_that_outlives_the_bound_returns_none(self) -> None:
        release = threading.Event()

        def blocking_reader(_: Path) -> bytes:
            release.wait(10.0)
            return b"late"

        try:
            result = backup_fs.read_bytes_bounded(
                Path("/opt/fixture-offloaded"), 0.1, reader=blocking_reader
            )
        finally:
            release.set()
        self.assertIsNone(result)


class RunLockTests(unittest.TestCase):
    def test_a_second_holder_is_refused_until_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "run.lock"
            first, second = backup_fs.RunLock(path), backup_fs.RunLock(path)
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()

    def test_release_without_acquire_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backup_fs.RunLock(Path(tmp) / "run.lock").release()

    def test_a_lock_error_other_than_contention_still_closes_the_descriptor(
        self,
    ) -> None:
        broken = OSError(errno.EIO, "fixture flock failure")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.lock"
            lock = backup_fs.RunLock(path)
            lock.acquire()
            lock.release()
            before = _open_descriptors()
            with mock.patch.object(backup_fs, "_flock", side_effect=broken):
                with self.assertRaises(OSError):
                    lock.acquire()
            self.assertEqual(_open_descriptors(), before)
            self.assertTrue(lock.acquire())
            with mock.patch.object(backup_fs, "_flock", side_effect=broken):
                with self.assertRaises(OSError):
                    lock.release()
            self.assertEqual(_open_descriptors(), before)
            # Closing the descriptor dropped the lock, so the next holder takes it.
            other = backup_fs.RunLock(path)
            self.assertTrue(other.acquire())
            other.release()

    def test_a_probe_sees_a_held_lock_without_waiting_taking_or_creating_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "run.lock"
            # Every run creates the lock file, so an absent one is free, and asking about it
            # creates nothing: a dry run that probes still writes nothing.
            self.assertFalse(backup_fs.lock_is_held(path))
            self.assertFalse(path.parent.exists())
            holder = backup_fs.RunLock(path)
            self.assertTrue(holder.acquire())
            try:
                self.assertTrue(backup_fs.lock_is_held(path))
            finally:
                holder.release()
            self.assertFalse(backup_fs.lock_is_held(path))
            # The probe let go of what it took: a real run takes the lock straight after.
            self.assertTrue(holder.acquire())
            holder.release()


def _open_descriptors() -> int:
    """How many descriptors this process holds open, as its /dev/fd lists them."""
    return len(os.listdir("/dev/fd"))


class _RecordingReal:
    """A stand-in for FakeRunner's ``real`` that records calls and runs nothing."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(
        self, argv: Sequence[str], cwd: "Path | None", timeout: float
    ) -> "tuple[int, str, str]":
        self.calls.append(list(argv))
        return 0, "recorded", ""


class FixtureTests(unittest.TestCase):
    """The fixtures themselves: fake-runner routing, and git isolation from the host."""

    def test_routes_by_program_then_by_script_and_records_calls(self) -> None:
        runner = fx.FakeRunner(
            {
                "launchctl": lambda argv: (0, "loaded", ""),
                "memory_snapshot.py": lambda argv: (0, "snap", ""),
            }
        )
        self.assertEqual(runner(["launchctl", "print"], None, 1.0), (0, "loaded", ""))
        self.assertEqual(
            runner(["/opt/py", "/opt/t/memory_snapshot.py"], None, 1.0), (0, "snap", "")
        )
        self.assertEqual(runner(["sh", "-c", "exit 0"], None, 5.0)[0], 0)
        self.assertEqual(len(runner.calls), 3)

    def test_an_unrouted_host_program_is_refused_before_real_runs(self) -> None:
        for program, argv in (
            ("launchctl", ["launchctl", "print", "gui/501"]),
            ("osascript", ["osascript", "-l", "JavaScript", "-e", "x"]),
            (
                "memory_snapshot.py",
                [
                    sys.executable,
                    "/opt/fixture/memory_snapshot.py",
                    "--mode",
                    "snapshot",
                ],
            ),
        ):
            with self.subTest(program=program):
                real = _RecordingReal()
                runner = fx.FakeRunner({}, real=real)
                with self.assertRaisesRegex(AssertionError, re.escape(program)):
                    runner(argv, None, 1.0)
                self.assertEqual(real.calls, [])
                self.assertEqual(runner.calls, [argv])

    def test_the_host_guard_holds_whatever_real_is(self) -> None:
        # The fragment matches, so without the guard this answers exit 1 and runs nothing.
        runner = fx.FakeRunner({}, real=fx.failing_when("print"))
        with self.assertRaisesRegex(AssertionError, "launchctl"):
            runner(["launchctl", "print", "gui/501"], None, 1.0)

    def test_an_unguarded_unrouted_program_still_falls_through(self) -> None:
        real = _RecordingReal()
        runner = fx.FakeRunner({"launchctl": lambda argv: (0, "", "")}, real=real)
        self.assertEqual(runner(["git", "--version"], None, 1.0), (0, "recorded", ""))
        self.assertEqual(real.calls, [["git", "--version"]])

    def test_an_inherited_repository_redirect_cannot_reach_another_repository(
        self,
    ) -> None:
        """A GIT_* redirect exported by an enclosing git hook must not retarget fixture git.

        Each variable is aimed at a separate throwaway repository. Building a fixture repo and
        committing to it, through the fixture helper and through the tool's own runner
        inside ``isolated_git_env``, must land every commit in the fixture repo and leave
        the other repository's history and index, and a foreign objects directory, untouched.
        """
        run_git = backup_proc.git_runner(backup_proc.run_cmd)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            other = fx.make_repo(root, "other")
            dot_git = other / ".git"
            # An objects directory belonging to no repository: a loose object written here
            # would be a fixture's write escaping, even though no ref moved.
            foreign_objects = root / "foreign-objects"
            foreign_objects.mkdir()
            redirects = {
                "GIT_DIR": dot_git,
                "GIT_WORK_TREE": other,
                "GIT_INDEX_FILE": dot_git / "index",
                "GIT_COMMON_DIR": dot_git,
                "GIT_OBJECT_DIRECTORY": foreign_objects,
            }
            for name, target in redirects.items():
                with self.subTest(name):
                    history = fx.git(other, "rev-list", "--all")
                    index = (dot_git / "index").read_bytes()
                    with mock.patch.dict(os.environ, {name: str(target)}):
                        repo = fx.make_repo(root, f"fixture-{name.lower()}")
                        fx.add_commit(repo, "through the fixture helper")
                        with fx.isolated_git_env():
                            code, _, err = run_git(
                                ["commit", "--quiet", "--allow-empty", "-m", "tool"],
                                repo,
                            )
                    self.assertEqual(code, 0, err)
                    # Before the count: an object written elsewhere leaves the fixture repo
                    # unreadable, and the leak should be reported as itself.
                    self.assertEqual(
                        [p for p in foreign_objects.rglob("*") if p.is_file()], []
                    )
                    self.assertEqual(fx.git(other, "rev-list", "--all"), history)
                    self.assertEqual((dot_git / "index").read_bytes(), index)
                    self.assertEqual(fx.git(repo, "rev-list", "--count", "HEAD"), "3\n")

    def test_a_fixture_commit_runs_no_hook_whatever_the_environment_says(self) -> None:
        """Inherited command-line config must not make a fixture commit run a hook.

        A parent ``git -c core.hooksPath=...`` exports GIT_CONFIG_PARAMETERS, and
        GIT_CONFIG_COUNT with its KEY/VALUE pairs does the same without any parent git.
        Aimed at a host's githooks/, a fixture's first commit would fire that host's
        reference-transaction chain. Each inherited case commits through the fixture helper
        and through the tool's runner inside ``isolated_git_env``. The last case plants the
        hook in the fixture repo's own hooks directory, which only the fixtures' own
        ``core.hooksPath`` keeps from running.
        """
        run_git = backup_proc.git_runner(backup_proc.run_cmd)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            hooks = root / "hooks"
            hooks.mkdir()
            marker = root / "markers" / "post-commit-ran"
            marker.parent.mkdir()
            hook = hooks / "post-commit"
            hook.write_text(
                f'#!/bin/sh\ntouch "{marker}"\n', encoding="utf-8", newline="\n"
            )
            hook.chmod(0o755)
            inherited = {
                "GIT_CONFIG_PARAMETERS": {
                    "GIT_CONFIG_PARAMETERS": f"'core.hooksPath'='{hooks}'"
                },
                "GIT_CONFIG_COUNT": {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "core.hooksPath",
                    "GIT_CONFIG_VALUE_0": str(hooks),
                },
            }
            for label, variables in inherited.items():
                with self.subTest(label):
                    marker.unlink(missing_ok=True)
                    with mock.patch.dict(os.environ, variables):
                        repo = fx.make_repo(root, f"fixture-{label.lower()}")
                        fx.add_commit(repo, "through the fixture helper")
                        with fx.isolated_git_env():
                            code, _, err = run_git(
                                ["commit", "--quiet", "--allow-empty", "-m", "tool"],
                                repo,
                            )
                    self.assertEqual(code, 0, err)
                    self.assertFalse(marker.exists())
            with self.subTest("a hook in the fixture repo's own hooks directory"):
                marker.unlink(missing_ok=True)
                repo = fx.make_repo(root, "fixture-own-hooks")
                shutil.copy2(hook, repo / ".git" / "hooks" / "post-commit")
                fx.add_commit(repo, "through the fixture helper")
                self.assertFalse(marker.exists())
                # And through the tool's runner: only isolated_git_env's own hooksPath keeps
                # this hook from running, which a strip-after-set ordering would delete.
                with fx.isolated_git_env():
                    code, _, err = run_git(
                        ["commit", "--quiet", "--allow-empty", "-m", "tool"], repo
                    )
                self.assertEqual(code, 0, err)
                self.assertFalse(marker.exists())
