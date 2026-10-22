#!/usr/bin/env python3
"""A run names and records its snapshot by this machine's identity, which never travels.

The identity's own rules -- the label a host name gives, the kept file -- are pinned in
test_machine.py. These are what a run does with it, end to end against real git and a temp
destination, with the host name injected as an obvious fixture.
"""

from __future__ import annotations

import unittest
import zipfile
from collections.abc import Sequence
from dataclasses import replace
from datetime import timedelta
from unittest import mock

import _fixtures as fx
from _run_case import FIRST_NAME, MACHINE, START, RunCase

import backup_machine as bmach
import backup_run as brun
import backup_status as bs
from backup_build import EXTRAS_ZIP, PREFIX_HOME
from backup_ledger import load_ledger, save_ledger
from backup_manifest import MANIFEST_NAME, parse_manifest
from backup_model import OUTCOME_FAILED, OUTCOME_SKIPPED, Ledger
from backup_restore_doc import RESTORE_DOC_NAME

#: What the home-relative identity file is called in the archive, were it ever archived.
_IDENTITY_MEMBER = f"{PREFIX_HOME}/.workspace_backup/{bmach.IDENTITY_NAME}"


def _all_uploaded(argv: Sequence[str]) -> "tuple[int, str, str]":
    """Every path osascript is asked about is in iCloud and uploaded."""
    return 0, fx.upload_states_json(*[(True, True)] * len(fx.osascript_paths(argv))), ""


class RunMachineTests(RunCase):
    def test_the_first_run_keeps_the_identity_and_names_and_records_it(self) -> None:
        self.assertEqual(self.run_backup(self.request()), brun.EXIT_OK)
        self.assertTrue(FIRST_NAME.endswith(f"-{MACHINE}"))
        self.assertEqual(
            bmach.identity_path(self.base).read_text(encoding="utf-8"), f"{MACHINE}\n"
        )
        folder = self.destination / FIRST_NAME
        manifest = parse_manifest((folder / MANIFEST_NAME).read_bytes())
        assert manifest is not None
        self.assertEqual(manifest.machine, MACHINE)
        doc = (folder / RESTORE_DOC_NAME).read_text("utf-8")
        self.assertIn(f"on machine `{MACHINE}`", doc)
        self.assertEqual(
            [entry.name for entry in load_ledger(self.state).entries], [FIRST_NAME]
        )

    def test_a_later_run_keeps_its_identity_when_the_host_is_renamed(self) -> None:
        self.run_backup(self.request())
        self.hostname = "Fixture-Mac-2.local"
        self.clock = START.replace(day=15)
        self.assertEqual(self.run_backup(self.request()), brun.EXIT_OK)
        self.assertEqual(self.last_run().snapshot, f"2026-09-15T060000-{MACHINE}")

    def test_a_hand_edited_identity_names_the_snapshot(self) -> None:
        identity = bmach.identity_path(self.base)
        identity.parent.mkdir(parents=True)
        identity.write_text("fixture-studio\n", encoding="utf-8", newline="\n")
        self.assertEqual(self.run_backup(self.request()), brun.EXIT_OK)
        self.assertEqual(self.last_run().snapshot, "2026-09-14T060000-fixture-studio")

    def test_an_invalid_identity_stops_the_run_before_anything_is_built(self) -> None:
        identity = bmach.identity_path(self.base)
        identity.parent.mkdir(parents=True)
        identity.write_text("Fixture Mac\n", encoding="utf-8", newline="\n")
        self.assertEqual(self.run_backup(self.request()), brun.EXIT_UNRUNNABLE)
        record = self.last_run()
        self.assertEqual(record.outcome, OUTCOME_FAILED)
        self.assertIn(str(identity), record.detail)
        self.assertEqual(identity.read_text(encoding="utf-8"), "Fixture Mac\n")
        self.assertEqual(self.memory_snapshot_calls(), [])
        self.assertFalse(self.destination.exists())

    def test_the_identity_file_never_travels_even_when_an_entry_reaches_it(
        self,
    ) -> None:
        # The tool's whole state folder whitelisted: the identity file is the one file in
        # it the archive must not hold, or a machine set up from this snapshot inherits it.
        config = replace(
            self.tree.request.config, home_files=(".zshrc", ".workspace_backup")
        )
        self.run_backup(replace(self.request(), config=config))
        with zipfile.ZipFile(self.destination / FIRST_NAME / EXTRAS_ZIP) as archive:
            names = archive.namelist()
        self.assertTrue(bmach.identity_path(self.base).is_file())
        self.assertNotIn(_IDENTITY_MEMBER, names)
        self.assertIn(f"{PREFIX_HOME}/.workspace_backup/run.lock", names)

    def test_an_entry_naming_only_the_identity_file_matches_no_files(self) -> None:
        config = replace(
            self.tree.request.config,
            home_files=(".zshrc", f".workspace_backup/{bmach.IDENTITY_NAME}"),
        )
        bmach.ensure_identity(self.base, lambda: self.hostname)
        self.assertEqual(
            self.run_backup(replace(self.request(), config=config)),
            brun.EXIT_ATTENTION,
        )
        manifest = parse_manifest(
            (self.destination / FIRST_NAME / MANIFEST_NAME).read_bytes()
        )
        assert manifest is not None
        self.assertNotIn(_IDENTITY_MEMBER, [m.name for m in manifest.archive_members])
        reasons = {item.item: item.reason for item in manifest.failed}
        entry = f"{PREFIX_HOME}:.workspace_backup/{bmach.IDENTITY_NAME}"
        self.assertEqual(
            reasons.get(entry),
            "matched only the machine identity file, which never travels",
        )

    def test_a_dry_run_names_the_identity_and_writes_nothing(self) -> None:
        request = replace(self.request(), dry_run=True)
        self.assertEqual(self.run_backup(request), brun.EXIT_OK)
        self.assertIn(
            f"would write: {self.destination / FIRST_NAME}", self.out.getvalue()
        )
        self.assertFalse(self.base.exists())

    def test_a_dry_run_refuses_an_invalid_identity(self) -> None:
        identity = bmach.identity_path(self.base)
        identity.parent.mkdir(parents=True)
        identity.write_text("..\n", encoding="utf-8", newline="\n")
        request = replace(self.request(), dry_run=True)
        self.assertEqual(self.run_backup(request), brun.EXIT_UNRUNNABLE)
        self.assertIn(str(identity), self.err.getvalue())
        self.assertEqual(self.out.getvalue(), "")

    def test_an_unconfirmed_promote_leaves_an_own_folder_status_asks_to_check(
        self,
    ) -> None:
        # One machine alone leaves this: the promote could not confirm, so its snapshot was
        # never ledgered. Status must name that cause first, not only a shared label.
        with mock.patch.object(brun, "read_bytes_bounded", return_value=None):
            self.assertEqual(self.run_backup(self.request()), brun.EXIT_ATTENTION)
        self.clock = START + timedelta(days=1)
        self.assertEqual(self.run_backup(self.request()), brun.EXIT_OK)
        facts = bs.gather_facts(
            destination=self.destination,
            state=self.state,
            keep=self.tree.request.config.keep,
            agent=bs.AGENT_LOADED,
            now=self.clock + timedelta(hours=1),
            home=self.tmp / "home",
            run=fx.FakeRunner({"osascript": _all_uploaded}),
            machine=MACHINE,
        )
        verdict = bs.judge(facts)
        self.assertEqual(verdict.code, bs.EXIT_ATTENTION)
        own = [line for line in verdict.lines if "named for this machine" in line]
        self.assertEqual(
            own,
            [
                f"snapshot folders named for this machine ({MACHINE}) are not in its ledger: "
                f"{FIRST_NAME}. Each is most likely this machine's own snapshot that a run "
                "could not ledger (a failed promote confirm or ledger save, or a restore that "
                "started with an empty ledger): check it, and keep or delete it by hand. Only "
                "if a second machine uses the same label, edit ~/.workspace_backup/machine on "
                "one of them"
            ],
        )


