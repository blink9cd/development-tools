#!/usr/bin/env python3
"""Which gitignored paths nobody has decided about."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from collections.abc import Sequence
from fnmatch import fnmatchcase
from pathlib import Path
from unittest import mock

import _fixtures as fx

import backup_drift as bd
import backup_proc

#: How far ahead a touched file's mtime is set, so git's stat check always sees it changed.
_FUTURE_S = 3600.0


def _answering(code: int, err: str) -> backup_proc.RunCmd:
    """A RunCmd that answers every command with exit *code* and stderr *err*."""

    def run(
        argv: Sequence[str], cwd: "Path | None", timeout: float
    ) -> "tuple[int, str, str]":
        return code, "", err

    return run


class PatternTests(unittest.TestCase):
    def test_the_matching_rules(self) -> None:
        cases = [
            ("a", "a", True),
            ("a", "a/b/c", True),
            ("a/b", "a", False),
            ("*.log", "x.log", True),
            ("*.log", "d/x.log", False),
            ("**/x.log", "d/e/x.log", True),
            ("**/x.log", "x.log", True),
            ("projects/p/src/__*", "projects/p/src/__mock", True),
            ("**/.*_cache", "utilities/t/.ruff_cache", True),
            ("?b", "ab", True),
            ("**", "anything/at/all", True),
            (".superpowers", "projects/p/.superpowers", False),
        ]
        for pattern, path, expected in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertIs(bd.pattern_matches(pattern, path), expected)

    def test_a_pattern_with_no_components_matches_and_reaches_nothing(self) -> None:
        # Configuration refuses such an entry already; this is the second guard, because an
        # empty pattern would otherwise match every path and classify every ignored file.
        for pattern in ("", "/", "//"):
            with self.subTest(pattern=pattern):
                self.assertFalse(bd.pattern_matches(pattern, "a"))
                self.assertFalse(bd.pattern_matches(pattern, "a/b"))
                self.assertFalse(bd.pattern_reaches_beneath(pattern, "a"))
                self.assertEqual(
                    bd.unclassified(["a"], extras=[pattern], regenerable=[]), ("a",)
                )

    def test_unclassified_keeps_only_paths_neither_list_matches(self) -> None:
        ignored = (".superpowers", "new_folder", "u/t/__pycache__")
        self.assertEqual(
            bd.unclassified(
                ignored, extras=[".superpowers"], regenerable=["**/__pycache__"]
            ),
            ("new_folder",),
        )

    def test_double_star_components_never_backtrack(self) -> None:
        # A backtracking matcher tries every way to split the path among the ``**``
        # components -- thousands of component matches here, and an effective hang at a few
        # dozen components. Bounded by one match per pattern component per path component.
        pattern = "**/" * 8 + "z"
        path = "/".join(["a"] * 8)
        bound = len(pattern.split("/")) * len(path.split("/"))
        with mock.patch.object(bd, "fnmatchcase", wraps=fnmatchcase) as matched:
            self.assertFalse(bd.pattern_matches(pattern, path))
        self.assertLessEqual(matched.call_count, bound)

    def test_a_walk_descends_only_where_a_pattern_could_still_match(self) -> None:
        cases = [
            (".zshrc", "Documents", False),
            (".config/gh/hosts.yml", ".config", True),
            (".config/gh/hosts.yml", ".config/other", False),
            (".superpowers", ".superpowers/sdd", True),
            ("**/*.md", "any/depth", True),
            ("a/*/*.md", "a/locked", True),
            ("a/*/*.md", "a/locked/deeper", False),
        ]
        for pattern, directory, expected in cases:
            with self.subTest(pattern=pattern, directory=directory):
                self.assertIs(bd.pattern_reaches_beneath(pattern, directory), expected)


class ParseIgnoredTests(unittest.TestCase):
    def test_a_rename_or_copy_records_original_path_is_never_read_as_a_status_line(
        self,
    ) -> None:
        for status in ("R ", " R", "C ", " C", "RM"):
            with self.subTest(status=status):
                out = f"{status} new\0!! original\0!! kept/\0"
                self.assertEqual(bd.parse_ignored(out), ("kept",))

    def test_malformed_and_empty_records_are_skipped_and_the_rest_sorted(self) -> None:
        out = "\0!! \0!! /\0!!x\0!!\0R\0!! b\0?? u\0!! a//\0 M m\0"
        self.assertEqual(bd.parse_ignored(out), ("a", "b"))


class ListIgnoredTests(unittest.TestCase):
    def _repo(self) -> Path:
        """An isolated repo whose .gitignore ignores ``ignored.txt`` and ``cache/``."""
        self.enterContext(fx.isolated_git_env())
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        repo = fx.make_repo(tmp)
        (repo / ".gitignore").write_text(
            "ignored.txt\ncache/\n", encoding="utf-8", newline="\n"
        )
        fx.git(repo, "add", ".gitignore")
        fx.git(repo, "commit", "--quiet", "-m", "ignore rules")
        return repo

    def test_lists_ignored_files_and_directories_but_not_untracked_ones(self) -> None:
        repo = self._repo()
        (repo / "ignored.txt").write_text("x", encoding="utf-8", newline="\n")
        (repo / "cache").mkdir()
        (repo / "cache" / "blob").write_text("x", encoding="utf-8", newline="\n")
        (repo / "untracked.txt").write_text("x", encoding="utf-8", newline="\n")
        self.assertEqual(
            bd.list_ignored(repo, run=backup_proc.run_cmd), ("cache", "ignored.txt")
        )

    def test_a_git_failure_raises(self) -> None:
        with self.assertRaisesRegex(bd.DriftError, "not a git repository"):
            bd.list_ignored(
                Path("/opt/fixture-repo"),
                run=_answering(128, "fatal: not a git repository"),
            )

    def test_a_timeout_or_missing_git_raises_rather_than_listing_nothing(self) -> None:
        for code, err in (
            (backup_proc.TIMEOUT_RC, "timed out after 900s: git"),
            (backup_proc.NOT_STARTED_RC, "could not start git"),
        ):
            with self.subTest(code=code):
                with self.assertRaisesRegex(bd.DriftError, err):
                    bd.list_ignored(
                        Path("/opt/fixture-repo"), run=_answering(code, err)
                    )

    def test_a_renamed_file_whose_original_name_looks_like_a_status_is_not_listed(
        self,
    ) -> None:
        repo = self._repo()
        (repo / "!! original").write_text("x", encoding="utf-8", newline="\n")
        fx.git(repo, "add", "!! original")
        fx.git(repo, "commit", "--quiet", "-m", "odd name")
        fx.git(repo, "mv", "!! original", "renamed")
        (repo / "ignored.txt").write_text("x", encoding="utf-8", newline="\n")
        self.assertEqual(
            bd.list_ignored(repo, run=backup_proc.run_cmd), ("ignored.txt",)
        )

    def test_a_checkout_that_hides_untracked_files_still_lists_ignored_ones(
        self,
    ) -> None:
        # git refuses --ignored=matching alongside this setting, so without an explicit
        # untracked mode one config line would fail every drift check.
        repo = self._repo()
        fx.git(repo, "config", "status.showUntrackedFiles", "no")
        (repo / "ignored.txt").write_text("x", encoding="utf-8", newline="\n")
        self.assertEqual(
            bd.list_ignored(repo, run=backup_proc.run_cmd), ("ignored.txt",)
        )

    def test_listing_never_rewrites_the_checkouts_index(self) -> None:
        # A stale stat makes a plain `git status` refresh and rewrite the index under
        # index.lock -- the lock a commit the user is making at that moment needs.
        repo = self._repo()
        future = time.time() + _FUTURE_S
        os.utime(repo / "README.md", (future, future))
        index = repo / ".git" / "index"
        before = index.read_bytes()
        bd.list_ignored(repo, run=backup_proc.run_cmd)
        self.assertEqual(index.read_bytes(), before)
