#!/usr/bin/env python3
"""The config table's shape rules, and which checkout a run reads from."""

from __future__ import annotations

import re
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

import _fixtures as fx

import backup_config as bc
import backup_proc
import backup_roots as br

#: A home that is never looked up on disk: only for checks that touch no filesystem.
HOME = Path("/opt/fixture-home")
VALID = """
[tool.workspace_backup]
destination = "~/Library/Mobile Documents/com~apple~CloudDocs/workspace-backup"
extras = [".superpowers", "projects/site/src/__*"]
regenerable = ["**/__pycache__"]
claude_files = ["settings.json"]
home_files = [".zshrc"]
"""


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        # Resolved: pyproject_config reports the pyproject it found by its resolved path, and
        # the macOS temp dir sits behind a symlink. Every destination a test expects is rooted
        # here too, because the loader resolves it against the real filesystem.
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()

    def _load(self, text: str, home: Path = HOME) -> bc.BackupConfig:
        (self.root / "pyproject.toml").write_text(text, encoding="utf-8", newline="\n")
        return bc.load_config(self.root, home=home)

    def test_a_valid_table_loads_with_the_default_keep(self) -> None:
        home = self.root / "fixture-home"
        self.assertEqual(
            self._load(VALID, home),
            bc.BackupConfig(
                pyproject=self.root / "pyproject.toml",
                destination=home
                / "Library/Mobile Documents/com~apple~CloudDocs/workspace-backup",
                keep=bc.DEFAULT_KEEP,
                extras=(".superpowers", "projects/site/src/__*"),
                regenerable=("**/__pycache__",),
                claude_files=("settings.json",),
                home_files=(".zshrc",),
            ),
        )

    def test_omitted_lists_are_empty_and_an_absolute_destination_is_kept(self) -> None:
        config = self._load(
            f"[tool.workspace_backup]\ndestination = '{self.root}/fixture-dest'\nkeep = 3\n"
        )
        self.assertEqual(
            config,
            bc.BackupConfig(
                pyproject=self.root / "pyproject.toml",
                destination=self.root / "fixture-dest",
                keep=3,
                extras=(),
                regenerable=(),
                claude_files=(),
                home_files=(),
            ),
        )

    def test_memory_snapshot_is_off_unless_the_table_turns_it_on(self) -> None:
        """A workspace without the memory_snapshot tool must not try to run it."""
        body = f"[tool.workspace_backup]\ndestination = '{self.root}/fixture-dest'\n"
        self.assertFalse(self._load(body).memory_snapshot)
        self.assertTrue(self._load(body + "memory_snapshot = true\n").memory_snapshot)
        self.assertFalse(self._load(body + "memory_snapshot = false\n").memory_snapshot)

    def test_rearm_commands_are_absent_unless_listed_and_keep_their_order(self) -> None:
        """Absent means the restore document's own default, so None, not empty."""
        body = f"[tool.workspace_backup]\ndestination = '{self.root}/fixture-dest'\n"
        self.assertIsNone(self._load(body).rearm_commands)
        listed = body + "rearm_commands = ['python b.py', 'python a.py --x \"$PWD\"']\n"
        self.assertEqual(
            self._load(listed).rearm_commands,
            ("python b.py", 'python a.py --x "$PWD"'),
        )

    def test_setup_doc_is_absent_unless_named(self) -> None:
        """The re-arm step names the workspace's setup document only when told which."""
        body = f"[tool.workspace_backup]\ndestination = '{self.root}/fixture-dest'\n"
        self.assertIsNone(self._load(body).setup_doc)
        named = body + "setup_doc = 'docs/FIXTURE_SETUP.md'\n"
        self.assertEqual(self._load(named).setup_doc, "docs/FIXTURE_SETUP.md")

    def test_a_destination_loads_resolved_so_one_place_has_one_spelling(self) -> None:
        """The ledger is keyed on the destination string, so every spelling must agree.

        The destination does not exist yet in any case below: resolution must not require it.
        """
        real = self.root / "real"
        real.mkdir()
        link = self.root / "link"
        link.symlink_to(real, target_is_directory=True)
        spellings = {
            "through a symlinked directory": (f"{link}/backup", HOME),
            "with a parent segment": (f"{real}/missing/../backup", HOME),
            "home through a symlinked directory": ("~/backup", link),
        }
        for label, (spelled, home) in spellings.items():
            with self.subTest(label):
                config = self._load(
                    f"[tool.workspace_backup]\ndestination = '{spelled}'\n", home
                )
                self.assertEqual(config.destination, real / "backup")

    def test_a_destination_through_a_symlink_loop_still_loads(self) -> None:
        """A link loop is for the run to report when it writes, not for the loader to crash on.

        Pins why the loader resolves with non-strict ``os.path.realpath``: ``Path.resolve``
        raises RuntimeError on a loop on 3.11 and 3.12. From 3.13 the two agree, so this test
        goes red for a ``Path.resolve`` loader only when run on a 3.11 or 3.12 interpreter.
        """
        (self.root / "a").symlink_to(self.root / "b")
        (self.root / "b").symlink_to(self.root / "a")
        config = self._load(
            f"[tool.workspace_backup]\ndestination = '{self.root}/a/backup'\n"
        )
        self.assertTrue(config.destination.is_absolute())

    def test_expand_home_handles_bare_tilde_prefix_and_plain_paths(self) -> None:
        self.assertEqual(bc.expand_home("~", HOME), HOME)
        self.assertEqual(bc.expand_home("~/x", HOME), HOME / "x")
        self.assertEqual(bc.expand_home("/opt/x", HOME), Path("/opt/x"))

    def test_a_missing_pyproject_or_table_is_an_error(self) -> None:
        with self.assertRaisesRegex(bc.ConfigError, "no pyproject.toml"):
            bc.load_config(
                self.root,
                home=HOME,
                is_file=lambda path: False,
                read_text=lambda path: "",
            )
        with self.assertRaisesRegex(
            bc.ConfigError, "no \\[tool.workspace_backup\\] table"
        ):
            self._load("[tool.other]\nx = 1\n")

    def test_a_malformed_file_is_an_error(self) -> None:
        with self.assertRaises(bc.ConfigError):
            self._load("[tool.workspace_backup\n")

    def test_every_shape_violation_is_an_error_naming_its_key(self) -> None:
        # Keyed by the key each message must name. The entries after "parent entry" are
        # refused by the manifest's name predicate, which a normalizing path rule would
        # accept: they must never reach a zip member name.
        cases: dict[str, dict[str, str]] = {
            "surprise": {"unknown key": 'destination = "/opt/d"\nsurprise = 1'},
            "destination": {
                "no destination": "keep = 3",
                "destination not a string": "destination = 3",
                "relative destination": 'destination = "relative/dir"',
                "destination holds a NUL": 'destination = "/opt/d\\u0000x"',
            },
            "keep": {
                "keep is a bool": 'destination = "/opt/d"\nkeep = true',
                "keep is zero": 'destination = "/opt/d"\nkeep = 0',
            },
            "extras": {
                "list not a list": 'destination = "/opt/d"\nextras = ".superpowers"',
                "empty entry": 'destination = "/opt/d"\nextras = [""]',
                "non-string entry": 'destination = "/opt/d"\nextras = [1]',
                "parent entry": 'destination = "/opt/d"\nextras = ["../outside"]',
                "trailing-slash entry": 'destination = "/opt/d"\nextras = [".superpowers/"]',
                "bare dot entry": 'destination = "/opt/d"\nextras = ["."]',
            },
            "home_files": {
                "absolute entry": 'destination = "/opt/d"\nhome_files = ["/etc/passwd"]',
                "NUL entry": 'destination = "/opt/d"\nhome_files = ["a\\u0000b"]',
            },
            "claude_files": {
                "dot-prefixed entry": 'destination = "/opt/d"\nclaude_files = ["./settings.json"]',
            },
            "regenerable": {
                "doubled-slash entry": 'destination = "/opt/d"\nregenerable = ["a//b"]',
            },
            "memory_snapshot": {
                "memory_snapshot is a string": 'destination = "/opt/d"\nmemory_snapshot = "yes"',
                "memory_snapshot is an integer": 'destination = "/opt/d"\nmemory_snapshot = 1',
            },
            # Each command is rendered inside a fenced block in RESTORE.md, so anything
            # that could end the fence, or hide in it, is refused here first.
            "rearm_commands": {
                "commands not a list": 'destination = "/opt/d"\nrearm_commands = "x"',
                "commands empty": 'destination = "/opt/d"\nrearm_commands = []',
                "non-string command": 'destination = "/opt/d"\nrearm_commands = [1]',
                "empty command": 'destination = "/opt/d"\nrearm_commands = [""]',
                "command with a newline": 'destination = "/opt/d"\nrearm_commands = ["a\\nb"]',
                "command with a fence": 'destination = "/opt/d"\nrearm_commands = ["a```b"]',
                "command with an escape": 'destination = "/opt/d"\nrearm_commands = ["a\\u001bb"]',
                "command with a bidi override": 'destination = "/opt/d"\nrearm_commands = ["a\\u202eb"]',
                "command with a zero-width space": 'destination = "/opt/d"\nrearm_commands = ["a\\u200bb"]',
            },
            # Named in backticks in RESTORE.md's prose, relative to the restored workspace.
            "setup_doc": {
                "setup_doc not a string": 'destination = "/opt/d"\nsetup_doc = 1',
                "empty setup_doc": 'destination = "/opt/d"\nsetup_doc = ""',
                "absolute setup_doc": 'destination = "/opt/d"\nsetup_doc = "/opt/SETUP.md"',
                "climbing setup_doc": 'destination = "/opt/d"\nsetup_doc = "../SETUP.md"',
                "inner climb": 'destination = "/opt/d"\nsetup_doc = "docs/../../SETUP.md"',
                "setup_doc with a newline": 'destination = "/opt/d"\nsetup_doc = "a\\nb"',
                "setup_doc with a fence": 'destination = "/opt/d"\nsetup_doc = "a```b"',
                "setup_doc with a format character": 'destination = "/opt/d"\nsetup_doc = "a\\ufeffb"',
                "setup_doc with a backtick": 'destination = "/opt/d"\nsetup_doc = "a`b"',
            },
        }
        # Anchored after the "<pyproject>: " prefix every shape message starts with, so the
        # key must appear in the message body: a temp-path component spelled like a key
        # (a TMPDIR named "keep") cannot satisfy the match.
        prefix = re.escape(f"{self.root / 'pyproject.toml'}: ")
        for key, bodies in cases.items():
            for label, body in bodies.items():
                with self.subTest(label, key=key):
                    pattern = rf"^{prefix}.*\b{re.escape(key)}\b"
                    with self.assertRaisesRegex(bc.ConfigError, pattern):
                        self._load(f"[tool.workspace_backup]\n{body}\n")


class RootsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(fx.isolated_git_env())
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.repo = fx.make_repo(self.tmp)
        self.worktree = self.tmp / "linked"
        fx.git(
            self.repo, "worktree", "add", "--quiet", "-b", "topic", str(self.worktree)
        )
        self.run_git = backup_proc.git_runner(backup_proc.run_cmd)

    def test_the_primary_checkout_is_resolved_from_a_linked_worktree(self) -> None:
        roots = br.resolve_roots(self.worktree, home=HOME, run_git=self.run_git)
        self.assertEqual(roots.workspace.resolve(), self.repo)
        self.assertEqual(roots.claude_home, HOME / ".claude")
        self.assertEqual(roots.home, HOME)

    def test_linked_worktree_detection(self) -> None:
        self.assertTrue(br.is_linked_worktree(self.worktree, run_git=self.run_git))
        self.assertFalse(br.is_linked_worktree(self.repo, run_git=self.run_git))

    def test_outside_a_repository_both_raise(self) -> None:
        """Holds even on a host whose temp dir sits in a work tree: the fixtures' ceiling."""
        outside = self.tmp / "plain"
        outside.mkdir()
        with self.assertRaises(br.RootsError):
            br.resolve_roots(outside, home=HOME, run_git=self.run_git)
        with self.assertRaises(br.RootsError):
            br.is_linked_worktree(outside, run_git=self.run_git)

    def test_a_common_dir_failure_after_toplevel_succeeds_raises(self) -> None:
        def half_broken(args: Sequence[str], cwd: Path) -> "tuple[int, str, str]":
            if "--show-toplevel" in args:
                return 0, f"{self.repo}\n", ""
            return 128, "", "fatal: fixture failure"

        with self.assertRaises(br.RootsError):
            br.is_linked_worktree(self.repo, run_git=half_broken)


