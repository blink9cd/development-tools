#!/usr/bin/env python3
"""A bundle holds every ref a restore needs, and every git failure is recorded as one."""

from __future__ import annotations

import errno
import os
import shutil
import tempfile
import time
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

import _fixtures as fx

import backup_bundle as bb
import backup_proc
from backup_model import DirtyWorktree, StashEntry, WorktreeHead

#: How far ahead a touched file's mtime is set, so git's stat check always sees it changed.
_FUTURE_S = 3600.0
#: A well-formed SHA-1 object name, and an obvious fake.
_SHA1 = "a" * 40
#: A repository path no test ever creates: only fake runners are pointed at it.
_NO_REPO = Path("/opt/fixture-repo")


def _answering(code: int, out: str) -> backup_proc.RunCmd:
    """A runner that answers every command with exit *code* and stdout *out*."""

    def run(
        argv: Sequence[str], cwd: "Path | None", timeout: float
    ) -> "tuple[int, str, str]":
        return code, out, "fixture answer"

    return run


class BundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(fx.isolated_git_env())
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.repo = fx.make_repo(self.tmp)
        fx.git(self.repo, "branch", "side")
        fx.git(self.repo, "tag", "v1")
        (self.repo / "README.md").write_text(
            "changed\n", encoding="utf-8", newline="\n"
        )
        fx.git(self.repo, "stash", "--quiet")
        fx.git(
            self.repo, "worktree", "add", "--quiet", "--detach", str(self.tmp / "wt")
        )
        self.detached_only = fx.add_commit(self.tmp / "wt", "only on a detached head")
        fx.git(
            self.repo,
            "worktree",
            "add",
            "--quiet",
            "--detach",
            str(self.tmp / "wt2"),
            "main",
        )
        self.snapshot = self.tmp / "snapshot"

    def test_the_bundle_records_every_ref_and_each_worktree_head(self) -> None:
        result = bb.create_bundle(
            self.repo,
            self.snapshot,
            "workspace.bundle",
            label="workspace",
            require_refs=True,
            run=backup_proc.run_cmd,
        )
        self.assertIsNone(result.failure)
        assert result.entry is not None
        self.assertTrue((self.snapshot / "workspace.bundle").is_file())
        self.assertEqual(
            result.entry.refs,
            ("refs/heads/main", "refs/heads/side", "refs/stash", "refs/tags/v1"),
        )
        self.assertEqual(
            result.entry.head, fx.git(self.repo, "rev-parse", "HEAD").strip()
        )
        heads = {head.name: head for head in result.entry.worktree_heads}
        self.assertEqual(heads["wt"], WorktreeHead("wt", self.detached_only, False))
        self.assertTrue(heads["wt2"].reachable)
        stash_sha = fx.git(self.repo, "rev-parse", "refs/stash").strip()
        self.assertEqual([stash.sha for stash in result.entry.stashes], [stash_sha])
        self.assertTrue(result.entry.stashes[0].message.startswith("WIP on main: "))

    def test_a_repository_without_a_stash_records_none_and_reads_no_reflog(
        self,
    ) -> None:
        fx.git(self.repo, "stash", "drop", "--quiet")
        result = bb.create_bundle(
            self.repo,
            self.snapshot,
            "no-stash.bundle",
            label="workspace",
            require_refs=True,
            # The reflog walk fails outright without refs/stash, so it must not run.
            run=fx.failing_when("--walk-reflogs"),
        )
        assert result.entry is not None, result.failure
        self.assertEqual(result.entry.stashes, ())

    def test_an_empty_repository_is_skipped_or_failed_as_asked(self) -> None:
        empty = self.tmp / "empty"
        empty.mkdir()
        fx.git(empty, "init", "--quiet")
        args = (empty, self.snapshot, "memory/empty.bundle")
        skipped = bb.create_bundle(
            *args, label="m", require_refs=False, run=backup_proc.run_cmd
        )
        self.assertEqual((skipped.entry, skipped.failure), (None, None))
        failed = bb.create_bundle(
            *args, label="m", require_refs=True, run=backup_proc.run_cmd
        )
        assert failed.failure is not None
        self.assertIn("no refs", failed.failure.reason)

    def test_each_git_failure_becomes_a_failed_item(self) -> None:
        # The bundle's own name must not contain a fragment: a "list-heads.bundle" would
        # make `bundle create` match "list-heads" and fail one step early.
        # Only list-heads runs after the file is written, so only its failure names a kept
        # bundle: that file may be the run's only restorable copy, so it is never deleted.
        for index, (fragment, expected, kept) in enumerate(
            (
                ("for-each-ref --format", "for-each-ref failed", False),
                ("--walk-reflogs", "stash log failed", False),
                ("bundle create", "bundle create failed", False),
                ("list-heads", "list-heads failed", True),
            )
        ):
            with self.subTest(fragment):
                result = bb.create_bundle(
                    self.repo,
                    self.snapshot,
                    f"case-{index}.bundle",
                    label="workspace",
                    require_refs=True,
                    run=fx.failing_when(fragment),
                )
                assert result.failure is not None
                self.assertIn(expected, result.failure.reason)
                note = f"; unverified bundle kept at case-{index}.bundle"
                self.assertEqual(result.failure.reason.endswith(note), kept)
                self.assertEqual("kept" in result.failure.reason, kept)

    def test_a_bundle_path_that_cannot_be_examined_stays_that_bundles_failure(
        self,
    ) -> None:
        # Path.is_file on 3.11 and 3.12 raises for an error outside a short list, such as a
        # memory slug too long to be a name; later interpreters (3.14 measured) return False.
        # Both probes raise here as those two would.
        target = self.snapshot / "long.bundle"
        too_long = OSError(errno.ENAMETOOLONG, "fixture name too long")
        real_is_file, real_lstat = Path.is_file, Path.lstat

        def is_file(path: Path) -> bool:
            if path == target:
                raise too_long
            return real_is_file(path)

        def lstat(path: Path) -> os.stat_result:
            if path == target:
                raise too_long
            return real_lstat(path)

        with (
            mock.patch.object(Path, "is_file", is_file),
            mock.patch.object(Path, "lstat", lstat),
        ):
            result = bb.create_bundle(
                self.repo,
                self.snapshot,
                target.name,
                label="long",
                require_refs=True,
                run=fx.failing_when("bundle create"),
            )
        self.assertIsNone(result.entry)
        assert result.failure is not None
        self.assertIn("bundle create failed", result.failure.reason)
        self.assertNotIn(bb.KEPT_BUNDLE_NOTE, result.failure.reason)

    def test_a_timeout_or_missing_git_is_a_failed_item_never_an_empty_repository(
        self,
    ) -> None:
        # require_refs=False: a listing that failed must not pass for the empty repository
        # that is allowed to be skipped, and a reachability query that failed must not pass
        # for a head no branch contains.
        fragments = (
            "for-each-ref --format",
            "bundle create",
            "list-heads",
            "--contains",
        )
        codes = (backup_proc.TIMEOUT_RC, backup_proc.NOT_STARTED_RC)
        for index, (fragment, code) in enumerate(
            (fragment, code) for fragment in fragments for code in codes
        ):
            with self.subTest(fragment=fragment, code=code):
                result = bb.create_bundle(
                    self.repo,
                    self.snapshot,
                    f"case-{index}.bundle",
                    label="workspace",
                    require_refs=False,
                    run=fx.failing_when(fragment, code),
                )
                self.assertIsNone(result.entry)
                assert result.failure is not None
                self.assertIn(f"exit {code}", result.failure.reason)

    def test_a_bundle_missing_a_ref_is_a_failed_item(self) -> None:
        def lose_the_stash(
            argv: Sequence[str], cwd: "Path | None", timeout: float
        ) -> "tuple[int, str, str]":
            code, out, err = backup_proc.run_cmd(argv, cwd, timeout)
            if "list-heads" in argv:
                out = "".join(
                    line + "\n" for line in out.splitlines() if "refs/stash" not in line
                )
            return code, out, err

        result = bb.create_bundle(
            self.repo,
            self.snapshot,
            "w.bundle",
            label="w",
            require_refs=True,
            run=lose_the_stash,
        )
        assert result.failure is not None
        self.assertIn("refs/stash", result.failure.reason)

    def test_dirty_worktrees_are_counted_and_a_vanished_one_is_unreadable(self) -> None:
        (self.repo / "untracked.txt").write_text("x", encoding="utf-8", newline="\n")
        shutil.rmtree(self.tmp / "wt2")
        dirty = bb.dirty_worktrees(self.repo, run=backup_proc.run_cmd)
        self.assertIn(DirtyWorktree(self.repo.name, 1), dirty)
        self.assertIn(DirtyWorktree("wt2", -1), dirty)
        self.assertNotIn("wt", [d.path_name for d in dirty])
        with self.assertRaises(bb.BundleError):
            bb.dirty_worktrees(self.repo, run=fx.failing_when("worktree list"))

    def test_one_worktree_timing_out_is_unreadable_and_the_rest_are_still_listed(
        self,
    ) -> None:
        (self.repo / "untracked.txt").write_text("x", encoding="utf-8", newline="\n")

        def stalls_in_wt(
            argv: Sequence[str], cwd: "Path | None", timeout: float
        ) -> "tuple[int, str, str]":
            if "status" in argv and cwd is not None and cwd.name == "wt":
                return backup_proc.TIMEOUT_RC, "", "timed out after 900s: git"
            return backup_proc.run_cmd(argv, cwd, timeout)

        self.assertEqual(
            bb.dirty_worktrees(self.repo, run=stalls_in_wt),
            (DirtyWorktree(self.repo.name, 1), DirtyWorktree("wt", -1)),
        )

    def test_counting_changes_never_rewrites_a_worktrees_index(self) -> None:
        # A stale stat makes a plain `git status` refresh and rewrite the index under
        # index.lock -- the lock a commit the user is making at that moment needs.
        future = time.time() + _FUTURE_S
        os.utime(self.repo / "README.md", (future, future))
        index = self.repo / ".git" / "index"
        before = index.read_bytes()
        bb.dirty_worktrees(self.repo, run=backup_proc.run_cmd)
        self.assertEqual(index.read_bytes(), before)

    def test_untracked_files_are_counted_where_config_hides_them(self) -> None:
        fx.git(self.repo, "config", "status.showUntrackedFiles", "no")
        (self.repo / "untracked.txt").write_text("x", encoding="utf-8", newline="\n")
        self.assertIn(
            DirtyWorktree(self.repo.name, 1),
            bb.dirty_worktrees(self.repo, run=backup_proc.run_cmd),
        )


