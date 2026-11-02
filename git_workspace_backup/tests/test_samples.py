#!/usr/bin/env python3
"""The shipped hand-verification samples are what the tool itself writes and reads.

A sample that has drifted from the code misleads the one reader it exists for, and nothing
else notices. So ``expected-manifest.json`` must be byte for byte what the manifest writer
emits for the manifest it parses to (a schema change the sample does not follow fails here),
and the RESTORE.md it renders must show what the samples README tells a reader to look
for. ``sample-config.toml`` must pass the configuration's own validation, against a fake home,
and name exactly the archive members the sample manifest records.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import _fixtures as fx  # noqa: F401 - installs the src/ bootstrap

import git_workspace_backup as wb
from backup_build import PREFIX_CLAUDE, PREFIX_HOME, PREFIX_WORKSPACE
from backup_config import BackupConfig, canonical_path, load_config
from backup_manifest import manifest_to_json, parse_manifest
from backup_model import Manifest
from backup_rearm import REARM_HEADING, rearm_for
from backup_restore_doc import extract_sh_blocks, render_restore_doc
from backup_verify import worktree_refspec

#: The done-bar's hand-verification folder, beside the tool's own script.
SAMPLES = Path(wb.__file__).resolve().parent / "samples"
SAMPLE_MANIFEST = SAMPLES / "expected-manifest.json"
SAMPLE_CONFIG = SAMPLES / "sample-config.toml"
#: Paths the configuration is loaded against: obviously fake, and never read from disk.
FAKE_HOME = Path("/opt/fixture-home")
FAKE_CHECKOUT = Path("/opt/fixture-checkout")
#: The line the first run block prints when RESTORE_ROOT is not an existing empty directory.
EMPTY_ROOT_REFUSAL = "RESTORE_ROOT must be empty"
#: The step placing the archive, and the note after it that the samples README lists.
FILES_HEADING = "## Files"
SECOND_MACHINE_HEADING = "## Setting up a second machine"
#: The re-arm step's fence opens by changing into the restored workspace.
REARM_CD = 'cd "$RESTORE_ROOT/workspace"'
#: The setup document the sample config names, an obvious example.
SAMPLE_SETUP_DOC = "docs/EXAMPLE_SETUP.md"
#: The machine the sample manifest names, an obvious fixture label.
SAMPLE_MACHINE = "fixture-mac"


def _sample_config() -> BackupConfig:
    """The sample table, validated by the tool's own loader against a fake home and checkout."""
    text = SAMPLE_CONFIG.read_text(encoding="utf-8")
    pyproject = FAKE_CHECKOUT / "pyproject.toml"
    return load_config(
        FAKE_CHECKOUT,
        home=FAKE_HOME,
        is_file=lambda path: path == pyproject,
        read_text=lambda path: text,
    )


def _sample_manifest(case: unittest.TestCase) -> Manifest:
    """The sample manifest, parsed by the tool's own parser; fails *case* when it is not ours."""
    manifest = parse_manifest(SAMPLE_MANIFEST.read_bytes())
    if manifest is None:
        case.fail(f"{SAMPLE_MANIFEST.name} is not a manifest this tool would read")
    return manifest


class SampleManifestTests(unittest.TestCase):
    def test_the_sample_manifest_is_exactly_what_the_writer_emits(self) -> None:
        manifest = _sample_manifest(self)
        written = manifest_to_json(manifest).encode("utf-8")
        self.assertEqual(written, SAMPLE_MANIFEST.read_bytes())

    def test_the_sample_manifest_renders_what_the_samples_readme_describes(
        self,
    ) -> None:
        manifest = _sample_manifest(self)
        config = _sample_config()
        commands = config.rearm_commands
        rearm = rearm_for(commands, FAKE_CHECKOUT, setup_doc=config.setup_doc)
        doc = render_restore_doc(manifest, rearm=rearm)
        self.assertIn("complete: yes", doc)
        # The machine beside Created: an obvious fixture label, never a real host name.
        self.assertEqual(manifest.machine, SAMPLE_MACHINE)
        self.assertIn(f"on machine `{SAMPLE_MACHINE}`; complete: yes", doc)
        blocks = extract_sh_blocks(doc)
        # One run block per bundle, then the files block.
        self.assertEqual(len(blocks), len(manifest.bundles) + 1)
        self.assertIn(EMPTY_ROOT_REFUSAL, blocks[0])
        for head in manifest.bundles[0].worktree_heads:
            spec = worktree_refspec(head.name)
            self.assertIsNotNone(spec)
            self.assertIn(str(spec), blocks[0])
        stores = [line for line in blocks[0].splitlines() if " stash store -m " in line]
        recorded = [stash.sha for stash in manifest.bundles[0].stashes]
        self.assertEqual(len(recorded), 2)
        self.assertEqual([line.rsplit(" ", 1)[1] for line in stores], recorded)
        # The second-machine note the README lists, placed after the files step it qualifies.
        self.assertIn(SECOND_MACHINE_HEADING, doc)
        self.assertLess(doc.index(FILES_HEADING), doc.index(SECOND_MACHINE_HEADING))
        self.assertIn("its own machine identity", doc)
        # It names the one Claude file the sample whitelists, as its archive member.
        note = doc[doc.index(SECOND_MACHINE_HEADING) : doc.index(REARM_HEADING)]
        claude = [
            m.name for m in manifest.archive_members if m.name.startswith("claude/")
        ]
        self.assertEqual(claude, ["claude/settings.json"])
        self.assertIn("\nclaude/settings.json\n", note)
        # The re-arm step last, listing the sample config's commands in order.
        rearm_lines = doc[doc.index(REARM_HEADING) :].splitlines()
        opener = rearm_lines.index("```text")
        fence = rearm_lines[opener + 1 : rearm_lines.index("```", opener + 1)]
        self.assertEqual(fence, [REARM_CD, *(commands or ())])
        self.assertEqual(len(fence), 3)
        # Its prose names the sample's setup document as what to rebuild from.
        self.assertEqual(config.setup_doc, SAMPLE_SETUP_DOC)
        self.assertIn(f"`{SAMPLE_SETUP_DOC}`", "\n".join(rearm_lines))


class SampleConfigTests(unittest.TestCase):
    def test_the_sample_config_passes_validation_and_names_the_manifests_members(
        self,
    ) -> None:
        config = _sample_config()
        self.assertFalse(config.memory_snapshot)
        self.assertEqual(
            config.destination, canonical_path(FAKE_HOME / "backup-sample")
        )
        # Every sample entry names one plain file, so each is exactly one archive member.
        whitelisted = {
            f"{prefix}/{entry}"
            for prefix, entries in (
                (PREFIX_WORKSPACE, config.extras),
                (PREFIX_CLAUDE, config.claude_files),
                (PREFIX_HOME, config.home_files),
            )
            for entry in entries
        }
        members = {member.name for member in _sample_manifest(self).archive_members}
        self.assertEqual(members, whitelisted)


if __name__ == "__main__":
    unittest.main()
