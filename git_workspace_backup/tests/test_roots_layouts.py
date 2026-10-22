#!/usr/bin/env python3
"""``--repo`` on a repository whose work tree is not its git directory's parent is refused.

The primary checkout is read as the parent of git's shared admin dir, which is right for a
plain repository and for each of its linked worktrees. A submodule keeps its git directory
under the superproject's ``.git/modules/``, and ``--separate-git-dir`` puts it anywhere, so
that parent is no work tree at all: measured, it named the superproject's ``.git/modules``
and the separate directory's parent. Both are refused, never backed up by mistake.
"""

from __future__ import annotations

import unittest
from dataclasses import replace

import _cli_case as cc
import _fixtures as fx

#: What the refusal says about the layout it found.
_REFUSAL = "is not the parent of its git directory"


class SplitLayoutTests(cc.CliCase):
    def setUp(self) -> None:
        super().setUp()
        self.elsewhere = self.tmp / "fixture-elsewhere"
        self.elsewhere.mkdir()

    def refused(self, repo: str) -> None:
        """Every mode reading --repo exits 2 with one line, and nothing is written."""
        for mode in ("run", "status", "install"):
            with self.subTest(mode=mode):
                outside = replace(self.host(), start=self.elsewhere)
                self.assertEqual(
                    self.main("--mode", mode, "--repo", repo, host=outside), 2
                )
                self.assert_one_error_line(_REFUSAL)
                self.assertIn("--repo", self.err.getvalue())
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.launchctl.argvs, [])

    def test_a_submodule_is_refused(self) -> None:
        sub = fx.make_repo(self.tmp, "fixture-sub")
        parent = fx.make_repo(self.tmp, "fixture-super")
        fx.git(
            parent,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "--quiet",
            str(sub),
            "sub",
        )
        self.refused(str(parent / "sub"))

    def test_a_separate_git_dir_repository_is_refused(self) -> None:
        work = self.tmp / "fixture-work"
        work.mkdir()
        separate = self.tmp / "fixture-separate.git"
        fx.git(
            work,
            "init",
            "--quiet",
            "--initial-branch=main",
            f"--separate-git-dir={separate}",
        )
        fx.add_commit(work, "initial")
        self.refused(str(work))

    def test_a_separate_git_dir_inside_another_repository_is_refused(self) -> None:
        # The git dir's parent is itself a work tree, of another repository: as a home
        # directory kept in git would be, holding a separate git dir made beneath it.
        outer = fx.make_repo(self.tmp, "fixture-outer")
        work = self.tmp / "fixture-inner-work"
        work.mkdir()
        separate = outer / "fixture-inner.git"
        fx.git(
            work,
            "init",
            "--quiet",
            "--initial-branch=main",
            f"--separate-git-dir={separate}",
        )
        fx.add_commit(work, "initial")
        self.refused(str(work))

    def test_a_plain_repository_and_its_linked_worktree_still_resolve(self) -> None:
        # The control: the fixture tree's checkout, named directly and through its worktree.
        for repo in (self.tree.workspace, self.tmp / "wt"):
            with self.subTest(repo=repo.name):
                outside = replace(self.host(), start=self.elsewhere)
                code = self.main(
                    "--mode", "run", "--dry-run", "--repo", str(repo), host=outside
                )
                self.assertEqual(code, 0, self.err.getvalue())


if __name__ == "__main__":
    unittest.main()