class GitOutputTests(unittest.TestCase):
    """What is read from git's output, and what a listing that fails does. No git runs."""

    def test_list_heads_ignores_lines_without_a_name(self) -> None:
        self.assertEqual(
            bb.list_heads(
                Path("x.bundle"),
                _NO_REPO,
                run=_answering(0, f"{'b' * 40}\n{'c' * 40} refs/heads/main\n"),
            ),
            {"refs/heads/main": "c" * 40},
        )

    def test_list_heads_keeps_only_lines_git_could_have_written(self) -> None:
        sha256 = "d" * 64
        lines = [
            f"{_SHA1} refs/heads/main",
            f"{sha256} refs/tags/v1",
            f"{'A' * 40} refs/heads/uppercase",
            f"{'a' * 39} refs/heads/short",
            f"{'g' * 40} refs/heads/not-hex",
            "--option-like refs/heads/option",
            f"{_SHA1}  refs/heads/leading-space",
            f"{_SHA1} refs/heads/tab\there",
            f"{_SHA1} HEAD\r",
            "",
        ]
        self.assertEqual(
            bb.parse_heads("\n".join(lines)),
            {"refs/heads/main": _SHA1, "refs/tags/v1": sha256},
        )

    def test_list_refs_keeps_only_ref_names_under_refs(self) -> None:
        out = "refs/heads/main\n\nHEAD\nrefs/tags/with space\nrefs/stash\n"
        self.assertEqual(bb.parse_refs(out), ("refs/heads/main", "refs/stash"))

    def test_the_stash_log_keeps_well_formed_entries_oldest_first(self) -> None:
        older, newer = "b" * 40, "c" * 64
        lines = [
            f"{newer}\0On main: newer entry",
            "no separator at all",
            f"{'B' * 40}\0uppercase sha",
            f"{older}\0On main: older entry",
            "",
        ]
        self.assertEqual(
            bb.parse_stash_log("\n".join(lines)),
            (
                StashEntry(older, "On main: older entry"),
                StashEntry(newer, "On main: newer entry"),
            ),
        )

    def test_a_worktree_head_names_exactly_one_component(self) -> None:
        cases = {
            "worktrees/wt/HEAD": "wt",
            "worktrees/HEAD": None,
            "worktrees//HEAD": None,
            "worktrees/a/b/HEAD": None,
            "worktrees/wt/HEADS": None,
            "refs/heads/main": None,
            "HEAD": None,
        }
        for ref, expected in cases.items():
            with self.subTest(ref=ref):
                self.assertEqual(bb.worktree_name(ref), expected)
        # No query runs for a ref that names no worktree: this runner's failure would raise.
        self.assertEqual(
            bb.worktree_heads(
                _NO_REPO,
                {"worktrees/HEAD": _SHA1, "refs/heads/main": _SHA1},
                run=_answering(1, ""),
            ),
            (),
        )

    def test_an_unsafe_bundle_name_is_a_failed_item_and_runs_nothing(self) -> None:
        runner = fx.FakeRunner({"git": lambda argv: (128, "", "git must not run")})
        snapshot = Path(self.enterContext(tempfile.TemporaryDirectory())) / "snapshot"
        for name in ("", "../escape.bundle", "/abs.bundle", "memory/./x.bundle"):
            with self.subTest(name=name):
                result = bb.create_bundle(
                    _NO_REPO, snapshot, name, label="m", require_refs=True, run=runner
                )
                self.assertIsNone(result.entry)
                assert result.failure is not None
                self.assertIn("unsafe bundle name", result.failure.reason)
        self.assertEqual(runner.calls, [])
        self.assertFalse(snapshot.exists())

    def test_a_bundle_directory_that_cannot_be_created_is_a_failed_item(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        blocker = tmp / "snapshot"
        blocker.write_text(
            "a file where a directory must go", encoding="utf-8", newline="\n"
        )
        result = bb.create_bundle(
            _NO_REPO,
            blocker,
            "memory/w.bundle",
            label="w",
            require_refs=True,
            run=_answering(0, "refs/heads/main\n"),
        )
        self.assertIsNone(result.entry)
        assert result.failure is not None
        self.assertEqual(result.failure.item, "w")
        # Named for the directory mkdir could not make. A runner answering every git command
        # would otherwise fail this bundle later -- "bundle lacks refs" -- under the same item.
        self.assertIn(str(blocker / "memory"), result.failure.reason)

    def test_a_worktree_listing_that_fails_or_names_no_worktree_raises(self) -> None:
        for code, out in (
            (0, ""),
            (0, f"HEAD {_SHA1}\0detached\0\0"),
            (backup_proc.TIMEOUT_RC, ""),
            (backup_proc.NOT_STARTED_RC, ""),
        ):
            with self.subTest(code=code, out=out):
                with self.assertRaises(bb.BundleError):
                    bb.dirty_worktrees(_NO_REPO, run=_answering(code, out))

    def test_worktree_paths_come_from_nul_separated_records(self) -> None:
        out = (
            f"worktree /opt/fixture/new\nline\0HEAD {_SHA1}\0detached\0\0"
            "worktree \0\0"
            "worktree /opt/fixture/b\0locked worktree /opt/fixture/not-a-path\0\0"
        )
        self.assertEqual(
            bb.parse_worktree_paths(out), ("/opt/fixture/new\nline", "/opt/fixture/b")
        )
