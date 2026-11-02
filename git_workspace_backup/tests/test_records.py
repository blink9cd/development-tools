#!/usr/bin/env python3
"""The manifest, ledger and run record: exact round trips, and 'not ours' for anything else."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import _fixtures as fx  # noqa: F401 - installs the src/ bootstrap

import backup_json as bj
import backup_ledger as bl
import backup_manifest as bm
from backup_model import (
    OUTCOME_COMPLETE,
    STATE_INCOMING,
    STATE_PROMOTED,
    BundleEntry,
    DirtyWorktree,
    FailedItem,
    FileEntry,
    Ledger,
    LedgerEntry,
    Manifest,
    RunRecord,
    StashEntry,
    WorktreeHead,
)

SHA = "a" * 64
MANIFEST = Manifest(
    created="2026-09-14T06:00:00+00:00",
    complete=False,
    bundles=(
        BundleEntry(
            name="workspace.bundle",
            head=SHA,
            refs=("refs/heads/main", "refs/stash"),
            worktree_heads=(WorktreeHead(name="wt", sha=SHA, reachable=False),),
            stashes=(StashEntry(sha=SHA, message="On main: wip"),),
        ),
    ),
    files=(FileEntry(name="workspace.bundle", sha256=SHA, size=10),),
    archive_members=(FileEntry(name="home/.zshrc", sha256=SHA, size=3),),
    dirty_worktrees=(DirtyWorktree(path_name="wt", changed_paths=-1),),
    failed=(FailedItem(item="memory:x", reason="no refs"),),
    skipped_symlinks=("workspace/.notes/link",),
    unclassified_ignored=("new_folder",),
)


def _manifest_payload() -> dict[str, object]:
    return json.loads(bm.manifest_to_json(MANIFEST))


def _with_bundle_key(key: str, value: object) -> dict[str, object]:
    """The fixture manifest payload with its one bundle's *key* set to *value*."""
    payload = _manifest_payload()
    bundles = payload["bundles"]
    assert isinstance(bundles, list)
    bundles[0][key] = value
    return payload


class LedgerValueTests(unittest.TestCase):
    def test_with_entry_replaces_an_entry_of_the_same_name(self) -> None:
        first = LedgerEntry("s1", STATE_PROMOTED, False, "")
        second = LedgerEntry("s1", STATE_PROMOTED, True, SHA)
        self.assertEqual(
            Ledger().with_entry(first).with_entry(second).entries, (second,)
        )

    def test_without_removes_only_the_named_entry(self) -> None:
        keep = LedgerEntry("s2", STATE_PROMOTED, True, SHA)
        ledger = Ledger((LedgerEntry("s1", STATE_INCOMING, False, ""), keep))
        self.assertEqual(ledger.without("s1").entries, (keep,))
        self.assertEqual(ledger.without("absent").entries, ledger.entries)


class ManifestTests(unittest.TestCase):
    def test_round_trip_is_exact(self) -> None:
        self.assertEqual(
            bm.parse_manifest(bm.manifest_to_json(MANIFEST).encode()), MANIFEST
        )

    def test_a_bundle_written_before_stashes_were_recorded_reads_as_none(
        self,
    ) -> None:
        payload = _manifest_payload()
        bundles = payload["bundles"]
        assert isinstance(bundles, list)
        del bundles[0]["stashes"]
        parsed = bm.parse_manifest(json.dumps(payload).encode())
        assert parsed is not None
        self.assertEqual(parsed.bundles[0], replace(MANIFEST.bundles[0], stashes=()))

    def test_the_machine_that_took_a_snapshot_is_recorded_and_read_back(self) -> None:
        manifest = replace(MANIFEST, machine="fixture-mac")
        written = bm.manifest_to_json(manifest)
        self.assertEqual(json.loads(written)["machine"], "fixture-mac")
        self.assertEqual(bm.parse_manifest(written.encode()), manifest)

    def test_a_manifest_written_before_identity_reads_as_no_machine(self) -> None:
        # Written without the key, as before identity, and read back as None, so the
        # snapshots taken before it stay this tool's and keep being pruned.
        self.assertIsNone(MANIFEST.machine)
        self.assertNotIn("machine", _manifest_payload())
        parsed = bm.parse_manifest(bm.manifest_to_json(MANIFEST).encode())
        assert parsed is not None
        self.assertIsNone(parsed.machine)

    def test_a_machine_that_is_not_a_label_is_foreign(self) -> None:
        for value in (None, "", "Fixture-Mac", "a/b", "..", 5, ["fixture-mac"]):
            with self.subTest(value=value):
                payload = {**_manifest_payload(), "machine": value}
                self.assertIsNone(bm.parse_manifest(json.dumps(payload).encode()))

    def test_anything_not_exactly_this_schema_is_foreign(self) -> None:
        cases: dict[str, object] = {
            "wrong tool": {**_manifest_payload(), "tool": "other"},
            "future schema": {**_manifest_payload(), "schema": 2},
            "extra key": {**_manifest_payload(), "extra": 1},
            "bool size": {
                **_manifest_payload(),
                "files": [{"name": "f", "sha256": SHA, "size": True}],
            },
            "negative size": {
                **_manifest_payload(),
                "files": [{"name": "f", "sha256": SHA, "size": -1}],
            },
            "dirty below minus one": {
                **_manifest_payload(),
                "dirty_worktrees": [{"path_name": "wt", "changed_paths": -2}],
            },
            "non-string list item": {**_manifest_payload(), "skipped_symlinks": [3]},
            "list is not a list": {**_manifest_payload(), "failed": "none"},
            "item is not an object": {**_manifest_payload(), "bundles": ["x"]},
            "complete is not a bool": {**_manifest_payload(), "complete": "yes"},
            "created is not a string": {**_manifest_payload(), "created": 5},
            "stash missing a key": _with_bundle_key("stashes", [{"sha": SHA}]),
            "stash is not an object": _with_bundle_key("stashes", ["x"]),
            "extra bundle key": _with_bundle_key("extra", 1),
        }
        for label, payload in cases.items():
            with self.subTest(label):
                self.assertIsNone(bm.parse_manifest(json.dumps(payload).encode()))
        missing = _manifest_payload()
        del missing["created"]
        for label, data in (
            ("missing key", json.dumps(missing).encode()),
            ("not JSON", b"\xff{"),
            ("top level not an object", b"[1, 2]"),
        ):
            with self.subTest(label):
                self.assertIsNone(bm.parse_manifest(data))


