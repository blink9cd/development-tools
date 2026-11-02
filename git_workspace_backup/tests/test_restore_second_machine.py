#!/usr/bin/env python3
"""RESTORE.md tells a second machine not to take this one's per-machine Claude files.

A snapshot restores the machine it was taken on. The Claude files its archive holds, named
from the manifest's archive members, can carry that machine's own facts (scheduled agents,
local paths), so a machine set up beside it, rather than replacing it, chooses file by file
what to place, and takes a machine identity of its own. The note runs nothing, and sits after
the Files step whose ``claude/`` copy it qualifies, before the re-arm step. The opening names
the machine that took the snapshot beside "Created".

The re-arm step itself is pinned here too: by default it names only this tool's install line,
and the configuration's commands replace that line, each refused when it could break the fence.
"""

from __future__ import annotations

import shlex
import unittest
from dataclasses import replace
from pathlib import Path

import _fixtures as fx  # noqa: F401 - installs the src/ bootstrap

import backup_rearm as brr
import backup_restore_doc as brd
from backup_model import BundleEntry, FileEntry, Manifest

_CREATED = "2026-09-14T06:00:00+00:00"
_HEAD = "a" * 40
_HEADING = "## Setting up a second machine"
#: The heading of the step that places the archive's ``claude/`` prefix.
_FILES_HEADING = "## Files"
#: The heading of the step that re-arms hooks and reinstalls the agents.
_REARM_HEADING = "## What a bundle does not carry"


def _manifest(bundles: "tuple[BundleEntry, ...]") -> Manifest:
    return Manifest(_CREATED, True, bundles, (), (), (), (), (), ())


def _members(*names: str) -> "tuple[FileEntry, ...]":
    return tuple(FileEntry(name, "b" * 64, 1) for name in names)


#: Archive members under each prefix: two Claude files, one beneath a folder, one beneath a
#: project folder the next section names, and one each under the other two prefixes.
_MEMBERS = _members(
    "claude/settings.json",
    "claude/skills/a.md",
    "claude/skills/b.md",
    "claude/projects/fixture-slug/notes.md",
    "home/.zshrc",
    "workspace/.env",
)


def _text_fence(section: str) -> "list[str]":
    """The lines inside *section*'s first ``text`` fence."""
    lines = section.splitlines()
    opener = lines.index("```text")
    return lines[opener + 1 : lines.index("```", opener + 1)]


def _section(doc: str) -> str:
    """The second-machine section's text, from its heading to the next heading."""
    start = doc.index(_HEADING)
    end = doc.find("\n## ", start + len(_HEADING))
    return doc[start:] if end == -1 else doc[start:end]


class SecondMachineNoteTests(unittest.TestCase):
    def _docs(self) -> "dict[str, str]":
        workspace = BundleEntry("workspace.bundle", _HEAD, ("refs/heads/main",), ())
        return {
            "with a bundle": brd.render_restore_doc(_manifest((workspace,))),
            "with no bundle": brd.render_restore_doc(_manifest(())),
            "with Claude files": brd.render_restore_doc(
                replace(_manifest(()), archive_members=_MEMBERS)
            ),
        }

    def test_every_document_carries_the_note_once(self) -> None:
        for label, doc in self._docs().items():
            with self.subTest(label):
                self.assertEqual(doc.count(_HEADING), 1)

    def test_the_note_follows_the_files_step_and_precedes_the_rearm_step(self) -> None:
        for label, doc in self._docs().items():
            with self.subTest(label):
                note = doc.index(_HEADING)
                self.assertLess(doc.index(_FILES_HEADING), note)
                self.assertLess(note, doc.index(_REARM_HEADING))

    def test_the_note_names_the_claude_files_the_archive_holds(self) -> None:
        section = _section(self._docs()["with Claude files"])
        # Each top-level name once, a folder with its slash; the project folders are the next
        # section's, so they are not named here.
        self.assertEqual(
            _text_fence(section), ["claude/settings.json", "claude/skills/"]
        )
        flat = " ".join(section.split())
        self.assertIn("the machine it was taken on", flat)
        for absent in ("ENVIRONMENT.md", "owner", "home/.zshrc", "fixture-slug"):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, flat)

    def test_with_no_claude_file_the_note_says_the_archive_holds_none(self) -> None:
        for label in ("with a bundle", "with no bundle"):
            with self.subTest(label):
                section = _section(self._docs()[label])
                flat = " ".join(section.split())
                self.assertIn("holds no file under `claude/`", flat)
                self.assertNotIn("```", section)
                self.assertNotIn("owner", flat)

    def test_the_note_says_the_claude_project_folders_are_that_machines_own(
        self,
    ) -> None:
        # Memory holds machine facts, so the slug folders are skipped on a second machine too.
        section = " ".join(_section(self._docs()["with a bundle"]).split())
        for phrase in ("memory corpus or project folder", "memory holds machine facts"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, section)

    def test_the_note_adds_no_run_block(self) -> None:
        for label, doc in self._docs().items():
            with self.subTest(label):
                self.assertNotIn("```sh", _section(doc))


