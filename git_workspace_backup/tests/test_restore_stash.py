#!/usr/bin/env python3
"""Every stash entry survives a backup and restore, not only the newest.

``refs/stash`` names only the top entry; the older ones live in that ref's reflog, which a
bundle does not carry. This drives the real path end to end: a repository holding several
stashes is bundled, its rendered restore block runs under ``sh -c`` with a temp HOME, bounded
through bounded_run, and the restored ``git stash list`` must match the original entry for
entry, by commit and by message. One message holds shell syntax and one entry untracked
files, so quoting and a three-parent stash commit are proved too. The renderer's refusals
are pinned here as well, beside the values they guard.
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
from backup_model import BundleEntry, Manifest, StashEntry

SHELL_TIMEOUT_S = 120.0
_CREATED = "2026-09-14T06:00:00+00:00"
_BUNDLE = "workspace.bundle"
#: More than one, so a restore that keeps only the top entry is caught.
_STASH_COUNT = 3
#: One line per stash entry: its commit and its message.
_STASH_FORMAT = "--format=%H %gs"
#: A file the hostile message would create if any part of it ran as shell.
_PWNED = "pwned"
#: A message that runs a command, expands a variable and splits on spaces unless quoted.
_HOSTILE_MESSAGE = f"it's $(touch {_PWNED}) `touch {_PWNED}` $HOME"
_SHA1 = "a" * 40


def _manifest(entry: BundleEntry) -> Manifest:
    return Manifest(_CREATED, True, (entry,), (), (), (), (), (), ())


class RestoredStashTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(fx.isolated_git_env())
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.repo = fx.make_repo(self.tmp)
        messages = [f"entry {index}" for index in range(_STASH_COUNT)]
        messages[1] = _HOSTILE_MESSAGE
        for index, message in enumerate(messages):
            (self.repo / "README.md").write_text(
                f"stashed change {index}\n", encoding="utf-8", newline="\n"
            )
            (self.repo / f"untracked-{index}.txt").write_text(
                "untracked\n", encoding="utf-8", newline="\n"
            )
            fx.git(self.repo, "stash", "push", "--quiet", "-u", "-m", message)
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

    def test_every_stash_entry_is_restored_in_order(self) -> None:
        original = fx.git(self.repo, "stash", "list", _STASH_FORMAT).splitlines()
        self.assertEqual(len(original), _STASH_COUNT)
        code, stderr = self._restore(self._bundle())
        self.assertEqual(code, 0, stderr)
        restored = fx.git(self.restored, "stash", "list", _STASH_FORMAT).splitlines()
        self.assertEqual(restored, original)
        self.assertEqual(list(self.tmp.rglob(_PWNED)), [])
        # The oldest entry applies: its commit and its untracked parent were both carried.
        fx.git(self.restored, "stash", "apply", "--quiet", "stash@{2}")
        self.assertTrue((self.restored / "untracked-0.txt").is_file())

    def test_a_restore_that_does_not_rebuild_the_recorded_list_stops_the_block(
        self,
    ) -> None:
        # A real stash commit recorded twice: each store succeeds, the list does not match.
        entry = self._bundle()
        doubled = replace(entry, stashes=entry.stashes + entry.stashes[-1:])
        code, stderr = self._restore(doubled)
        self.assertNotEqual(code, 0)
        self.assertIn("the restored stash list is not the recorded one", stderr)

    def test_a_stash_value_that_could_change_what_runs_is_refused(self) -> None:
        entry = BundleEntry("workspace.bundle", _SHA1, ("refs/stash",), ())
        cases = {
            "message line break": StashEntry(_SHA1, "x\n```sh\necho pwned\n```"),
            "message carriage return": StashEntry(_SHA1, "x\ry"),
            "message fence marker": StashEntry(_SHA1, "x```y"),
            "option-like sha": StashEntry("--force", "x"),
            "uppercase sha": StashEntry("A" * 40, "x"),
            "short sha": StashEntry("a" * 39, "x"),
        }
        for label, stash in cases.items():
            manifest = _manifest(replace(entry, stashes=(stash,)))
            with self.subTest(label), self.assertRaises(brd.RestoreDocError):
                brd.render_restore_doc(manifest)

    def test_a_bundle_without_stashes_renders_no_stash_commands(self) -> None:
        entry = BundleEntry("workspace.bundle", _SHA1, ("refs/heads/main",), ())
        doc = brd.render_restore_doc(_manifest(entry))
        self.assertNotIn("stash", brd.extract_sh_blocks(doc)[0])


if __name__ == "__main__":
    unittest.main()
