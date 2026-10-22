#!/usr/bin/env python3
"""A restored checkout's HEAD is where the snapshot's was, and that is proved, not assumed.

The restore block checks out without naming a commit, which keeps whatever HEAD the mirror
clone chose, then compares HEAD with the head the manifest recorded. Clone chooses a branch
only when its tip is the bundle's HEAD commit, and otherwise leaves HEAD detached at that
commit: an attached primary restores on its branch, and a detached one restores detached, each
at the recorded head. A recorded head the restore does not reproduce stops the block, with a
message that does not appear verbatim in the block's own text.

Every repository and bundle is real git in a temp dir, and each block runs under ``sh -c``
with a temp HOME, bounded through bounded_run.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import _fixtures as fx

import backup_bundle as bb
import backup_proc
import backup_restore_doc as brd
import bounded_run
from backup_model import BundleEntry, Manifest

SHELL_TIMEOUT_S = 120.0
_CREATED = "2026-09-14T06:00:00+00:00"
_BUNDLE = "workspace.bundle"
#: What a block prints when the restored HEAD is not the recorded head, restated.
_HEAD_REFUSAL = "HEAD is not the recorded head"


def _manifest(entry: BundleEntry) -> Manifest:
    return Manifest(_CREATED, True, (entry,), (), (), (), (), (), ())


class RestoredHeadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(fx.isolated_git_env())
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.repo = fx.make_repo(self.tmp)
        fx.add_commit(self.repo, "second")
        fx.add_commit(self.repo, "third")
        self.snapshot = self.tmp / "snapshot"
        self.restore = self.tmp / "restore"
        self.restored = self.restore / "workspace"

    def _bundle(self) -> BundleEntry:
        result = bb.create_bundle(
            self.repo,
            self.snapshot,
            _BUNDLE,
            label=_BUNDLE,
            require_refs=True,
            run=backup_proc.run_cmd,
        )
        assert result.entry is not None, result.failure
        return result.entry

    def _restore(self, entry: BundleEntry) -> tuple[int, str]:
        """Run *entry*'s restore block into a fresh RESTORE_ROOT; its exit code and stderr."""
        block = brd.extract_sh_blocks(brd.render_restore_doc(_manifest(entry)))[0]
        self.assertNotIn(_HEAD_REFUSAL, block)
        self.restore.mkdir()
        home = self.tmp / "home"
        home.mkdir()
        env = {
            **os.environ,
            **fx.ISOLATED_GIT_ENV,
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / "config"),
            "SNAPSHOT": str(self.snapshot),
            "RESTORE_ROOT": str(self.restore),
        }
        done = bounded_run.run_bounded(
            ["sh", "-c", block], timeout=SHELL_TIMEOUT_S, cwd=self.tmp, env=env
        )
        return done.returncode, done.stderr

    def test_an_attached_primary_restores_on_its_branch_at_the_recorded_head(
        self,
    ) -> None:
        entry = self._bundle()
        code, stderr = self._restore(entry)
        self.assertEqual(code, 0, stderr)
        symbolic = fx.git(self.restored, "symbolic-ref", "HEAD")
        self.assertEqual(symbolic.strip(), "refs/heads/main")
        self.assertEqual(fx.git(self.restored, "rev-parse", "HEAD").strip(), entry.head)
        self.assertEqual(fx.git(self.restored, "status", "--porcelain"), "")

    def test_a_detached_primary_restores_detached_at_the_recorded_head(self) -> None:
        # A commit on main that no branch tips, so no branch can stand in for it.
        fx.git(self.repo, "checkout", "--quiet", "--detach", "HEAD~1")
        entry = self._bundle()
        self.assertEqual(entry.head, fx.git(self.repo, "rev-parse", "HEAD").strip())
        code, stderr = self._restore(entry)
        self.assertEqual(code, 0, stderr)
        abbreviated = fx.git(self.restored, "rev-parse", "--abbrev-ref", "HEAD")
        self.assertEqual(abbreviated.strip(), "HEAD")
        self.assertEqual(fx.git(self.restored, "rev-parse", "HEAD").strip(), entry.head)
        self.assertEqual(fx.git(self.restored, "status", "--porcelain"), "")

    def test_a_recorded_head_the_restore_does_not_reproduce_stops_the_block(
        self,
    ) -> None:
        # A real commit in the bundle, only not its HEAD: checking it out by sha would pass.
        other = fx.git(self.repo, "rev-parse", "HEAD~1").strip()
        code, stderr = self._restore(replace(self._bundle(), head=other))
        self.assertNotEqual(code, 0)
        self.assertIn(f"{_HEAD_REFUSAL} {other}", stderr)


if __name__ == "__main__":
    unittest.main()
