#!/usr/bin/env python3
"""RESTORE.md's commands actually restore, proven by running them.

Only a fence whose info string is exactly ``sh`` is ever extracted, so the steps that
overwrite a home directory, kept in ``text`` fences, never run. A manifest value that could
end a fence early, turning the text after it into a block that does run, is refused
before anything is rendered, and so is a head that is not a full object id.

The round trips build a real snapshot from fixture repositories and run every extracted
block with ``sh -e``, with ``HOME`` and ``XDG_CONFIG_HOME`` at empty temp directories, so a
block that reached for either could never touch the host's.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import _fixtures as fx

import backup_build as bld
import backup_restore_doc as brd
import bounded_run
from backup_model import BundleEntry, FileEntry, Manifest, WorktreeHead

SHELL_TIMEOUT_S = 120.0
_CREATED = "2026-09-14T06:00:00+00:00"
_HEAD = "a" * 40
_WORKTREE_SHA = "b" * 40
#: A worktree name that closes its sh fence and puts a command on the line after it.
_BREAKOUT = "wt\n```\necho pwned"
#: A tilde that begins a shell word, which the shell expands to a home directory.
_WORD_TILDE = re.compile(r"(?:^|\s)~", re.MULTILINE)
#: A single quote and both spellings of command substitution: legal as a directory name and
#: as a ref component, and inert only when every interpolation is quoted.
_HOSTILE_WORKTREE = "s'$(touch${IFS}pwned)`touch${IFS}pwned`"
#: The same for a memory project's slug, which becomes both a bundle name and a directory.
_HOSTILE_SLUG = "-m q'$(touch pwned)"
#: What an executed substitution above would create.
_PWNED = "pwned"
#: The heading of the manual step naming what the restore places under a project slug.
_SLUG_HEADING = "## Claude project folders"


def _manifest(
    bundles: "tuple[BundleEntry, ...]",
    *,
    complete: bool = True,
    created: str = _CREATED,
) -> Manifest:
    return Manifest(
        created=created,
        complete=complete,
        bundles=bundles,
        files=(),
        archive_members=(),
        dirty_worktrees=(),
        failed=(),
        skipped_symlinks=(),
        unclassified_ignored=(),
    )


def _workspace_entry(*worktree_names: str, head: str = _HEAD) -> BundleEntry:
    heads = tuple(WorktreeHead(name, _WORKTREE_SHA, False) for name in worktree_names)
    return BundleEntry("workspace.bundle", head, ("refs/heads/main",), heads)


class RenderTests(unittest.TestCase):
    def test_restore_dirs(self) -> None:
        self.assertEqual(brd.restore_dir("workspace.bundle"), "workspace")
        self.assertEqual(
            brd.restore_dir("memory/-fixture-proj.bundle"),
            "claude/projects/-fixture-proj/memory",
        )

    def test_checkout_and_worktree_lines_appear_only_when_needed(self) -> None:
        with_head = _workspace_entry("wt")
        unborn = BundleEntry("memory/x.bundle", "", (), ())
        doc = brd.render_restore_doc(_manifest((with_head, unborn), complete=False))
        # The checkout names no commit, so it keeps the branch the mirror chose; the recorded
        # head is then proved, where checking it out by sha would leave HEAD detached.
        self.assertIn("checkout --quiet --force\n", doc)
        self.assertNotIn(f"checkout --quiet --force {_HEAD}", doc)
        self.assertIn(f'rev-parse HEAD)" = {_HEAD} ]', doc)
        self.assertIn("worktrees/wt/HEAD:refs/backup/worktrees/wt/HEAD", doc)
        self.assertEqual(doc.count("checkout --quiet --force"), 1)
        self.assertEqual(doc.count("rev-parse HEAD"), 1)
        self.assertIn("complete: no", doc)

    def test_only_sh_blocks_are_extracted(self) -> None:
        doc = "# t\n\n```text\nnot run\n```\n\n```sh\necho one\n```\n\n```sh\necho two\n```\n"
        self.assertEqual(brd.extract_sh_blocks(doc), ("echo one\n", "echo two\n"))

    def test_only_a_fence_whose_info_string_is_exactly_sh_is_extracted(self) -> None:
        for info in ("text", "shell", "bash", "zsh", "sh something", "sh ", ""):
            with self.subTest(info=info):
                doc = f"# t\n\n```{info}\necho {info!r}\n```\n\n```sh\necho kept\n```\n"
                self.assertEqual(brd.extract_sh_blocks(doc), ("echo kept\n",))

    def test_a_fence_left_open_never_swallows_a_later_block(self) -> None:
        home_line = 'cp -R home/. "$HOME/"'
        text_block = f"```text\n{home_line}\n```\n"
        later = "\n```sh\necho later\n```\n"
        cases = {
            "unterminated": f"```sh\necho a\n\n{text_block}{later}",
            "closer with a trailing space": f"```sh\necho a\n``` \n\n{text_block}{later}",
        }
        for label, doc in cases.items():
            with self.subTest(label):
                # The broken block yields nothing, and scanning goes on to the next one.
                self.assertEqual(brd.extract_sh_blocks(doc), ("echo later\n",))

    def test_the_intro_sets_both_variables_absolutely_in_a_text_fence(self) -> None:
        doc = brd.render_restore_doc(_manifest((_workspace_entry(),)))
        exports = (
            'export SNAPSHOT="$(CDPATH= cd -- "<path to this folder>" && pwd)"',
            'export RESTORE_ROOT="$(CDPATH= cd -- "<empty directory>" && pwd)"',
        )
        self.assertIn(f"```text\n{exports[0]}\n{exports[1]}\n```\n", doc)
        for block in brd.extract_sh_blocks(doc):
            with self.subTest(block=block):
                self.assertNotIn("export", block)
        # A whole-document paste runs on past the first block's refusal, so say to stop.
        intro = doc[: doc.index("```sh")]
        self.assertIn("If the first block fails, stop", intro)

    def test_the_memory_step_names_each_restored_corpus_only_with_a_memory_bundle(
        self,
    ) -> None:
        memory = (
            BundleEntry("memory/-p.bundle", _HEAD, (), ()),
            BundleEntry(f"memory/{_HOSTILE_SLUG}.bundle", _HEAD, (), ()),
        )
        doc = brd.render_restore_doc(_manifest((_workspace_entry(), *memory)))
        step = doc[doc.index(_SLUG_HEADING) :]
        self.assertIn("`~/.claude/projects/<slug>/memory`", step)
        self.assertIn("`~/.claude/projects/<slug>/`", step)
        self.assertIn("encoding of the restored workspace's absolute path", step)
        body = step[step.index("```text\n") + len("```text\n") :]
        paths = body[: body.index("```")].split("\n")[:-1]
        # Parsed back as the shell would read them: one word each, the hostile slug intact.
        self.assertEqual(
            [shlex.split(line) for line in paths],
            [
                ["$RESTORE_ROOT/claude/projects/-p/memory"],
                [f"$RESTORE_ROOT/claude/projects/{_HOSTILE_SLUG}/memory"],
            ],
        )
        self.assertEqual(len(brd.extract_sh_blocks(doc)), len(memory) + 2)
        without = brd.render_restore_doc(_manifest((_workspace_entry(),)))
        self.assertNotIn(_SLUG_HEADING, without)
        self.assertNotIn("claude/projects", without)

    def test_the_slug_step_names_each_archived_project_folder_without_memory(
        self,
    ) -> None:
        # Kept beside a memory corpus, archived through claude_files, and so placed under
        # the recorded slug by the manual cp step: the slug warning must cover it too.
        members = (
            "claude/projects/-p/kept/a.txt",
            "claude/projects/-p/kept/b.txt",
            f"claude/projects/{_HOSTILE_SLUG}/other/c.txt",
            "claude/projects/loose.json",
            "claude/settings.json",
            "workspace/projects/-q/x.txt",
        )
        manifest = replace(
            _manifest((_workspace_entry(),)),
            archive_members=tuple(FileEntry(name, "0" * 64, 1) for name in members),
        )
        doc = brd.render_restore_doc(manifest)
        step = doc[doc.index(_SLUG_HEADING) :]
        self.assertIn("`~/.claude/projects/<slug>/`", step)
        self.assertIn("encoding of the restored workspace's absolute path", step)
        body = step[step.index("```text\n") + len("```text\n") :]
        paths = body[: body.index("```")].split("\n")[:-1]
        self.assertEqual(
            [shlex.split(line) for line in paths],
            [
                ["$RESTORE_ROOT/extras/claude/projects/-p"],
                [f"$RESTORE_ROOT/extras/claude/projects/{_HOSTILE_SLUG}"],
            ],
        )
        self.assertEqual(len(brd.extract_sh_blocks(doc)), 2)

    def test_an_archived_slug_that_could_end_a_fence_is_refused(self) -> None:
        for slug in ("a\nb", "a\rb", "a```b"):
            with self.subTest(slug=slug):
                manifest = replace(
                    _manifest((_workspace_entry(),)),
                    archive_members=(
                        FileEntry(f"claude/projects/{slug}/f", "0" * 64, 1),
                    ),
                )
                with self.assertRaises(brd.RestoreDocError):
                    brd.render_restore_doc(manifest)

    def test_only_the_bundle_names_the_build_writes_are_accepted(self) -> None:
        def entry(name: str) -> BundleEntry:
            return BundleEntry(name, _HEAD, (), ())

        shape = "not a bundle name the build writes"
        cases = {
            "nested under workspace": ((entry("workspace/evil.bundle"),), shape),
            "claude": ((entry("claude.bundle"),), shape),
            "nested memory slug": ((entry("memory/a/b.bundle"),), shape),
            "the extras directory": ((entry("extras.bundle"),), "overlap"),
            "nested beside its parent": (
                (entry("workspace.bundle"), entry("workspace/evil.bundle")),
                "overlap",
            ),
            "duplicate workspace": ((entry("workspace.bundle"),) * 2, "duplicate"),
            "duplicate memory": ((entry("memory/x.bundle"),) * 2, "duplicate"),
        }
        for label, (bundles, reason) in cases.items():
            with self.subTest(label):
                with self.assertRaises(brd.RestoreDocError) as caught:
                    brd.render_restore_doc(_manifest(bundles))
                self.assertIn(reason, str(caught.exception))
        accepted = (
            entry("workspace.bundle"),
            entry("memory/a.bundle"),
            entry("memory/-b c'.bundle"),
        )
        blocks = brd.extract_sh_blocks(brd.render_restore_doc(_manifest(accepted)))
        self.assertEqual(len(blocks), len(accepted) + 1)
        # The builder writes memory/<slug>.bundle for any one-component slug, even one that
        # itself ends in ".bundle", and restore_dir strips only the last suffix.
        doubled = entry("memory/x.bundle.bundle")
        doc = brd.render_restore_doc(_manifest((entry("workspace.bundle"), doubled)))
        self.assertIn('"/claude/projects/x.bundle/memory/.git', doc)

    def test_every_run_block_is_one_subshell_whose_every_command_reads_its_root(
        self,
    ) -> None:
        refusal = "RESTORE_ROOT must be empty:"
        manifests = {
            "with bundles": (
                _workspace_entry("wt"),
                BundleEntry("memory/a.bundle", _HEAD, (), ()),
            ),
            "without bundles": (),
        }
        for label, bundles in manifests.items():
            with self.subTest(label):
                blocks = brd.extract_sh_blocks(
                    brd.render_restore_doc(_manifest(bundles))
                )
                # Only the first block checks: every later one sees the earlier output.
                self.assertEqual(
                    [refusal in block for block in blocks],
                    [True] + [False] * (len(blocks) - 1),
                )
                for block in blocks:
                    lines = block.splitlines()
                    self.assertEqual((lines[:2], lines[-1]), (["(", "set -eu"], ")"))
                    # Every block, not only the first, refuses a relative root or snapshot
                    # before anything else: a later block pasted alone checks nothing else.
                    self.assertTrue(lines[2].startswith('case "${RESTORE_ROOT:?'))
                    self.assertTrue(lines[3].startswith('case "${SNAPSHOT:?'))
                    self.assertNotIn("target", block)
                    for line in lines[2:-1]:
                        self.assertIsNone(re.search(r"(?:^|[;&|({]\s*)exit\b", line))
                    for line in lines[4:-1]:
                        self.assertIn('"${RESTORE_ROOT:?', line)

    def test_bundle_names_are_built_from_the_builders_one_suffix(self) -> None:
        self.assertEqual(bld.BUNDLE_SUFFIX, ".bundle")
        self.assertEqual(bld.WORKSPACE_BUNDLE, f"workspace{bld.BUNDLE_SUFFIX}")

    def test_restore_targets_overlap_only_when_equal_or_nested_by_component(
        self,
    ) -> None:
        cases = (
            ("workspace", "workspace", True),
            ("workspace", "workspace/evil", True),
            ("workspace/evil", "workspace", True),
            ("work", "workspace", False),
            ("claude/projects/a/memory", "claude/projects/ab/memory", False),
            ("extras", "claude/projects/x/memory", False),
        )
        for first, second, expected in cases:
            with self.subTest(first=first, second=second):
                self.assertIs(brd.restore_targets_overlap(first, second), expected)

    def test_no_extracted_block_reaches_the_home_directory(self) -> None:
        bundles = (
            _workspace_entry("wt"),
            BundleEntry("memory/-p.bundle", _HEAD, (), ()),
        )
        doc = brd.render_restore_doc(_manifest(bundles))
        # The document does carry steps that write the home directory, so a pass below is
        # not for want of them: they must sit only in fences that are never extracted.
        self.assertIn('cp -R home/. "$HOME/"', doc)
        blocks = brd.extract_sh_blocks(doc)
        self.assertEqual(len(blocks), len(bundles) + 1)
        for block in blocks:
            with self.subTest(block=block):
                self.assertNotIn("HOME", block)
                self.assertIsNone(_WORD_TILDE.search(block))

    def test_a_value_that_could_end_its_fence_early_is_refused(self) -> None:
        cases = {
            "worktree name": _manifest((_workspace_entry(_BREAKOUT),)),
            "worktree carriage return": _manifest((_workspace_entry("wt\rx"),)),
            "worktree fence marker": _manifest((_workspace_entry("wt```x"),)),
            "bundle name": _manifest(
                (
                    replace(
                        _workspace_entry(), name="x\n```sh\necho pwned\n```\n.bundle"
                    ),
                )
            ),
            "bundle carriage return": _manifest(
                (replace(_workspace_entry(), name="x\r.bundle"),)
            ),
            "bundle fence marker": _manifest(
                (replace(_workspace_entry(), name="x```.bundle"),)
            ),
            "created": _manifest((), created="now\n```sh\necho pwned\n```"),
        }
        for label, manifest in cases.items():
            with self.subTest(label), self.assertRaises(brd.RestoreDocError):
                brd.render_restore_doc(manifest)

    def test_a_head_that_is_not_a_full_lowercase_object_id_is_refused(self) -> None:
        refused = (
            "A" * 40,
            "a" * 39,
            "a" * 41,
            "g" * 40,
            "a" * 63,
            "--force",
            _HEAD + "\n",
        )
        for head in refused:
            with self.subTest(head=head):
                with self.assertRaises(brd.RestoreDocError) as caught:
                    brd.render_restore_doc(_manifest((_workspace_entry(head=head),)))
                self.assertIn(repr(head), str(caught.exception))
        for head in ("0" * 40, "f" * 64):
            with self.subTest(head=head):
                doc = brd.render_restore_doc(_manifest((_workspace_entry(head=head),)))
                self.assertIn(f'rev-parse HEAD)" = {head} ]', doc)
        doc = brd.render_restore_doc(_manifest((_workspace_entry(head=""),)))
        self.assertNotIn("checkout --quiet --force", doc)
        self.assertNotIn("rev-parse HEAD", doc)

    def test_a_name_unsafe_as_a_path_or_a_ref_is_refused(self) -> None:
        bundle_names = (
            "../x.bundle",
            "/x.bundle",
            "memory/../x.bundle",
            "a//b.bundle",
            # Each is a safe name whose restore directory is not: "..", "x/..",
            # "claude/projects/../memory", "" and "claude/projects//memory".
            "...bundle",
            "x/...bundle",
            "memory/...bundle",
            ".bundle",
            "memory/.bundle",
            # No suffix: not a bundle name this tool writes.
            "workspace",
        )
        for name in bundle_names:
            with self.subTest(bundle=name), self.assertRaises(brd.RestoreDocError):
                brd.render_restore_doc(
                    _manifest((replace(_workspace_entry(), name=name),))
                )
        for name in ("*", "a:b", ".hidden", "x.lock", "a b", ""):
            with self.subTest(worktree=name), self.assertRaises(brd.RestoreDocError):
                brd.render_restore_doc(_manifest((_workspace_entry(name),)))


@unittest.skipUnless(
    shutil.which("unzip"), "unzip is required to execute the files block"
)
class RoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(fx.isolated_git_env())
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.tree = fx.make_tree(self.tmp)
        self.restore = self.tmp / "restore"
        self.home = self.tmp / "restore-home"
        self.config_home = self.tmp / "restore-config"

    def _restore(self) -> bld.BuildResult:
        """Build the tree, render its RESTORE.md and run every sh block in it, in order."""
        result = bld.build_snapshot(self.tree.request, run=fx.FakeRunner(fx.MEMORY_OK))
        self.assertEqual(result.failed, ())
        blocks = brd.extract_sh_blocks(
            brd.render_restore_doc(_manifest(result.bundles))
        )
        self.assertEqual(len(blocks), len(result.bundles) + 1)
        for directory in (self.restore, self.home, self.config_home):
            directory.mkdir()
        env = {
            **os.environ,
            **fx.ISOLATED_GIT_ENV,
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.config_home),
            "SNAPSHOT": str(self.tree.staging),
            "RESTORE_ROOT": str(self.restore),
        }
        for block in blocks:
            done = bounded_run.run_bounded(
                ["sh", "-e", "-c", block],
                timeout=SHELL_TIMEOUT_S,
                cwd=self.tmp,
                env=env,
            )
            self.assertEqual(
                done.returncode, 0, f"block failed:\n{block}\n{done.stderr}"
            )
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertEqual(list(self.config_home.iterdir()), [])
        return result

    def test_every_sh_block_restores_the_snapshot(self) -> None:
        self._restore()
        workspace = self.restore / "workspace"
        # The fixture workspace is on main, and the restored checkout is too, not detached.
        self.assertEqual(
            fx.git(workspace, "symbolic-ref", "HEAD").strip(), "refs/heads/main"
        )
        refs = set(fx.git(workspace, "for-each-ref", "--format=%(refname)").split())
        expected = {
            "refs/heads/main",
            "refs/heads/side",
            "refs/tags/v1",
            "refs/stash",
            "refs/backup/worktrees/wt/HEAD",
        }
        self.assertTrue(expected <= refs, refs)
        self.assertEqual(
            fx.git(workspace, "rev-parse", "refs/backup/worktrees/wt/HEAD").strip(),
            self.tree.detached_only,
        )
        self.assertEqual(fx.git(workspace, "status", "--porcelain"), "")
        self.assertEqual(
            (workspace / "blob.bin").read_bytes(),
            (self.tree.workspace / "blob.bin").read_bytes(),
        )
        memory = self.restore / "claude" / "projects" / "-fixture-proj" / "memory"
        self.assertTrue((memory / "README.md").is_file())
        self.assertEqual(
            (self.restore / "extras" / "home" / ".zshrc").read_text(encoding="utf-8"),
            "z",
        )

    def test_names_ending_in_a_dot_or_holding_shell_syntax_restore_exactly(
        self,
    ) -> None:
        shas = {}
        for name in ("x.", _HOSTILE_WORKTREE):
            fx.git(
                self.tree.workspace,
                "worktree",
                "add",
                "--quiet",
                "--detach",
                str(self.tmp / name),
            )
            shas[name] = fx.add_commit(self.tmp / name, "only on this worktree")
        projects = self.tree.request.roots.claude_home / "projects"
        fx.make_repo(projects / _HOSTILE_SLUG, name="memory")
        result = self._restore()
        recorded = {head.name for head in result.bundles[0].worktree_heads}
        self.assertLessEqual(set(shas), recorded)
        workspace = self.restore / "workspace"
        for name, sha in shas.items():
            with self.subTest(worktree=name):
                ref = f"refs/backup/worktrees/{name}/HEAD"
                resolved = fx.git(
                    workspace, "rev-parse", "--verify", "--end-of-options", ref
                )
                self.assertEqual(resolved.strip(), sha)
        memory = self.restore / "claude" / "projects" / _HOSTILE_SLUG / "memory"
        self.assertTrue((memory / "README.md").is_file())
        self.assertEqual(list(self.tmp.rglob(_PWNED)), [])


if __name__ == "__main__":
    unittest.main()