#: Names that are empty, absolute, or have an empty or dot component: each can escape.
_ESCAPING_NAMES = ("/abs", "../x", "a/../../b", "a//b", "./a", "", "nul\x00")
#: The manifest fields whose names are later joined onto the snapshot directory.
_NAME_FIELDS = ("files", "archive_members", "bundles")


def _renamed(field: str, name: str) -> Manifest:
    """The fixture manifest with the single entry of *field* renamed to *name*."""
    file_entry = (replace(MANIFEST.files[0], name=name),)
    if field == "files":
        return replace(MANIFEST, files=file_entry)
    if field == "archive_members":
        return replace(MANIFEST, archive_members=file_entry)
    return replace(MANIFEST, bundles=(replace(MANIFEST.bundles[0], name=name),))


class ManifestNameTests(unittest.TestCase):
    """A manifest name is joined onto the snapshot directory, so it must not leave it."""

    def test_the_predicate_accepts_relative_names_and_refuses_escapes(self) -> None:
        for name in (
            "workspace.bundle",
            "extras.zip",
            "memory/p.bundle",
            "workspace/pyproject.toml",
            "home/.zshrc",
            "a..b",
        ):
            with self.subTest(accepted=name):
                self.assertTrue(bj.is_safe_relative_name(name))
        for name in (*_ESCAPING_NAMES, ".", "..", "a/", "a/./b"):
            with self.subTest(refused=name):
                self.assertFalse(bj.is_safe_relative_name(name))

    def test_escaping_names_are_refused_in_every_name_field(self) -> None:
        for field in _NAME_FIELDS:
            for name in _ESCAPING_NAMES:
                with self.subTest(field=field, name=name):
                    data = bm.manifest_to_json(_renamed(field, name)).encode()
                    self.assertIsNone(bm.parse_manifest(data))

    def test_nested_relative_names_are_accepted_in_every_name_field(self) -> None:
        for field in _NAME_FIELDS:
            for name in ("memory/p.bundle", "home/.zshrc"):
                with self.subTest(field=field, name=name):
                    manifest = _renamed(field, name)
                    data = bm.manifest_to_json(manifest).encode()
                    self.assertEqual(bm.parse_manifest(data), manifest)


class LedgerFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.ledger = Ledger(
            (
                LedgerEntry("2026-09-14T060000", STATE_PROMOTED, True, SHA),
                LedgerEntry(".incoming-2026-09-15T060000", STATE_INCOMING, False, ""),
            )
        )

    def test_save_then_load_round_trips(self) -> None:
        bl.save_ledger(self.tmp, self.ledger)
        self.assertEqual(bl.load_ledger(self.tmp), self.ledger)

    def test_a_missing_ledger_is_empty(self) -> None:
        self.assertEqual(bl.load_ledger(self.tmp / "absent"), Ledger())

    def test_a_corrupt_ledger_raises_rather_than_reading_as_empty(self) -> None:
        (self.tmp / bl.LEDGER_NAME).write_text("{", encoding="utf-8", newline="\n")
        with self.assertRaises(bl.LedgerError):
            bl.load_ledger(self.tmp)

    def test_unsafe_names_and_unknown_states_are_rejected(self) -> None:
        for name in ("", ".", "..", "a/b", "nul\x00"):
            with self.subTest(name=name):
                bad = Ledger((LedgerEntry(name, STATE_PROMOTED, True, SHA),))
                self.assertIsNone(bl.parse_ledger(bl.ledger_to_json(bad).encode()))
        unknown = Ledger((LedgerEntry("s", "deleted", True, SHA),))
        self.assertIsNone(bl.parse_ledger(bl.ledger_to_json(unknown).encode()))
        self.assertIsNone(
            bl.parse_ledger(b'{"tool": "other", "schema": 1, "entries": []}')
        )

    def test_a_name_that_appears_twice_is_refused_by_the_parser_and_the_value(
        self,
    ) -> None:
        # An incoming row, then a promoted row of the same name: the plan would take the name
        # as a leftover and, the manifest proof passing, delete the newest snapshot.
        name = "2026-09-14T060000"
        promoted = LedgerEntry(name, STATE_PROMOTED, True, SHA)
        payload = json.loads(bl.ledger_to_json(Ledger((promoted,))))
        row = payload["entries"][0]
        incoming = {**row, "state": STATE_INCOMING, "complete": False}
        payload["entries"] = [incoming, row]
        self.assertIsNone(bl.parse_ledger(json.dumps(payload).encode()))
        with self.assertRaisesRegex(ValueError, "more than once"):
            Ledger((promoted, replace(promoted, complete=False)))

    def test_state_dir_is_keyed_by_destination(self) -> None:
        base = Path("/opt/fixture-home/.workspace_backup")
        real = bl.state_dir_for(base, Path("/opt/fixture-dest/real"))
        scratch = bl.state_dir_for(base, Path("/opt/fixture-dest/scratch"))
        self.assertNotEqual(real, scratch)
        self.assertEqual(real, bl.state_dir_for(base, Path("/opt/fixture-dest/real")))
        self.assertEqual(real.parent, base / "state")

    def test_newest_complete_ignores_incomplete_and_incoming(self) -> None:
        ledger = self.ledger.with_entry(
            LedgerEntry("2026-09-16T060000", STATE_PROMOTED, False, SHA)
        )
        self.assertEqual(bl.newest_complete_name(ledger), "2026-09-14T060000")
        self.assertIsNone(bl.newest_complete_name(Ledger()))

    def test_newest_complete_entry_is_the_entry_its_name_is_taken_from(self) -> None:
        # Status reads the manifest hash from this entry: one selection gives both the name
        # and the hash, so the two can never come from different rules.
        ledger = self.ledger.with_entry(
            LedgerEntry("2026-09-13T060000", STATE_PROMOTED, True, "b" * 64)
        ).with_entry(LedgerEntry("2026-09-16T060000", STATE_PROMOTED, False, SHA))
        self.assertEqual(
            bl.newest_complete_entry(ledger),
            LedgerEntry("2026-09-14T060000", STATE_PROMOTED, True, SHA),
        )
        self.assertIsNone(bl.newest_complete_entry(Ledger()))


class RunRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_write_then_read_round_trips_with_and_without_a_snapshot(self) -> None:
        record = RunRecord(
            finished="2026-09-14T06:01:00+00:00",
            outcome=OUTCOME_COMPLETE,
            detail="ok",
            snapshot="2026-09-14T060000",
            failed=(FailedItem("x", "y"),),
        )
        for value in (record, replace(record, snapshot=None, failed=())):
            with self.subTest(snapshot=value.snapshot):
                bl.write_last_run(self.tmp, value)
                self.assertEqual(bl.read_last_run(self.tmp), value)

    def test_missing_or_foreign_records_read_as_none(self) -> None:
        self.assertIsNone(bl.read_last_run(self.tmp))
        (self.tmp / bl.LAST_RUN_NAME).write_text(
            '{"tool": "workspace_backup", "schema": 1}', encoding="utf-8", newline="\n"
        )
        self.assertIsNone(bl.read_last_run(self.tmp))
        record = RunRecord("t", "exploded", "d", None, ())
        self.assertIsNone(bl.parse_run_record(bl.run_record_to_json(record).encode()))
        bad_snapshot = json.loads(
            bl.run_record_to_json(replace(record, outcome=OUTCOME_COMPLETE))
        )
        bad_snapshot["snapshot"] = 5
        self.assertIsNone(bl.parse_run_record(json.dumps(bad_snapshot).encode()))
        foreign = json.loads(
            bl.run_record_to_json(replace(record, outcome=OUTCOME_COMPLETE))
        )
        foreign["tool"] = "other"
        self.assertIsNone(bl.parse_run_record(json.dumps(foreign).encode()))
