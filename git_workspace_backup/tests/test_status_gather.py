#!/usr/bin/env python3
"""Gathering status facts from a destination and its recorded state.

osascript is answered by FakeRunner routes. launchctl is routed to a refusal: gathering takes
the agent's state as an argument and must never ask launchd itself. Every destination, state
directory and home is a fresh temporary directory; iCloud Drive's folder is only ever derived
under that injected home, never the real one.
"""

from __future__ import annotations

import tempfile
import unicodedata
import unittest
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import _fixtures as fx
from _status_case import (
    COMPLETE_RUN,
    LATER,
    MACHINE,
    MANIFEST,
    NEWEST,
    NOW,
    OLDER,
    OTHER_MACHINE,
    entry,
)

import backup_config as bc
import backup_icloud as bi
import backup_status as bs
from backup_fs import read_bytes_bounded, sha256_bytes
from backup_ledger import (
    LAST_RUN_NAME,
    LEDGER_NAME,
    LedgerError,
    save_ledger,
    write_last_run,
)
from backup_manifest import MANIFEST_NAME, manifest_to_json
from backup_model import Ledger
from backup_promote import MANIFEST_READ_TIMEOUT_S, ReadBoundedFn


def refuse_launchctl(argv: Sequence[str]) -> "tuple[int, str, str]":
    """A launchctl route that fails the test: gathering is handed the agent's state."""
    raise AssertionError(f"status gathering must never run launchctl: {list(argv)}")


def runner_answering(osascript: fx.Route) -> fx.FakeRunner:
    """A runner whose osascript answers through *osascript* and whose launchctl refuses."""
    return fx.FakeRunner({"osascript": osascript, "launchctl": refuse_launchctl})


def all_uploaded(argv: Sequence[str]) -> "tuple[int, str, str]":
    """Every path asked about is in iCloud and uploaded."""
    return 0, fx.upload_states_json(*[(True, True)] * len(fx.osascript_paths(argv))), ""


class GatherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.destination = self.tmp / "dest"
        self.state = self.tmp / "state"
        self.home = self.tmp / "home"

    def gather(
        self,
        run: fx.FakeRunner,
        read_bounded: ReadBoundedFn = read_bytes_bounded,
        *,
        now: datetime = NOW,
        destination: "Path | None" = None,
        home: "Path | None" = None,
    ) -> bs.StatusFacts:
        return bs.gather_facts(
            destination=self.destination if destination is None else destination,
            state=self.state,
            keep=7,
            agent=bs.AGENT_LOADED,
            now=now,
            home=self.home if home is None else home,
            run=run,
            machine=MACHINE,
            read_bounded=read_bounded,
        )

    def place_manifest(self, name: str, data: bytes) -> Path:
        """Write *data* as ``destination/name/MANIFEST.json``; the manifest's path."""
        folder = self.destination / name
        folder.mkdir(parents=True)
        (folder / MANIFEST_NAME).write_bytes(data)
        return folder / MANIFEST_NAME

    def test_an_empty_state_has_nothing_to_check(self) -> None:
        runner = runner_answering(all_uploaded)
        facts = self.gather(runner)
        self.assertEqual(
            facts,
            bs.StatusFacts(
                bs.AGENT_LOADED,
                None,
                Ledger(),
                7,
                None,
                None,
                bi.UPLOAD_NONE,
                NOW,
                False,
                machine=MACHINE,
            ),
        )
        self.assertEqual(runner.calls, [])

    def test_the_newest_complete_manifest_and_every_file_it_lists_are_checked(
        self,
    ) -> None:
        data = manifest_to_json(MANIFEST).encode()
        path = self.place_manifest(NEWEST, data)
        ledger = Ledger(
            (
                entry(OLDER),
                entry(NEWEST, sha=sha256_bytes(data)),
                entry(LATER, complete=False),
            )
        )
        save_ledger(self.state, ledger)
        write_last_run(self.state, COMPLETE_RUN)
        runner = runner_answering(all_uploaded)
        facts = self.gather(runner)
        self.assertEqual(
            (facts.newest_complete, facts.newest_manifest, facts.upload),
            (NEWEST, MANIFEST, bi.UPLOAD_UPLOADED),
        )
        self.assertEqual((facts.ledger, facts.last_run), (ledger, COMPLETE_RUN))
        [argv] = runner.calls
        self.assertEqual(
            fx.osascript_paths(argv), [str(path), str(path.parent / "extras.zip")]
        )

    def test_the_manifest_is_read_through_the_bounded_reader_and_its_timeout(
        self,
    ) -> None:
        data = manifest_to_json(MANIFEST).encode()
        save_ledger(self.state, Ledger((entry(NEWEST, sha=sha256_bytes(data)),)))
        reads: list[tuple[Path, float]] = []

        def read(path: Path, timeout: float) -> "bytes | None":
            reads.append((path, timeout))
            return data

        facts = self.gather(runner_answering(all_uploaded), read)
        self.assertEqual(facts.newest_manifest, MANIFEST)
        self.assertEqual(
            reads,
            [(self.destination / NEWEST / MANIFEST_NAME, MANIFEST_READ_TIMEOUT_S)],
        )

    def test_an_unreadable_manifest_still_checks_its_own_upload(self) -> None:
        save_ledger(self.state, Ledger((entry(NEWEST),)))
        runner = runner_answering(
            fx.osascript_answering(0, fx.upload_states_json((True, False)))
        )
        facts = self.gather(runner, lambda path, timeout: None)
        self.assertIsNone(facts.newest_manifest)
        self.assertEqual(facts.upload, bi.UPLOAD_PENDING)
        [argv] = runner.calls
        self.assertEqual(
            fx.osascript_paths(argv), [str(self.destination / NEWEST / MANIFEST_NAME)]
        )

    def test_a_manifest_that_does_not_hash_to_the_ledger_is_not_trusted(self) -> None:
        data = manifest_to_json(MANIFEST).encode()
        path = self.place_manifest(NEWEST, data)
        save_ledger(self.state, Ledger((entry(NEWEST, sha=sha256_bytes(data + b" ")),)))
        runner = runner_answering(all_uploaded)
        facts = self.gather(runner)
        self.assertIsNone(facts.newest_manifest)
        # Its file list is not believed either: only the manifest itself is asked about.
        [argv] = runner.calls
        self.assertEqual(fx.osascript_paths(argv), [str(path)])

    def test_bytes_that_hash_right_but_do_not_parse_are_no_manifest(self) -> None:
        self.place_manifest(NEWEST, b"{")
        save_ledger(self.state, Ledger((entry(NEWEST, sha=sha256_bytes(b"{")),)))
        facts = self.gather(runner_answering(all_uploaded))
        self.assertIsNone(facts.newest_manifest)
        self.assertEqual(facts.upload, bi.UPLOAD_UPLOADED)

    def test_a_failed_upload_query_reads_as_unreadable(self) -> None:
        data = manifest_to_json(MANIFEST).encode()
        self.place_manifest(NEWEST, data)
        save_ledger(self.state, Ledger((entry(NEWEST, sha=sha256_bytes(data)),)))
        facts = self.gather(runner_answering(fx.osascript_answering(1, "")))
        self.assertEqual(facts.newest_manifest, MANIFEST)
        self.assertEqual(facts.upload, bi.UPLOAD_UNREADABLE)

    def test_a_corrupt_ledger_raises_ledger_error(self) -> None:
        self.state.mkdir(parents=True)
        (self.state / LEDGER_NAME).write_text("{", encoding="utf-8", newline="\n")
        with self.assertRaises(LedgerError):
            self.gather(runner_answering(all_uploaded))

    def test_state_files_that_exist_but_cannot_be_read_raise_a_ledger_error(
        self,
    ) -> None:
        # A directory where the file should be: it exists, so it is not absent, and reading
        # it fails. The CLI reports a LedgerError as "could not judge", with this reason.
        (self.state / LEDGER_NAME).mkdir(parents=True)
        with self.assertRaises(LedgerError) as caught:
            self.gather(runner_answering(all_uploaded))
        self.assertIn(LEDGER_NAME, str(caught.exception))
        (self.state / LEDGER_NAME).rmdir()
        (self.state / LAST_RUN_NAME).mkdir()
        with self.assertRaises(LedgerError) as caught:
            self.gather(runner_answering(all_uploaded))
        self.assertIn(LAST_RUN_NAME, str(caught.exception))

    def test_snapshot_folders_the_ledger_does_not_track_are_listed_never_followed(
        self,
    ) -> None:
        save_ledger(self.state, Ledger((entry(NEWEST),)))
        (self.destination / NEWEST).mkdir(parents=True)
        untracked = ("2026-09-12T060000", OLDER)
        for name in untracked:
            (self.destination / name).mkdir()
        # None of these is a snapshot folder: a plain name, a file, a link to a directory
        # (never followed), and a cross-volume copy's leftover.
        (self.destination / "not-a-snapshot").mkdir()
        (self.destination / "2026-09-11T060000").write_text(
            "x", encoding="utf-8", newline="\n"
        )
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (self.destination / "2026-09-10T060000").symlink_to(
            elsewhere, target_is_directory=True
        )
        (self.destination / ".incoming-2026-09-09T060000").mkdir()
        facts = self.gather(runner_answering(all_uploaded), lambda path, timeout: None)
        self.assertEqual(facts.untracked, untracked)

    def test_folders_are_told_apart_by_the_machine_their_names_carry(self) -> None:
        own = f"2026-09-13T060000-{MACHINE}"
        save_ledger(self.state, Ledger((entry(own),)))
        bare = "2026-09-11T060000"
        own_untracked = f"2026-09-12T060000-{MACHINE}"
        others = (f"2026-09-12T060000-{OTHER_MACHINE}", f"{OLDER}-{OTHER_MACHINE}")
        for name in (own, bare, own_untracked, *others):
            (self.destination / name).mkdir(parents=True)
        # Not snapshot folders of another machine: a file and a cross-volume leftover.
        (self.destination / f"2026-09-10T060000-{OTHER_MACHINE}").write_text(
            "x", encoding="utf-8", newline="\n"
        )
        (self.destination / f".incoming-2026-09-09T060000-{OTHER_MACHINE}").mkdir()
        facts = self.gather(runner_answering(all_uploaded), lambda path, timeout: None)
        self.assertEqual(facts.untracked, (bare, own_untracked))
        self.assertEqual(facts.other_machines, len(others))
        self.assertEqual(facts.machine, MACHINE)

    def test_an_unreadable_destination_lists_nothing_and_the_listing_is_bounded(
        self,
    ) -> None:
        names = ("2026-09-10T060000", "2026-09-11T060000", "2026-09-12T060000")
        for name in names:
            (self.destination / name).mkdir(parents=True)
        with self.subTest("bounded"):
            with mock.patch.object(bs, "UNTRACKED_SCAN_LIMIT", len(names) - 1):
                facts = self.gather(runner_answering(all_uploaded))
            self.assertEqual(len(facts.untracked), len(names) - 1)
        with self.subTest("unreadable"):
            # Already a problem of its own elsewhere in the verdict, so not said twice.
            self.destination.chmod(0o000)
            self.addCleanup(self.destination.chmod, 0o700)
            facts = self.gather(runner_answering(all_uploaded))
            self.destination.chmod(0o700)
            self.assertEqual(facts.untracked, ())

    def test_an_aware_now_is_refused_before_anything_is_read(self) -> None:
        # The corrupt ledger would raise LedgerError instead if anything were read first.
        self.state.mkdir(parents=True)
        (self.state / LEDGER_NAME).write_text("{", encoding="utf-8", newline="\n")
        runner = runner_answering(all_uploaded)
        with self.assertRaisesRegex(ValueError, "now"):
            self.gather(runner, now=NOW.replace(tzinfo=timezone.utc))
        self.assertEqual(runner.calls, [])


