#!/usr/bin/env python3
"""RESTORE.md's run blocks never write outside RESTORE_ROOT -- proven by running them.

Every attack is built from real git objects in temp dirs: a temp HOME holding a ``.zshrc`` of
``ORIGINAL``, RESTORE_ROOT beneath it, and bundles made to reach that file. When the renderer
accepts an attack manifest the test runs its bundle blocks, then asserts the home is untouched
and that the renderer refused -- so removing a refusal shows the very write it prevented,
without ever reaching past the temp tree.

A block runs with a variable unset only after the test has checked that every expansion of
that variable in it is guarded, so an unguarded block is never run against the host's root.

Blocks are also pasted, on stdin, into interactive ``sh``, ``bash`` and ``zsh``, because a
person restoring by hand does exactly that and an interactive shell does not stop at a failed
expansion the way ``sh -c`` does. Each runs with a temp HOME, ZDOTDIR and HISTFILE, with ENV
and BASH_ENV removed and its own no-startup-file flags, so no rc file of the host's loads and
nothing it writes lands outside the temp tree.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path

import _fixtures as fx

import backup_restore_doc as brd
import bounded_run
from backup_model import BundleEntry, Manifest

SHELL_TIMEOUT_S = 120.0
_CREATED = "2026-09-14T06:00:00+00:00"
_HEAD = "a" * 40
_RC = ".zshrc"
_ORIGINAL = "ORIGINAL"
_FROM_BUNDLE = "FROM-BUNDLE"
_UNCOMMITTED = "uncommitted work\n"
#: The two variables every run block reads.
_VARIABLES = ("RESTORE_ROOT", "SNAPSHOT")
#: Each shell a block may be pasted into, with the flags that keep its startup files unread.
_INTERACTIVE_SHELLS = (
    ("sh", ("-i",)),
    ("bash", ("--norc", "--noprofile", "-i")),
    ("zsh", ("-f", "-i")),
)
#: Variables whose value an interactive shell runs: the startup files ENV and BASH_ENV name,
#: and the hook bash runs before every prompt.
_STARTUP_VARIABLES = ("ENV", "BASH_ENV", "PROMPT_COMMAND")
#: The prefix bash gives an exported function, which every bash started beneath defines.
_EXPORTED_FUNCTION_PREFIX = "BASH_FUNC_"
#: What each refusal prints, restated. A block passes the words as separate arguments, so
#: none appears verbatim in the text an interactive shell echoes back from a paste.
_EMPTY_REFUSAL = "RESTORE_ROOT must be empty: set"
_ABSOLUTE_REFUSAL = "{} must be absolute: set"
#: A root with the owner's write and search bits but no read bit.
_UNREADABLE_MODE = 0o300
_OWNER_MODE = 0o700


def _manifest(bundles: "tuple[BundleEntry, ...]") -> Manifest:
    return Manifest(
        created=_CREATED,
        complete=True,
        bundles=bundles,
        files=(),
        archive_members=(),
        dirty_worktrees=(),
        failed=(),
        skipped_symlinks=(),
        unclassified_ignored=(),
    )


def _runs_in_an_interactive_shell(name: str) -> bool:
    """Whether an interactive shell would run the value of inherited variable *name*."""
    return name in _STARTUP_VARIABLES or name.startswith(_EXPORTED_FUNCTION_PREFIX)


def _unguarded_expansions(block: str, variable: str) -> "list[str]":
    """Each expansion of *variable* in *block* that an unset or empty value would not stop."""
    return re.findall(rf"\$\{{?{variable}\b(?!:\?)", block)


class ContainmentTests(unittest.TestCase):
    """No run block writes above RESTORE_ROOT, whatever the manifest names."""

    def setUp(self) -> None:
        self.enterContext(fx.isolated_git_env())
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.home = self.tmp / "home"
        self.restore = self.home / "restore"
        self.restore.mkdir(parents=True)
        (self.home / _RC).write_text(_ORIGINAL, encoding="utf-8", newline="\n")
        self.snapshot = self.tmp / "snapshot"
        self.snapshot.mkdir()
        self.env = {
            **os.environ,
            **fx.ISOLATED_GIT_ENV,
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.tmp / "config"),
            "SNAPSHOT": str(self.snapshot),
            "RESTORE_ROOT": str(self.restore),
        }

    def _interactive_env(self, **overrides: str) -> "dict[str, str]":
        """The block environment for an interactive shell that reads no host startup file."""
        zdotdir = self.tmp / "zdotdir"
        zdotdir.mkdir(exist_ok=True)
        env = {
            **self.env,
            "ZDOTDIR": str(zdotdir),
            "HISTFILE": str(self.tmp / "history"),
            **overrides,
        }
        for name in [key for key in env if _runs_in_an_interactive_shell(key)]:
            del env[name]
        return env

    def _bundle(
        self, name: str, files: Mapping[str, str], links: Mapping[str, str]
    ) -> BundleEntry:
        """A real bundle *name* in the snapshot: one commit holding *files* and symlinks."""
        source = fx.make_repo(self.tmp / "sources", name=name.replace("/", "-"))
        for rel, text in files.items():
            (source / rel).write_text(text, encoding="utf-8", newline="\n")
        for rel, target in links.items():
            (source / rel).symlink_to(target)
        fx.git(source, "add", "--all")
        head = fx.add_commit(source, f"what {name} restores")
        bundle = self.snapshot / name
        bundle.parent.mkdir(parents=True, exist_ok=True)
        fx.git(source, "bundle", "create", "--quiet", str(bundle), "--all")
        return BundleEntry(name, head, (), ())

    def _run_bundle_blocks_if_rendered(
        self, bundles: "tuple[BundleEntry, ...]"
    ) -> bool:
        """Run each bundle's block in order when the manifest renders; whether it rendered."""
        try:
            doc = brd.render_restore_doc(_manifest(bundles))
        except brd.RestoreDocError:
            return False
        for block in brd.extract_sh_blocks(doc)[: len(bundles)]:
            bounded_run.run_bounded(
                ["sh", "-e", "-c", block],
                timeout=SHELL_TIMEOUT_S,
                cwd=self.tmp,
                env=self.env,
            )
        return True

    def _assert_home_untouched(self) -> None:
        self.assertEqual((self.home / _RC).read_text(encoding="utf-8"), _ORIGINAL)
        self.assertFalse((self.home / ".git").exists())

    def _require_shell(self, shell: str) -> None:
        if shutil.which(shell) is None:
            self.skipTest(f"{shell} is not installed")

    def _tree(self) -> "list[Path]":
        """Every path under the temp dir but the history file an interactive shell writes."""
        history = self.tmp / "history"
        return sorted(path for path in self.tmp.rglob("*") if path != history)

    def test_the_interactive_environment_inherits_no_prompt_hook_or_function(
        self,
    ) -> None:
        # A host's PROMPT_COMMAND runs before every prompt, and an exported BASH_FUNC_ls%%
        # becomes a function named ls in every bash: either would change what a paste runs.
        self.env["PROMPT_COMMAND"] = "touch prompt-hook-ran"
        self.env["BASH_FUNC_ls%%"] = "() { :; }"
        env = self._interactive_env()
        self.assertNotIn("PROMPT_COMMAND", env)
        self.assertEqual([name for name in env if name.startswith("BASH_FUNC_")], [])

    def test_every_block_refuses_a_relative_root_or_snapshot_and_writes_nothing(
        self,
    ) -> None:
        workspace = self._bundle("workspace.bundle", {}, {})
        memory = self._bundle("memory/-p.bundle", {}, {})
        blocks = brd.extract_sh_blocks(
            brd.render_restore_doc(_manifest((workspace, memory)))
        )
        # Each relative value names somewhere real from the cwd, so a block that ran on would
        # write: an existing empty root, and the snapshot holding real bundles.
        (self.tmp / "relative-root").mkdir()
        relative = {"RESTORE_ROOT": "relative-root", "SNAPSHOT": self.snapshot.name}
        for variable, value in relative.items():
            refusal = _ABSOLUTE_REFUSAL.format(variable)
            self.assertFalse(any(refusal in block for block in blocks))
            # (label, argv, stdin): each block under sh -c, and the whole set pasted.
            runs: "list[tuple[str, list[str], str | None]]" = [
                ("sh -c", ["sh", "-c", block], None) for block in blocks
            ]
            runs += [
                (name, [name, *flags], "".join(blocks))
                for name, flags in _INTERACTIVE_SHELLS
            ]
            for label, argv, stdin_text in runs:
                with self.subTest(variable=variable, run=label):
                    self._require_shell(argv[0])
                    env = self._interactive_env(**{variable: value})
                    before = self._tree()
                    done = bounded_run.run_bounded(
                        argv,
                        timeout=SHELL_TIMEOUT_S,
                        cwd=self.tmp,
                        env=env,
                        stdin_text=stdin_text,
                    )
                    self.assertIn(refusal, done.stderr)
                    self.assertEqual(self._tree(), before)

    def test_the_first_block_sees_past_a_user_ls_and_refuses_an_unreadable_root(
        self,
    ) -> None:
        entry = self._bundle("workspace.bundle", {}, {})
        first = brd.extract_sh_blocks(brd.render_restore_doc(_manifest((entry,))))[0]
        # (label, what is pasted first, a file left in the root, the root's mode, refused)
        cases = (
            ("an ls function hiding a file", "ls() { :; }", True, _OWNER_MODE, True),
            (
                "an ls alias listing nothing",
                "alias ls='ls -l'",
                False,
                _OWNER_MODE,
                False,
            ),
            ("an unreadable root", "", False, _UNREADABLE_MODE, True),
        )
        for shell, flags in _INTERACTIVE_SHELLS:
            for index, (label, prelude, left, mode, refused) in enumerate(cases):
                with self.subTest(shell=shell, case=label):
                    self._require_shell(shell)
                    root = self.tmp / f"root-{shell}-{index}"
                    root.mkdir()
                    if left:
                        (root / "left-behind").write_text(
                            "x", encoding="utf-8", newline="\n"
                        )
                    root.chmod(mode)
                    self.addCleanup(root.chmod, _OWNER_MODE)
                    done = bounded_run.run_bounded(
                        [shell, *flags],
                        timeout=SHELL_TIMEOUT_S,
                        cwd=self.tmp,
                        env=self._interactive_env(RESTORE_ROOT=str(root)),
                        stdin_text=f"{prelude}\n{first}",
                    )
                    root.chmod(_OWNER_MODE)
                    names = sorted(path.name for path in root.iterdir())
                    if refused:
                        self.assertIn(_EMPTY_REFUSAL, done.stderr)
                        self.assertEqual(names, ["left-behind"] if left else [])
                    else:
                        self.assertNotIn(_EMPTY_REFUSAL, done.stderr)
                        self.assertEqual(names, ["workspace"])

    def test_a_restore_directory_above_restore_root_is_refused_not_written(
        self,
    ) -> None:
        # "...bundle" is a safe name whose restore directory is "..".
        escaping = self._bundle("...bundle", {_RC: _FROM_BUNDLE}, {})
        rendered = self._run_bundle_blocks_if_rendered((escaping,))
        self._assert_home_untouched()
        self.assertFalse(rendered)

    def test_a_restore_directory_nested_in_another_never_writes_through_its_link(
        self,
    ) -> None:
        # The workspace checkout holds "evil -> ../..": from RESTORE_ROOT/workspace/evil that
        # is the home directory, so a bundle restoring to workspace/evil would land there.
        workspace = self._bundle("workspace.bundle", {}, {"evil": "../.."})
        nested = self._bundle("workspace/evil.bundle", {_RC: _FROM_BUNDLE}, {})
        rendered = self._run_bundle_blocks_if_rendered((workspace, nested))
        self._assert_home_untouched()
        self.assertFalse(rendered)

    def test_a_run_block_stops_before_any_command_without_restore_root(self) -> None:
        entry = BundleEntry("workspace.bundle", _HEAD, (), ())
        blocks = brd.extract_sh_blocks(brd.render_restore_doc(_manifest((entry,))))
        # Checked before anything runs, and outside any subTest so a failure ends the test:
        # an unguarded block run without RESTORE_ROOT would aim its writes at "/".
        unguarded = [
            found
            for block in blocks
            for variable in _VARIABLES
            for found in _unguarded_expansions(block, variable)
        ]
        self.assertEqual(unguarded, [])
        self.assertTrue(all("${RESTORE_ROOT:?" in block for block in blocks))
        before = sorted(self.tmp.rglob("*"))
        unset = {key: value for key, value in self.env.items() if key != "RESTORE_ROOT"}
        for label, env in (
            ("unset", unset),
            ("empty", {**self.env, "RESTORE_ROOT": ""}),
        ):
            for block in blocks:
                with self.subTest(label, block=block):
                    done = bounded_run.run_bounded(
                        ["sh", "-e", "-c", block],
                        timeout=SHELL_TIMEOUT_S,
                        cwd=self.tmp,
                        env=env,
                    )
                    self.assertNotEqual(done.returncode, 0)
                    self.assertIn("RESTORE_ROOT", done.stderr)
                    self.assertEqual(done.stdout, "")
        self.assertEqual(sorted(self.tmp.rglob("*")), before)

    def test_a_pasted_block_never_touches_the_current_repository(self) -> None:
        # RESTORE_ROOT is empty, as the intro's export leaves it when its cd fails. The block
        # restores the victim's own head, so a checkout that ran would succeed; the snapshot
        # holds no bundle, so a clone that ran would fail before creating anything.
        for shell, flags in _INTERACTIVE_SHELLS:
            with self.subTest(shell=shell):
                self._require_shell(shell)
                victim = fx.make_repo(self.tmp / "victims", name=shell)
                fx.git(
                    victim, "remote", "add", "origin", "https://example.invalid/x.git"
                )
                head = fx.git(victim, "rev-parse", "HEAD").strip()
                readme = victim / "README.md"
                readme.write_text(_UNCOMMITTED, encoding="utf-8", newline="\n")
                entry = BundleEntry("workspace.bundle", head, (), ())
                doc = brd.render_restore_doc(_manifest((entry,)))
                env = self._interactive_env(RESTORE_ROOT="")
                before = self._tree()
                bounded_run.run_bounded(
                    [shell, *flags],
                    timeout=SHELL_TIMEOUT_S,
                    cwd=victim,
                    env=env,
                    stdin_text=brd.extract_sh_blocks(doc)[0],
                )
                self.assertEqual(self._tree(), before)
                self.assertEqual(fx.git(victim, "remote").split(), ["origin"])
                branch = fx.git(victim, "rev-parse", "--abbrev-ref", "HEAD")
                self.assertEqual(branch.strip(), "main")
                self.assertEqual(fx.git(victim, "rev-parse", "HEAD").strip(), head)
                self.assertEqual(readme.read_text(encoding="utf-8"), _UNCOMMITTED)
                self.assertEqual(list(self.snapshot.iterdir()), [])
                self.assertEqual(list(self.restore.iterdir()), [])

    def test_the_first_block_refuses_a_restore_root_that_is_not_empty(self) -> None:
        # A real bundle, so a clone that got past the check would succeed and write.
        entry = self._bundle("workspace.bundle", {}, {})
        first = brd.extract_sh_blocks(brd.render_restore_doc(_manifest((entry,))))[0]
        (self.restore / "left-behind").write_text("x", encoding="utf-8", newline="\n")
        # bash 3.2's sh -i echoes pasted input to stderr, so the assertion below would pass on
        # the echo alone if the block's text held the message it prints.
        self.assertNotIn(_EMPTY_REFUSAL, first)
        # (label, argv, stdin): sh -c takes the block as an argument, a pasted one on stdin.
        runs: "list[tuple[str, list[str], str | None]]" = [
            ("sh -c", ["sh", "-c", first], None)
        ]
        runs += [(name, [name, *flags], first) for name, flags in _INTERACTIVE_SHELLS]
        for label, argv, stdin_text in runs:
            with self.subTest(label):
                self._require_shell(argv[0])
                done = bounded_run.run_bounded(
                    argv,
                    timeout=SHELL_TIMEOUT_S,
                    cwd=self.tmp,
                    env=self._interactive_env(),
                    stdin_text=stdin_text,
                )
                self.assertNotEqual(done.returncode, 0)
                self.assertIn(_EMPTY_REFUSAL, done.stderr)
                names = [path.name for path in self.restore.iterdir()]
                self.assertEqual(names, ["left-behind"])


if __name__ == "__main__":
    unittest.main()