def _rearm(doc: str) -> str:
    """The re-arm section's text, from its heading to the end of the document."""
    return doc[doc.index(_REARM_HEADING) :]


def _rearm_fence(doc: str) -> "list[str]":
    """The lines inside the re-arm section's ``text`` fence."""
    lines = _rearm(doc).splitlines()
    opener = lines.index("```text")
    return lines[opener + 1 : lines.index("```", opener + 1)]


class MachineIdentityTests(unittest.TestCase):
    def test_the_opening_names_the_machine_beside_created(self) -> None:
        doc = brd.render_restore_doc(replace(_manifest(()), machine="fixture-mac"))
        self.assertIn(
            f"Created {_CREATED} on machine `fixture-mac`; complete: yes.", doc
        )

    def test_a_snapshot_from_before_identity_names_no_machine(self) -> None:
        doc = brd.render_restore_doc(_manifest(()))
        self.assertIn(f"Created {_CREATED}; complete: yes.", doc)
        self.assertNotIn("on machine", doc)

    def test_a_machine_that_is_not_a_label_is_refused(self) -> None:
        for machine in ("fixture`mac", "fixture\n```sh", "Fixture", ""):
            with self.subTest(machine=machine):
                with self.assertRaisesRegex(brd.RestoreDocError, "machine"):
                    brd.render_restore_doc(replace(_manifest(()), machine=machine))

    def test_the_second_machine_note_says_it_takes_its_own_identity(self) -> None:
        section = " ".join(_section(brd.render_restore_doc(_manifest(()))).split())
        for phrase in (
            "its own machine identity",
            "`~/.workspace_backup/machine`",
            "no snapshot carries",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, section)

    def test_the_note_says_a_replacement_keeps_the_old_label(self) -> None:
        # A replacement for the same machine keeps the old label, so its old snapshots stay
        # its own; only a genuinely new machine lets the first run derive one.
        section = " ".join(_section(brd.render_restore_doc(_manifest(()))).split())
        for phrase in (
            "A machine replacing this one",
            "before its first backup run",
            "its old snapshots stay its own",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, section)

    def test_the_rearm_advice_still_names_this_backups_agent(self) -> None:
        doc = brd.render_restore_doc(_manifest(()))
        rearm = " ".join(doc[doc.index(_REARM_HEADING) :].split())
        self.assertIn("re-install this backup's scheduled agent", rearm)