class IcloudDrivePlacementTests(unittest.TestCase):
    """Whether the destination lies in iCloud Drive, judged against the injected home."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = self.tmp / "state"

    def in_icloud_drive(self, destination: Path, home: Path) -> bool:
        facts = bs.gather_facts(
            destination=destination,
            state=self.state,
            keep=7,
            agent=bs.AGENT_LOADED,
            now=NOW,
            home=home,
            run=runner_answering(all_uploaded),
            machine=MACHINE,
        )
        return facts.in_icloud_drive

    def test_the_root_and_what_is_under_it_are_in_and_a_prefix_sibling_is_not(
        self,
    ) -> None:
        home = self.tmp / "home"
        root = bc.icloud_drive_root(home)
        cases = {
            "the root itself": (root, True),
            "a folder under the root": (root / "workspace-backup", True),
            # Component-wise: a folder whose name merely starts with the root's is elsewhere.
            "a sibling sharing its prefix": (
                root.with_name(root.name + "-other") / "workspace-backup",
                False,
            ),
            "a local folder": (self.tmp / "elsewhere", False),
            # The default APFS volume ignores case, and a resolved path keeps the case it
            # was given, so another spelling of the root is the same folder.
            "the root spelled in another case": (Path(str(root).swapcase()), True),
        }
        for label, (destination, expected) in cases.items():
            with self.subTest(label):
                self.assertIs(self.in_icloud_drive(destination, home), expected)

    def test_a_destination_spelled_in_the_other_normalization_form_is_in(self) -> None:
        # APFS matches names across Unicode normalization forms, so an NFD spelling of an
        # NFC home names the same folder; compared code point by code point it reads as
        # elsewhere, and a signed-out iCloud Drive would be a note instead of a problem.
        home = self.tmp / unicodedata.normalize("NFC", "hôme")
        spelled = bc.icloud_drive_root(home) / "workspace-backup"
        destination = Path(unicodedata.normalize("NFD", str(spelled)))
        self.assertNotEqual(str(destination), str(spelled))
        self.assertTrue(self.in_icloud_drive(destination, home))

    def test_a_link_on_either_side_resolves_to_the_same_folder(self) -> None:
        real = self.tmp / "real-home"
        (real / bc.ICLOUD_DRIVE).mkdir(parents=True)
        link = self.tmp / "home-link"
        link.symlink_to(real, target_is_directory=True)
        leaf = "workspace-backup"
        self.assertTrue(self.in_icloud_drive(real / bc.ICLOUD_DRIVE / leaf, link))
        self.assertTrue(self.in_icloud_drive(link / bc.ICLOUD_DRIVE / leaf, real))


if __name__ == "__main__":
    unittest.main()