class PreIdentityLedgerTests(RunCase):
    """A ledger written before machine identity holds bare names; a run still reads them."""

    def make_bare(self, name: str) -> str:
        """Rename snapshot *name* and its ledger entry to the bare, pre-identity form."""
        bare = name.removesuffix(f"-{MACHINE}")
        (self.destination / name).rename(self.destination / bare)
        entries = load_ledger(self.state).entries
        renamed = (replace(e, name=bare) if e.name == name else e for e in entries)
        save_ledger(self.state, Ledger(tuple(renamed)))
        return bare

    def test_if_due_skips_on_todays_bare_complete_entry(self) -> None:
        self.run_backup(self.request())
        self.make_bare(FIRST_NAME)
        self.clock = START + timedelta(hours=1)
        due = replace(self.request(), if_due=True)
        self.assertEqual(self.run_backup(due), brun.EXIT_OK)
        self.assertEqual(self.last_run().outcome, OUTCOME_SKIPPED)
        self.assertEqual(self.memory_snapshot_calls(), [])

    def test_keep_prunes_bare_entries_in_a_mixed_ledger_oldest_first(self) -> None:
        request = replace(
            self.request(), config=replace(self.tree.request.config, keep=2)
        )
        bare = []
        for day in (0, 1):
            self.clock = START + timedelta(days=day)
            self.assertEqual(self.run_backup(request), brun.EXIT_OK)
            bare.append(self.make_bare(self.last_run().snapshot or ""))
        suffixed = []
        for day in (2, 3):
            self.clock = START + timedelta(days=day)
            self.assertEqual(self.run_backup(request), brun.EXIT_OK)
            suffixed.append(self.last_run().snapshot)
            ledgered = [entry.name for entry in load_ledger(self.state).entries]
            expected = [*bare[day - 1 :], *suffixed]
            self.assertEqual(sorted(ledgered), expected)
            folders = sorted(path.name for path in self.destination.iterdir())
            self.assertEqual(folders, expected)


if __name__ == "__main__":
    unittest.main()