class ReArmInstallLineTests(unittest.TestCase):
    """By default the re-arm step names only this tool's own install line.

    The entry script is found beside this suite's folder, named after the tool's folder as
    the layout requires, so nothing here needs the workspace it came from.
    """

    def setUp(self) -> None:
        self.tool = Path(__file__).resolve().parent.parent
        self.script = self.tool / f"{self.tool.name}.py"
        self.assertTrue(self.script.is_file(), self.script)

    def test_the_entry_script_is_the_one_the_default_names(self) -> None:
        self.assertEqual(brr.ENTRY_SCRIPT, self.script)

    def test_inside_the_workspace_the_line_names_the_script_relative_to_it(
        self,
    ) -> None:
        rearm = brr.default_rearm(self.tool.parent)
        doc = brd.render_restore_doc(_manifest(()), rearm=rearm)
        line = f"python {self.tool.name}/{self.script.name} --mode install"
        self.assertEqual(_rearm_fence(doc), ['cd "$RESTORE_ROOT/workspace"', line])

    def test_outside_the_workspace_the_line_names_the_script_and_repo_absolutely(
        self,
    ) -> None:
        """A script outside the workspace backs up its own clone unless --repo names it."""
        rearm = brr.default_rearm(Path("/opt/fixture-elsewhere"))
        script = shlex.quote(str(self.script))
        line = f"python {script} --mode install --repo /opt/fixture-elsewhere"
        self.assertEqual(rearm.commands, (line,))

    def test_the_repo_is_quoted_as_the_script_is(self) -> None:
        rearm = brr.default_rearm(Path("/opt/fixture elsewhere"))
        [line] = rearm.commands
        self.assertTrue(line.endswith(" --repo '/opt/fixture elsewhere'"), line)

    def test_outside_the_workspace_the_prose_says_the_paths_are_the_snapshots(
        self,
    ) -> None:
        for setup_doc in (None, "docs/FIXTURE_SETUP.md"):
            with self.subTest(setup_doc=setup_doc):
                rearm = brr.rearm_for(None, Path("/opt/fixture-elsewhere"), setup_doc)
                doc = brd.render_restore_doc(_manifest(()), rearm=rearm)
                prose = " ".join(_rearm(doc).split())
                self.assertIn("when the snapshot was taken", prose)
                self.assertIn("agent. These paths", prose)

    def test_inside_the_workspace_the_document_is_unchanged(self) -> None:
        """The in-checkout case: no --repo, no note, and the document as it always was."""
        line = f"python {self.tool.name}/{self.script.name} --mode install"
        rearm = brr.default_rearm(self.tool.parent)
        self.assertEqual(rearm, brr.ReArm((line,), configured=False))
        section = "\n".join(
            [
                _REARM_HEADING,
                "",
                "A bundle holds no `.git/config`, no hooks and no virtualenvs. Re-arm"
                " the git hooks",
                "first: a hook that is missing fails silently. Then set up again"
                " whatever",
                "else the workspace built from those, and re-install this backup's"
                " scheduled agent:",
                "",
                "```text",
                'cd "$RESTORE_ROOT/workspace"',
                line,
                "```",
                "",
            ]
        )
        doc = brd.render_restore_doc(_manifest(()), rearm=rearm)
        self.assertTrue(doc.endswith(section), doc[-600:])
        self.assertNotIn("when the snapshot was taken", doc)

    def test_with_no_workspace_known_the_line_holds_a_placeholder(self) -> None:
        doc = brd.render_restore_doc(_manifest(()))
        line = f"python <path to {self.script.name}> --mode install"
        self.assertEqual(_rearm_fence(doc)[1:], [line])

    def test_the_default_step_names_no_tool_or_doc_of_another_workspace(self) -> None:
        for workspace in (None, self.tool.parent):
            with self.subTest(workspace=workspace):
                rearm = brr.default_rearm(workspace)
                section = _rearm(brd.render_restore_doc(_manifest(()), rearm=rearm))
                absent = ("utilities/", "INSTALLATION_STEPS", "pii_precommit")
                for name in (*absent, "core.hooksPath"):
                    self.assertNotIn(name, section)


class ReArmConfiguredTests(unittest.TestCase):
    """Configured re-arm commands replace the default, in order, inside the text fence."""

    _COMMANDS = (
        "python tools/fixture_hooks.py --install",
        'python tools/fixture_wire.py --repo-root "$PWD"',
    )

    def test_configured_commands_are_rendered_in_order_after_the_cd(self) -> None:
        rearm = brr.rearm_for(self._COMMANDS, Path("/opt/fixture-checkout"))
        self.assertEqual(rearm, brr.ReArm(self._COMMANDS, configured=True))
        doc = brd.render_restore_doc(_manifest(()), rearm=rearm)
        self.assertEqual(
            _rearm_fence(doc), ['cd "$RESTORE_ROOT/workspace"', *self._COMMANDS]
        )
        self.assertIn("configuration", " ".join(_rearm(doc).split()))

    def test_absent_commands_fall_back_to_the_default(self) -> None:
        workspace = Path("/opt/fixture-checkout")
        self.assertEqual(brr.rearm_for(None, workspace), brr.default_rearm(workspace))
        self.assertFalse(brr.default_rearm(workspace).configured)

    def test_a_command_that_could_break_the_fence_is_refused(self) -> None:
        # The last three are format characters: a bidi override, a zero-width space and a
        # BOM, which render as nothing or reorder what the restorer reads.
        refused = ("", "a\nb", "a\rb", "a```b", "a\x00b", "a\x1bb", "a\u2028b")
        for bad in (*refused, "a\u202eb", "a\u200bb", "a\ufeffb"):
            with self.subTest(command=bad):
                self.assertIsNotNone(brr.rearm_command_problem(bad))
                rearm = brr.ReArm(("python ok.py", bad), configured=True)
                with self.assertRaisesRegex(brd.RestoreDocError, "re-arm command"):
                    brd.render_restore_doc(_manifest(()), rearm=rearm)
        self.assertIsNone(brr.rearm_command_problem('python a.py --x "$PWD"'))


