#!/usr/bin/env python3
"""Snapshot names by example; promotion never replaces a folder; the sweep stays in its root.

Prune's tests live in test_prune.py, and the name properties in test_properties_promote.py.
"""

from __future__ import annotations

import errno
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import _fixtures as fx

import backup_promote as bp
from backup_manifest import MANIFEST_NAME
from backup_model import STATE_INCOMING, Ledger, LedgerEntry

SNAPSHOT = "2026-09-14T060000"
#: An obvious fixture machine label, never this machine's own.
MACHINE = "fixture-mac"


class NameTests(unittest.TestCase):
    def test_names_round_trip_and_sort_chronologically(self) -> None:
        moment = datetime(2026, 9, 14, 6, 0, 5)
        self.assertEqual(bp.snapshot_name(moment), "2026-09-14T060005")
        self.assertEqual(bp.parse_snapshot_name("2026-09-14T060005"), moment)
        self.assertIsNone(bp.parse_snapshot_name(".incoming-2026-09-14T060005"))
        self.assertLess(
            bp.snapshot_name(moment), bp.snapshot_name(datetime(2026, 9, 15))
        )

    def test_a_name_strptime_accepts_but_snapshot_name_never_writes_is_refused(
        self,
    ) -> None:
        # strptime reads each of these as 2026-09-14/04 at some time, so only the
        # round-trip check refuses them; the last three strptime refuses itself.
        for name in (
            "2026-9-14T060005",  # unpadded month
            "2026-09-4T060005",  # unpadded day
            "2026-09-14T60005",  # unpadded hour
            "2026-09-14T06705",  # unpadded minute
            "2026-09-14T06005",  # unpadded second
            "٢٠٢٦-09-14T060005",  # non-ASCII digits
            "2026-09-14T060005x",  # trailing junk
            "2026-09-14T060005\n",
            "2026-02-30T060000",  # no such day
        ):
            with self.subTest(name=name):
                self.assertIsNone(bp.parse_snapshot_name(name))

    def test_a_name_carries_the_machine_after_its_timestamp(self) -> None:
        moment = datetime(2026, 9, 14, 6, 0, 5)
        name = bp.snapshot_name(moment, MACHINE)
        self.assertEqual(name, f"2026-09-14T060005-{MACHINE}")
        self.assertEqual(bp.parse_snapshot_name(name), moment)
        parts = bp.split_snapshot_name(name)
        self.assertEqual(parts, (moment, MACHINE))
        assert parts is not None
        self.assertEqual(parts.machine, MACHINE)

    def test_a_bare_name_was_written_before_identity_and_names_no_machine(
        self,
    ) -> None:
        parts = bp.split_snapshot_name("2026-09-14T060005")
        self.assertIsNotNone(parts)
        assert parts is not None
        self.assertEqual(parts.moment, datetime(2026, 9, 14, 6, 0, 5))
        self.assertIsNone(parts.machine)

    def test_a_machine_suffix_that_is_not_a_label_is_refused(self) -> None:
        for tail in (
            "-",
            "-Fixture-mac",
            "_fixture-mac",
            "fixture-mac",
            "-fixture--mac",
            "-fixture-mac-",
            "-fixture/mac",
            "-fixture.mac",
            "-" + "a" * 33,
            "-fixture-mac\n",
        ):
            with self.subTest(tail=tail):
                name = f"2026-09-14T060005{tail}"
                self.assertIsNone(bp.split_snapshot_name(name))
                self.assertIsNone(bp.parse_snapshot_name(name))
        self.assertIsNone(
            bp.split_snapshot_name(f".incoming-2026-09-14T060005-{MACHINE}")
        )
        self.assertIsNone(bp.split_snapshot_name("no time mark"))

    def test_a_machine_that_is_not_a_label_is_never_written_into_a_name(self) -> None:
        for machine in ("", "Fixture", "a/b", ".."):
            with self.subTest(machine=machine):
                with self.assertRaisesRegex(ValueError, "not a machine label"):
                    bp.snapshot_name(datetime(2026, 9, 14), machine)

    def test_names_sort_by_time_first_whatever_the_machine(self) -> None:
        earlier = bp.snapshot_name(datetime(2026, 9, 14, 6, 0, 0), "zz-fixture")
        later = bp.snapshot_name(datetime(2026, 9, 14, 6, 0, 1), "aa-fixture")
        bare_later = bp.snapshot_name(datetime(2026, 9, 15))
        self.assertEqual(
            sorted([bare_later, later, earlier]), [earlier, later, bare_later]
        )


class PromoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.staging = self.tmp / "staging" / SNAPSHOT
        self.staging.mkdir(parents=True)
        (self.staging / MANIFEST_NAME).write_text("{}", encoding="utf-8", newline="\n")
        self.destination = self.tmp / "dest"
        self.saved: list[Ledger] = []
        self.renames: list[tuple[Path, Path]] = []

    def _recording_rename(self, src: Path, dst: Path) -> None:
        self.renames.append((src, dst))
        os.rename(src, dst)

    def _cross_volume_once(self, src: Path, dst: Path) -> None:
        self.renames.append((src, dst))
        if len(self.renames) == 1:
            raise OSError(errno.EXDEV, "cross-device link")
        os.rename(src, dst)

    def test_a_rename_moves_the_snapshot_and_creates_the_destination(self) -> None:
        target, ledger = bp.promote(
            self.staging,
            self.destination,
            "2026-09-14T060000",
            Ledger(),
            save_ledger=self.saved.append,
        )
        self.assertEqual(target, self.destination / "2026-09-14T060000")
        self.assertTrue((target / MANIFEST_NAME).is_file())
        self.assertFalse(self.staging.exists())
        self.assertEqual((ledger, self.saved), (Ledger(), []))

    def test_a_name_carrying_a_machine_is_promoted(self) -> None:
        name = f"{SNAPSHOT}-{MACHINE}"
        target, _ = bp.promote(
            self.staging,
            self.destination,
            name,
            Ledger(),
            save_ledger=self.saved.append,
        )
        self.assertEqual(target, self.destination / name)
        self.assertTrue((target / MANIFEST_NAME).is_file())

    def test_an_existing_folder_is_never_replaced(self) -> None:
        (self.destination / "2026-09-14T060000").mkdir(parents=True)
        with self.assertRaisesRegex(bp.PromoteError, "already exists"):
            bp.promote(
                self.staging,
                self.destination,
                "2026-09-14T060000",
                Ledger(),
                save_ledger=self.saved.append,
            )

    def test_an_empty_target_is_refused_before_any_rename_is_attempted(self) -> None:
        # rename(2) silently replaces an empty directory, so only the check refuses it.
        (self.destination / "2026-09-14T060000").mkdir(parents=True)
        with self.assertRaisesRegex(bp.PromoteError, "already exists"):
            bp.promote(
                self.staging,
                self.destination,
                "2026-09-14T060000",
                Ledger(),
                save_ledger=self.saved.append,
                rename=self._recording_rename,
            )
        self.assertEqual(self.renames, [])
        self.assertTrue((self.staging / MANIFEST_NAME).is_file())

    def test_a_dangling_symlink_at_the_target_is_refused(self) -> None:
        self.destination.mkdir()
        (self.destination / "2026-09-14T060000").symlink_to(self.tmp / "nowhere")
        with self.assertRaisesRegex(bp.PromoteError, "already exists"):
            bp.promote(
                self.staging,
                self.destination,
                "2026-09-14T060000",
                Ledger(),
                save_ledger=self.saved.append,
                rename=self._recording_rename,
            )
        self.assertEqual(self.renames, [])

    def test_a_target_that_cannot_be_checked_is_never_moved_onto(self) -> None:
        # A destination that cannot be searched: lstat of the target fails with EACCES,
        # which is neither "absent" nor "present".
        self.destination.mkdir()
        self.destination.chmod(0o600)
        self.addCleanup(self.destination.chmod, 0o700)
        with self.assertRaisesRegex(bp.PromoteError, "could not check"):
            bp.promote(
                self.staging,
                self.destination,
                SNAPSHOT,
                Ledger(),
                save_ledger=self.saved.append,
                rename=self._recording_rename,
            )
        self.assertEqual(self.renames, [])

    def test_a_name_that_is_not_a_snapshot_name_is_refused_before_anything_moves(
        self,
    ) -> None:
        for name in ("a/b", f"../{SNAPSHOT}", "2026-9-14T060000", fx.TOO_LONG_NAME, ""):
            with self.subTest(name=name):
                with self.assertRaisesRegex(bp.PromoteError, "not a snapshot name"):
                    bp.promote(
                        self.staging,
                        self.destination,
                        name,
                        Ledger(),
                        save_ledger=self.saved.append,
                        rename=self._recording_rename,
                    )
                self.assertEqual((self.renames, self.saved), ([], []))
                self.assertFalse(self.destination.exists())
                self.assertTrue((self.staging / MANIFEST_NAME).is_file())

    def test_a_destination_whose_parent_is_absent_is_not_created(self) -> None:
        destination = self.tmp / "absent" / "dest"
        with self.assertRaisesRegex(bp.PromoteError, "could not create"):
            bp.promote(
                self.staging,
                destination,
                "2026-09-14T060000",
                Ledger(),
                save_ledger=self.saved.append,
            )
        self.assertFalse((self.tmp / "absent").exists())
        self.assertTrue((self.staging / MANIFEST_NAME).is_file())

    def test_a_rename_failure_other_than_cross_volume_is_an_error(self) -> None:
        def refuse(src: Path, dst: Path) -> None:
            raise PermissionError(errno.EACCES, "denied")

        with self.assertRaisesRegex(bp.PromoteError, "could not move"):
            bp.promote(
                self.staging,
                self.destination,
                SNAPSHOT,
                Ledger(),
                save_ledger=self.saved.append,
                rename=refuse,
            )

    def test_cross_volume_records_intent_first_then_copies(self) -> None:
        target, ledger = bp.promote(
            self.staging,
            self.destination,
            "2026-09-14T060000",
            Ledger(),
            save_ledger=self.saved.append,
            rename=self._cross_volume_once,
        )
        self.assertTrue((target / MANIFEST_NAME).is_file())
        self.assertFalse(self.staging.exists())
        self.assertEqual(
            self.saved,
            [
                Ledger(
                    (
                        LedgerEntry(
                            ".incoming-2026-09-14T060000", STATE_INCOMING, False, ""
                        ),
                    )
                )
            ],
        )
        self.assertEqual(ledger, Ledger())

    def test_a_failed_cross_volume_copy_leaves_its_intent_recorded(self) -> None:
        def always_cross(src: Path, dst: Path) -> None:
            raise OSError(errno.EXDEV, "cross-device link")

        def broken_copy(src: Path, dst: Path) -> object:
            raise OSError(errno.ENOSPC, "no space")

        with self.assertRaisesRegex(bp.PromoteError, "cross-volume copy"):
            bp.promote(
                self.staging,
                self.destination,
                SNAPSHOT,
                Ledger(),
                save_ledger=self.saved.append,
                rename=always_cross,
                copy_tree=broken_copy,
            )
        self.assertEqual(self.saved[-1].entries[0].state, STATE_INCOMING)

    def test_a_target_that_appears_during_a_cross_volume_copy_is_never_replaced(
        self,
    ) -> None:
        # The copy is slow, so the target is checked again right before the final rename;
        # rename(2) would otherwise silently replace the empty folder made here.
        def copy_then_occupy(src: Path, dst: Path) -> object:
            shutil.copytree(src, dst, symlinks=True)
            (self.destination / SNAPSHOT).mkdir()
            return dst

        with self.assertRaisesRegex(bp.PromoteError, "already exists"):
            bp.promote(
                self.staging,
                self.destination,
                SNAPSHOT,
                Ledger(),
                save_ledger=self.saved.append,
                rename=self._cross_volume_once,
                copy_tree=copy_then_occupy,
            )
        incoming = self.destination / f"{bp.INCOMING_PREFIX}{SNAPSHOT}"
        self.assertEqual(len(self.renames), 1)
        self.assertEqual(list((self.destination / SNAPSHOT).iterdir()), [])
        self.assertTrue((incoming / MANIFEST_NAME).is_file())
        self.assertEqual(self.saved[-1].entries[0].name, incoming.name)

    def test_an_intent_that_cannot_be_recorded_copies_nothing(self) -> None:
        copies: list[Path] = []

        def always_cross(src: Path, dst: Path) -> None:
            raise OSError(errno.EXDEV, "cross-device link")

        def unwritable(ledger: Ledger) -> None:
            raise OSError(errno.EROFS, "read-only")

        def recording_copy(src: Path, dst: Path) -> object:
            copies.append(dst)
            return dst

        with self.assertRaisesRegex(bp.PromoteError, "could not record"):
            bp.promote(
                self.staging,
                self.destination,
                SNAPSHOT,
                Ledger(),
                save_ledger=unwritable,
                rename=always_cross,
                copy_tree=recording_copy,
            )
        self.assertEqual(copies, [])

    def test_a_staging_copy_that_will_not_go_is_left_for_the_sweep(self) -> None:
        def stuck(path: Path) -> None:
            raise OSError(errno.EBUSY, "busy")

        target, ledger = bp.promote(
            self.staging,
            self.destination,
            "2026-09-14T060000",
            Ledger(),
            save_ledger=self.saved.append,
            rename=self._cross_volume_once,
            remove_tree=stuck,
        )
        self.assertTrue((target / MANIFEST_NAME).is_file())
        self.assertTrue(self.staging.is_dir())
        self.assertEqual(ledger, Ledger())


class StagingSweepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_everything_left_in_staging_is_removed(self) -> None:
        root = self.tmp / "staging"
        (root / "old-run").mkdir(parents=True)
        (root / "stray.tmp").write_text("x", encoding="utf-8", newline="\n")
        self.assertEqual(bp.sweep_staging(root), ("old-run", "stray.tmp"))
        self.assertEqual(list(root.iterdir()), [])
        self.assertEqual(bp.sweep_staging(self.tmp / "absent"), ())

    def test_only_direct_children_go_and_symlinks_are_never_followed(self) -> None:
        root = self.tmp / "staging"
        (root / "old-run" / "deep").mkdir(parents=True)
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("k", encoding="utf-8", newline="\n")
        (root / "dir-link").symlink_to(outside, target_is_directory=True)
        (root / "file-link").symlink_to(outside / "keep.txt")
        handed: list[Path] = []
        self.assertEqual(
            bp.sweep_staging(root, remove_tree=handed.append),
            ("dir-link", "file-link", "old-run"),
        )
        self.assertEqual(handed, [root / "old-run"])
        self.assertTrue((outside / "keep.txt").is_file())
        self.assertFalse((root / "dir-link").is_symlink())
        self.assertTrue(root.is_dir())

    def test_a_symlinked_staging_root_sweeps_nothing(self) -> None:
        real = self.tmp / "real"
        (real / "old-run").mkdir(parents=True)
        (real / "stray.tmp").write_text("x", encoding="utf-8", newline="\n")
        root = self.tmp / "staging"
        root.symlink_to(real, target_is_directory=True)
        handed: list[Path] = []
        self.assertEqual(bp.sweep_staging(root, remove_tree=handed.append), ())
        self.assertEqual(handed, [])
        self.assertTrue((real / "stray.tmp").is_file())
        self.assertTrue(root.is_symlink())


if __name__ == "__main__":
    unittest.main()
