#!/usr/bin/env python3
"""``--mode verify`` through the CLI, on snapshots a real run wrote into a temp destination.

The run is the fixture tree's own, with launchctl and osascript routed to fakes; home, state
and destination are the case's temp dirs. So these prove verify accepts exactly what a run
writes, ledger included, and that RESTORE.md points a restorer at it.
"""

from __future__ import annotations

import os
import unittest
from dataclasses import replace
from pathlib import Path

import _cli_case as cc
from bounded_run import run_bounded

import backup_restore_doc as brd
from backup_model import Manifest
from backup_rearm import ENTRY_SCRIPT_NAME

#: The placeholder RESTORE.md puts where a restorer types the interpreter and the tool's path.
_TOOL_PLACEHOLDER = f"python <path to {ENTRY_SCRIPT_NAME}>"
#: A shell function standing in for the tool: it prints each argument it was given, NUL-ended.
_ARGV_ECHO = 'argv_echo() { printf "%s\\0" "$@"; }; '
#: How long the shell that expands the printed command may take.
_SHELL_TIMEOUT_S = 30.0


def _printed_verify_argv(doc: str, snapshot: Path) -> "tuple[int, list[str]]":
    """The shell's exit, and the arguments the verify line in *doc* hands the tool.

    The line is expanded by sh, with ``SNAPSHOT`` set to *snapshot*.

    The placeholder is replaced by a function that prints its arguments, and nothing else is
    touched, so the quoting and the command substitution are the document's own.
    """
    (line,) = [text for text in doc.splitlines() if "--mode verify" in text]
    command = line.replace(_TOOL_PLACEHOLDER, "argv_echo", 1)
    # run_bounded kills the shell's whole tree on expiry, so the dirname it starts cannot
    # hold the captured pipe open past the bound.
    done = run_bounded(
        ["/bin/sh", "-c", _ARGV_ECHO + command],
        timeout=_SHELL_TIMEOUT_S,
        env={"PATH": os.environ.get("PATH", os.defpath), "SNAPSHOT": str(snapshot)},
    )
    return done.returncode, done.stdout.split("\0")[:-1]


class VerifyCliTests(cc.CliCase):
    def snapshot(self, destination: "Path | None" = None) -> Path:
        """The folder the fixture run at START writes into *destination*."""
        chosen = self.destination if destination is None else destination
        return chosen / cc.FIRST_NAME

    def test_a_snapshot_a_run_wrote_is_verified(self) -> None:
        self.assertEqual(self.main("--mode", "run"), 0)
        code = self.main("--mode", "verify", "--snapshot", str(self.snapshot()))
        self.assertEqual(code, 0, self.err.getvalue())
        self.assertTrue(self.out.getvalue().startswith(f"verified: {cc.FIRST_NAME} ("))
        self.assertEqual(self.err.getvalue(), "")

    def test_a_home_relative_snapshot_is_expanded_against_the_hosts_home(self) -> None:
        destination = self.tree.home / "dest"
        self.write_config(destination)
        self.assertEqual(self.main("--mode", "run"), 0)
        snapshot = f"~/dest/{cc.FIRST_NAME}"
        self.assertEqual(self.main("--mode", "verify", "--snapshot", snapshot), 0)

    def test_a_destination_given_on_the_command_line_is_the_one_checked(self) -> None:
        other = self.tmp / "other-dest"
        self.assertEqual(self.main("--mode", "run", "--destination", str(other)), 0)
        argv = ["--mode", "verify", "--snapshot", str(self.snapshot(other))]
        # Against the configured destination the folder is not directly in it.
        self.assertEqual(self.main(*argv), 2)
        self.assertIn("not directly in the destination", self.err.getvalue())
        self.assertEqual(self.main(*argv, "--destination", str(other)), 0)

    def test_a_tampered_restore_document_fails(self) -> None:
        self.assertEqual(self.main("--mode", "run"), 0)
        restore = self.snapshot() / brd.RESTORE_DOC_NAME
        restore.write_bytes(b"# a planted procedure\n")
        self.assertEqual(
            self.main("--mode", "verify", "--snapshot", str(self.snapshot())), 1
        )
        self.assertIn(brd.RESTORE_DOC_NAME, self.err.getvalue())

    def test_another_home_has_no_ledger_so_the_manifest_is_not_authenticated(
        self,
    ) -> None:
        self.assertEqual(self.main("--mode", "run"), 0)
        second_home = self.tmp / "second-home"
        second_home.mkdir()
        second = replace(self.host(), home=second_home)
        argv = ["--mode", "verify", "--snapshot", str(self.snapshot())]
        self.assertEqual(self.main(*argv, host=second), 1)
        self.assertIn("NOT authenticated", self.err.getvalue())
        self.assertNotIn("verified", self.out.getvalue() + self.err.getvalue())

    def test_the_command_restore_md_prints_works_from_a_checkout_without_config(
        self,
    ) -> None:
        # The tool as a clone of its own: the checkout it sits in has no backup table.
        self.assertEqual(self.main("--mode", "run"), 0)
        (self.tree.workspace / "pyproject.toml").write_text(
            '[project]\nname = "fixture"\n', encoding="utf-8", newline="\n"
        )
        doc = (self.snapshot() / brd.RESTORE_DOC_NAME).read_text(encoding="utf-8")
        code, argv = _printed_verify_argv(doc, self.snapshot())
        self.assertEqual(code, 0)
        self.assertEqual(self.main(*argv), 0, self.err.getvalue())
        self.assertTrue(self.out.getvalue().startswith(f"verified: {cc.FIRST_NAME} ("))

    def test_verify_without_a_snapshot_is_refused_before_git_runs(self) -> None:
        self.assertEqual(self.main("--mode", "verify"), 2)
        self.assert_one_error_line("--snapshot")
        self.assertEqual(self.runner.calls, [])

    def test_a_relative_snapshot_is_refused(self) -> None:
        self.assertEqual(self.main("--mode", "verify", "--snapshot", cc.FIRST_NAME), 2)
        self.assert_one_error_line("--snapshot")


class RestoreDocPointerTests(unittest.TestCase):
    def test_restore_md_points_at_verify_and_does_not_vouch_for_itself(self) -> None:
        manifest = Manifest(
            "2026-09-14T06:00:00+00:00", True, (), (), (), (), (), (), ()
        )
        doc = brd.render_restore_doc(manifest)
        self.assertIn(
            f'{_TOOL_PLACEHOLDER} --mode verify --snapshot "$SNAPSHOT" '
            '--destination "$(dirname -- "$SNAPSHOT")"',
            doc,
        )
        self.assertIn("cannot vouch for itself", doc)
        # A text block, never one that runs: verify needs the tool, which a restore may lack.
        self.assertNotIn("--mode verify", "".join(brd.extract_sh_blocks(doc)))


if __name__ == "__main__":
    unittest.main()