class ReArmSetupDocTests(unittest.TestCase):
    """A configured setup document is named in the step's prose; else the generic wording."""

    _DOC = "docs/FIXTURE_SETUP.md"
    _COMMANDS = ("python tools/fixture_hooks.py --install",)
    _WORKSPACE = Path("/opt/fixture-checkout")

    def _prose(self, rearm: brr.ReArm) -> str:
        """The step's prose, joined onto one line: everything before its fence."""
        section = _rearm(brd.render_restore_doc(_manifest(()), rearm=rearm))
        return " ".join(section[: section.index("```text")].split())

    def test_rearm_for_carries_the_setup_doc_with_or_without_commands(self) -> None:
        configured = brr.rearm_for(self._COMMANDS, self._WORKSPACE, setup_doc=self._DOC)
        self.assertEqual(
            configured, brr.ReArm(self._COMMANDS, configured=True, setup_doc=self._DOC)
        )
        default = brr.rearm_for(None, self._WORKSPACE, setup_doc=self._DOC)
        self.assertEqual(default.commands, brr.default_rearm(self._WORKSPACE).commands)
        self.assertEqual(default.setup_doc, self._DOC)
        self.assertIsNone(brr.rearm_for(self._COMMANDS, self._WORKSPACE).setup_doc)

    def test_a_named_setup_doc_is_what_the_restorer_rebuilds_from(self) -> None:
        for commands in (self._COMMANDS, None):
            with self.subTest(configured=commands is not None):
                rearm = brr.rearm_for(commands, self._WORKSPACE, setup_doc=self._DOC)
                prose = self._prose(rearm)
                self.assertIn(
                    f"rebuild the virtualenvs and prerequisites as `{self._DOC}` describes",
                    prose,
                )
                self.assertNotIn("setup notes", prose)

    def test_without_a_setup_doc_the_wording_stays_generic(self) -> None:
        configured = self._prose(brr.rearm_for(self._COMMANDS, self._WORKSPACE))
        self.assertIn("as the workspace's own setup notes describe", configured)
        default = self._prose(brr.rearm_for(None, self._WORKSPACE))
        self.assertIn("set up again whatever else the workspace built", default)
        for prose in (configured, default):
            self.assertNotIn(self._DOC, prose)

    def test_the_prose_instructs_the_restorer_rather_than_hoping(self) -> None:
        for setup_doc in (None, self._DOC):
            with self.subTest(setup_doc=setup_doc):
                rearm = brr.rearm_for(
                    self._COMMANDS, self._WORKSPACE, setup_doc=setup_doc
                )
                prose = self._prose(rearm)
                self.assertNotIn("should", prose)
                # The commands' owner is named outright: with a setup document in the
                # sentence before, an "its" would point at the document.
                self.assertIn("commands the workspace's configuration names", prose)
                self.assertIn(
                    "re-install any scheduled agent they leave out, including this "
                    "backup's:",
                    prose,
                )

    def test_a_setup_doc_that_is_absolute_climbs_or_breaks_the_prose_is_refused(
        self,
    ) -> None:
        for bad in ("", "/opt/SETUP.md", "../SETUP.md", "docs/../../S.md", "a\nb"):
            with self.subTest(setup_doc=bad):
                self.assertIsNotNone(brr.setup_doc_problem(bad))
                rearm = brr.ReArm(self._COMMANDS, configured=True, setup_doc=bad)
                with self.assertRaisesRegex(brd.RestoreDocError, "setup document"):
                    brd.render_restore_doc(_manifest(()), rearm=rearm)
        for bad in ("a```b", "a`b", "a\u202eb", "..\\SETUP.md", "C:/SETUP.md"):
            with self.subTest(setup_doc=bad):
                self.assertIsNotNone(brr.setup_doc_problem(bad))
        for good in (self._DOC, "SETUP.md", "docs/a..b.md"):
            with self.subTest(setup_doc=good):
                self.assertIsNone(brr.setup_doc_problem(good))


if __name__ == "__main__":
    unittest.main()