class RepoRootOfTests(unittest.TestCase):
    """The primary checkout is read from git's shared admin dir, through the RunGit seam."""

    def test_the_root_is_the_parent_of_the_absolute_common_dir(self) -> None:
        calls: list[tuple[tuple[str, ...], Path]] = []

        def answering(args: Sequence[str], cwd: Path) -> "tuple[int, str, str]":
            calls.append((tuple(args), cwd))
            return 0, "/opt/fixture-repo/.git\n", ""

        start = Path("/opt/fixture-repo/linked")
        self.assertEqual(br.repo_root_of(answering, start), Path("/opt/fixture-repo"))
        self.assertEqual(
            calls,
            [(("rev-parse", "--path-format=absolute", "--git-common-dir"), start)],
        )

    def test_a_failing_query_raises_naming_the_query_and_its_output(self) -> None:
        cases = {
            "stderr wins": (
                128,
                "ignored",
                "fatal: fixture failure\n",
                "fixture failure",
            ),
            "stdout when stderr is empty": (
                1,
                "fixture stdout\n",
                "",
                "fixture stdout",
            ),
        }
        for label, (code, out, err, expected) in cases.items():
            with self.subTest(label):

                def failing(
                    args: Sequence[str],
                    cwd: Path,
                    answer: "tuple[int, str, str]" = (code, out, err),
                ) -> "tuple[int, str, str]":
                    return answer

                pattern = rf"--git-common-dir failed \(exit {code}\): .*{expected}"
                with self.assertRaisesRegex(br.GitError, pattern):
                    br.repo_root_of(failing, Path("/opt/fixture-repo"))

    def test_the_tool_no_longer_imports_the_worktree_sweep_library(self) -> None:
        """The one function it used from a shared git library is copied in, not imported."""
        self.assertNotIn("worktree_sweep", vars(br))
        source = Path(br.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import worktree_sweep", source)
