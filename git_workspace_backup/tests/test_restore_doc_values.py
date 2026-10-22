#!/usr/bin/env python3
"""Every manifest value RESTORE.md writes passes one character rule; names compare as APFS does.

A value holding an escape, a backspace or a bidi control renders as text a terminal shows
differently from what runs, so the renderer refuses every such value with the re-arm commands'
own rule; a stash message may hold a tab, which git writes into one. And two names a
case-insensitive, normalization-insensitive volume reads as one folder are one name: two
memory bundles spelled apart only that way would restore into a single directory.
"""

from __future__ import annotations

import unittest
from dataclasses import replace

import _fixtures as fx  # noqa: F401 - installs the src/ bootstrap

import backup_restore_doc as brd
from backup_model import BundleEntry, FileEntry, Manifest, StashEntry, WorktreeHead

_HEAD = "a" * 40
_CREATED = "2026-09-14T06:00:00+00:00"
#: Characters that make what a terminal shows differ from what runs.
_HIDDEN = {"escape": "\x1b", "backspace": "\x08", "bidi override": "‮"}
#: The one field a tab may appear in.
_STASH_MESSAGE = "stash message"


def _manifest(
    bundles: "tuple[BundleEntry, ...]" = (), members: "tuple[FileEntry, ...]" = ()
) -> Manifest:
    return Manifest(_CREATED, True, bundles, (), members, (), (), (), ())


def _workspace(
    stashes: "tuple[StashEntry, ...]" = (),
    heads: "tuple[WorktreeHead, ...]" = (),
) -> BundleEntry:
    return BundleEntry("workspace.bundle", _HEAD, ("refs/heads/main",), heads, stashes)


def _member(name: str) -> FileEntry:
    return FileEntry(name, "b" * 64, 1)


def _carrying(char: str) -> "dict[str, Manifest]":
    """One manifest per rendered field, with *char* in that field alone."""
    return {
        "created": replace(_manifest(), created=f"2026-09-14{char}"),
        _STASH_MESSAGE: _manifest(
            (_workspace(stashes=(StashEntry(_HEAD, f"On main: a{char}b"),)),)
        ),
        "worktree name": _manifest(
            (_workspace(heads=(WorktreeHead(f"wt{char}", _HEAD, True),)),)
        ),
        "bundle name": _manifest(
            (BundleEntry(f"memory/s{char}.bundle", _HEAD, (), ()),)
        ),
        "archived project slug": _manifest(
            members=(_member(f"claude/projects/s{char}/x.md"),)
        ),
        "claude file": _manifest(members=(_member(f"claude/a{char}.json"),)),
    }


class RenderedValueTests(unittest.TestCase):
    def test_a_hidden_character_in_any_rendered_value_is_refused(self) -> None:
        for label, char in _HIDDEN.items():
            for field, manifest in _carrying(char).items():
                with self.subTest(char=label, field=field):
                    with self.assertRaises(brd.RestoreDocError):
                        brd.render_restore_doc(manifest)

    def test_a_tab_is_refused_everywhere_but_a_stash_message(self) -> None:
        for field, manifest in _carrying("\t").items():
            with self.subTest(field=field):
                if field == _STASH_MESSAGE:
                    self.assertIn("a\tb", brd.render_restore_doc(manifest))
                else:
                    with self.assertRaises(brd.RestoreDocError):
                        brd.render_restore_doc(manifest)

    def test_each_value_renders_clean(self) -> None:
        # The control: every field above, holding no such character, renders.
        for field, manifest in _carrying("").items():
            with self.subTest(field=field):
                self.assertIn(
                    "# Restoring this snapshot", brd.render_restore_doc(manifest)
                )


class ApfsNameTests(unittest.TestCase):
    def test_two_names_one_folder_on_apfs_are_a_duplicate(self) -> None:
        pairs = {
            "case": ("memory/Fixture.bundle", "memory/fixture.bundle"),
            "normalization": ("memory/café.bundle", "memory/café.bundle"),
        }
        for label, names in pairs.items():
            with self.subTest(label):
                bundles = tuple(BundleEntry(name, _HEAD, (), ()) for name in names)
                with self.assertRaisesRegex(brd.RestoreDocError, "duplicate"):
                    brd.render_restore_doc(_manifest(bundles))

    def test_targets_that_differ_only_in_case_or_form_overlap(self) -> None:
        cases = (
            ("Workspace", "workspace/evil", True),
            ("claude/projects/Café/memory", "claude/projects/café", True),
            ("work", "Workspace", False),
        )
        for first, second, expected in cases:
            with self.subTest(first=first, second=second):
                self.assertIs(brd.restore_targets_overlap(first, second), expected)


if __name__ == "__main__":
    unittest.main()
