#!/usr/bin/env python3
"""Prune deletes only what the ledger names, never through a link, and only when proven."""

from __future__ import annotations

import errno
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import _fixtures as fx

import backup_promote as bp
from backup_fs import sha256_bytes
from backup_manifest import MANIFEST_NAME
from backup_model import STATE_INCOMING, STATE_PROMOTED, Ledger, LedgerEntry

#: How long a prune over one FIFO manifest may take. Far under MANIFEST_READ_TIMEOUT_S, so a
#: read that waits on the FIFO for a writer fails here instead of passing late.
FIFO_PRUNE_BOUND_S = 5.0

# mkfifo(3) is POSIX-only. The split is an ``if`` so a win32 type-check pass
# still sees the name; the win32 arm exists only for that pass.
if sys.platform == "win32":

    def _make_fifo(path: Path) -> None:  # pragma: only-on win32
        """Refuse: Windows has no FIFOs, and this tool never runs there."""
        raise OSError(errno.ENOSYS, "mkfifo needs POSIX, which Windows lacks")

else:  # pragma: only-on posix
    _make_fifo = os.mkfifo


class PruneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.destination = self.tmp / "dest"
        self.ledger = Ledger()

    def _snapshot(self, name: str, *, complete: bool = True) -> None:
        folder = self.destination / name
        folder.mkdir(parents=True)
        data = f'{{"name": "{name}"}}'.encode()
        (folder / MANIFEST_NAME).write_bytes(data)
        self.ledger = self.ledger.with_entry(
            LedgerEntry(name, STATE_PROMOTED, complete, sha256_bytes(data))
        )

    def test_plan_keeps_newest_complete_and_incomplete_and_drops_incoming(
        self,
    ) -> None:
        ledger = Ledger(
            tuple(
                LedgerEntry(f"2026-09-1{i}T060000", STATE_PROMOTED, i % 2 == 0, "")
                for i in range(6)
            )
            + (LedgerEntry(".incoming-x", STATE_INCOMING, False, ""),)
        )
        self.assertEqual(
            bp.plan_prune(ledger, 2),
            ("2026-09-10T060000", "2026-09-11T060000", ".incoming-x"),
        )

    def test_a_negative_keep_is_refused_rather_than_sliced(self) -> None:
        self._snapshot("2026-09-01T060000")
        with self.assertRaisesRegex(ValueError, "keep"):
            bp.plan_prune(self.ledger, -1)

    def test_old_snapshots_are_deleted_and_the_rest_kept(self) -> None:
        for day in range(1, 5):
            self._snapshot(f"2026-09-0{day}T060000")
        (self.destination / "not-ours").mkdir()
        result = bp.prune(self.destination, self.ledger, 2)
        self.assertEqual(result.deleted, ("2026-09-02T060000", "2026-09-01T060000"))
        self.assertEqual(
            sorted(p.name for p in self.destination.iterdir()),
            ["2026-09-03T060000", "2026-09-04T060000", "not-ours"],
        )
        self.assertEqual(
            [e.name for e in result.ledger.entries],
            ["2026-09-03T060000", "2026-09-04T060000"],
        )

    def test_a_snapshot_shaped_folder_the_ledger_does_not_hold_is_never_deleted(
        self,
    ) -> None:
        self._snapshot("2026-09-02T060000")
        orphan = self.destination / "2026-09-01T060000"
        orphan.mkdir()
        (orphan / MANIFEST_NAME).write_bytes(b"{}")
        result = bp.prune(self.destination, self.ledger, 0)
        self.assertEqual(result.deleted, ("2026-09-02T060000",))
        self.assertTrue((orphan / MANIFEST_NAME).is_file())

    def test_the_manifest_is_read_bounded_by_the_named_timeout(self) -> None:
        self._snapshot("2026-09-01T060000")
        reads: list[tuple[Path, float]] = []

        def recording_read(path: Path, timeout: float) -> bytes:
            reads.append((path, timeout))
            return path.read_bytes()

        result = bp.prune(self.destination, self.ledger, 0, read_bounded=recording_read)
        self.assertEqual(result.deleted, ("2026-09-01T060000",))
        self.assertEqual(bp.MANIFEST_READ_TIMEOUT_S, 60.0)
        self.assertEqual(
            reads,
            [
                (
                    self.destination / "2026-09-01T060000" / MANIFEST_NAME,
                    bp.MANIFEST_READ_TIMEOUT_S,
                )
            ],
        )

    def test_unproven_folders_are_deferred_never_forced(self) -> None:
        for day in range(1, 5):
            self._snapshot(f"2026-09-0{day}T060000")
        # Incomplete snapshots are kept on their own count, so the oldest of these three is
        # planned, and it must meet the same manifest proof as a complete one.
        for day in range(1, 4):
            self._snapshot(f"2026-08-0{day}T060000", complete=False)
        for name in ("2026-09-01T060000", "2026-08-01T060000"):
            (self.destination / name / MANIFEST_NAME).write_text(
                "tampered", encoding="utf-8", newline="\n"
            )
        result = bp.prune(
            self.destination,
            self.ledger,
            2,
            read_bounded=lambda path, timeout: (
                None if "09-02" in str(path) else path.read_bytes()
            ),
        )
        self.assertEqual(result.deleted, ())
        self.assertEqual(
            sorted(result.deferred),
            ["2026-08-01T060000", "2026-09-01T060000", "2026-09-02T060000"],
        )
        self.assertEqual(len(result.ledger.entries), 7)
        for name in result.deferred:
            self.assertTrue((self.destination / name).is_dir(), name)

    def test_symlinks_vanished_folders_incoming_leftovers_and_remove_failures(
        self,
    ) -> None:
        vanished = "2026-08-20T060000"
        leftover = f"{bp.INCOMING_PREFIX}2026-09-04T060000"
        self._snapshot("2026-09-01T060000")
        self._snapshot("2026-09-02T060000")
        self._snapshot("2026-09-03T060000")
        (self.destination / "2026-09-01T060000").rename(self.tmp / "moved")
        (self.destination / "2026-09-01T060000").symlink_to(self.tmp / "moved")
        self.ledger = self.ledger.with_entry(
            LedgerEntry(vanished, STATE_PROMOTED, False, "")
        )
        self.ledger = self.ledger.with_entry(
            LedgerEntry(leftover, STATE_INCOMING, False, "")
        )
        (self.destination / leftover).mkdir()
        result = bp.prune(self.destination, self.ledger, 0)
        self.assertIn("2026-09-01T060000", result.deferred)
        self.assertTrue((self.tmp / "moved").is_dir())
        self.assertNotIn(vanished, [e.name for e in result.ledger.entries])
        self.assertNotIn(vanished, result.deleted)
        self.assertIn(leftover, result.deleted)

        def refuse(path: Path) -> None:
            raise OSError(errno.EBUSY, "busy")

        self._snapshot("2026-09-09T060000")
        stuck = bp.prune(
            self.destination, self.ledger.without(vanished), 0, remove_tree=refuse
        )
        self.assertIn("2026-09-09T060000", stuck.deferred)

    def test_a_symlinked_folder_is_never_handed_to_remove_tree(self) -> None:
        # The manifest behind the link still hashes to the ledger's value, so only the
        # link check keeps the link from reaching remove_tree.
        self._snapshot("2026-09-01T060000")
        link = self.destination / "2026-09-01T060000"
        link.rename(self.tmp / "moved")
        link.symlink_to(self.tmp / "moved", target_is_directory=True)
        handed: list[Path] = []
        result = bp.prune(
            self.destination,
            self.ledger,
            0,
            read_bounded=lambda path, timeout: path.read_bytes(),
            remove_tree=handed.append,
        )
        self.assertEqual((handed, result.deferred), ([], ("2026-09-01T060000",)))

    def test_a_folder_swapped_for_a_symlink_mid_prune_is_not_followed(self) -> None:
        # Swapped after the link check, during the manifest read: the default remove_tree
        # (rmtree) must itself refuse the link rather than delete what it points at.
        self._snapshot("2026-09-01T060000")
        folder = self.destination / "2026-09-01T060000"
        moved = self.tmp / "moved"

        def read_then_swap(path: Path, timeout: float) -> bytes:
            data = path.read_bytes()
            folder.rename(moved)
            folder.symlink_to(moved, target_is_directory=True)
            return data

        result = bp.prune(self.destination, self.ledger, 0, read_bounded=read_then_swap)
        self.assertEqual(result.deferred, ("2026-09-01T060000",))
        self.assertTrue((moved / MANIFEST_NAME).is_file())

    def test_a_symlinked_manifest_is_no_proof_even_with_matching_bytes(self) -> None:
        self._snapshot("2026-09-01T060000")
        manifest = self.destination / "2026-09-01T060000" / MANIFEST_NAME
        outside = self.tmp / "outside.json"
        outside.write_bytes(manifest.read_bytes())
        manifest.unlink()
        manifest.symlink_to(outside)
        result = bp.prune(self.destination, self.ledger, 0)
        self.assertEqual(
            (result.deleted, result.deferred), ((), ("2026-09-01T060000",))
        )
        self.assertTrue(manifest.is_symlink())
        self.assertTrue(outside.is_file())

    def test_a_fifo_manifest_is_deferred_at_once_and_leaves_no_blocked_reader(
        self,
    ) -> None:
        self._snapshot("2026-09-01T060000")
        manifest = self.destination / "2026-09-01T060000" / MANIFEST_NAME
        manifest.unlink()
        _make_fifo(manifest)
        threads_before = threading.active_count()
        started = time.monotonic()
        result = bp.prune(self.destination, self.ledger, 0)
        elapsed = time.monotonic() - started
        self.assertEqual(
            (result.deleted, result.deferred), ((), ("2026-09-01T060000",))
        )
        self.assertLess(elapsed, FIFO_PRUNE_BOUND_S)
        self.assertLessEqual(threading.active_count(), threads_before)

    def test_a_folder_that_cannot_be_examined_is_deferred_and_kept(self) -> None:
        # A destination that cannot be searched: stat of the destination itself succeeds,
        # lstat of a folder inside it fails with EACCES -- neither absent nor present.
        self._snapshot("2026-09-01T060000")
        self.destination.chmod(0o600)
        self.addCleanup(self.destination.chmod, 0o700)
        result = bp.prune(self.destination, self.ledger, 0)
        self.assertEqual(
            (result.deleted, result.deferred), ((), ("2026-09-01T060000",))
        )
        self.assertEqual(result.ledger, self.ledger)

    def test_a_destination_that_is_not_a_directory_defers_everything(self) -> None:
        leftover = f"{bp.INCOMING_PREFIX}2026-09-01T060000"
        ledger = Ledger((LedgerEntry(leftover, STATE_INCOMING, False, ""),))
        result = bp.prune(self.destination, ledger, 0)
        self.assertEqual((result.deleted, result.deferred), ((), (leftover,)))
        self.assertEqual(result.ledger, ledger)

    def test_an_incoming_entry_naming_a_snapshot_folder_never_deletes_it(self) -> None:
        # Incoming entries skip the manifest proof, so one naming a real snapshot folder
        # would delete it unproven.
        self._snapshot("2026-09-14T060000")
        ledger = Ledger((LedgerEntry("2026-09-14T060000", STATE_INCOMING, False, ""),))
        result = bp.prune(self.destination, ledger, 5)
        self.assertEqual(
            (result.deleted, result.deferred), ((), ("2026-09-14T060000",))
        )
        self.assertTrue(
            (self.destination / "2026-09-14T060000" / MANIFEST_NAME).is_file()
        )

    def test_an_incoming_entry_that_is_not_an_incoming_snapshot_name_is_deferred(
        self,
    ) -> None:
        for name in (
            ".incoming-garbage",
            "_incoming-2026-09-14T060000",  # look-alike prefix, snapshot-name tail
            ".incoming-2026-9-14T060000",  # non-canonical tail
            ".incoming-2026-09-14T060000-Fixture",  # a machine that is not a label
            ".incoming-",
        ):
            with self.subTest(name=name):
                (self.destination / name).mkdir(parents=True, exist_ok=True)
                ledger = Ledger((LedgerEntry(name, STATE_INCOMING, False, ""),))
                result = bp.prune(self.destination, ledger, 0)
                self.assertEqual((result.deleted, result.deferred), ((), (name,)))
                self.assertTrue((self.destination / name).is_dir())

    def test_names_carrying_a_machine_are_proven_and_pruned_like_bare_ones(
        self,
    ) -> None:
        # Bare names come from before machine identity, and roll off through keep.
        self._snapshot("2026-09-01T060000")
        self._snapshot("2026-09-02T060000-fixture-mac")
        self._snapshot("2026-09-03T060000-fixture-mac")
        leftover = f"{bp.INCOMING_PREFIX}2026-09-02T070000-fixture-mac"
        (self.destination / leftover).mkdir()
        ledger = self.ledger.with_entry(
            LedgerEntry(leftover, STATE_INCOMING, False, "")
        )
        result = bp.prune(self.destination, ledger, 1)
        self.assertEqual(
            result.deleted,
            ("2026-09-02T060000-fixture-mac", "2026-09-01T060000", leftover),
        )
        self.assertEqual(
            [entry.name for entry in result.ledger.entries],
            ["2026-09-03T060000-fixture-mac"],
        )

    def test_a_promoted_entry_whose_name_does_not_round_trip_is_deferred(self) -> None:
        # Its manifest hashes to the ledger's value, so only the name check refuses it.
        self._snapshot("2026-9-14T060000")
        result = bp.prune(self.destination, self.ledger, 0)
        self.assertEqual((result.deleted, result.deferred), ((), ("2026-9-14T060000",)))
        self.assertTrue((self.destination / "2026-9-14T060000").is_dir())

    def test_a_ledger_name_that_cannot_be_one_of_ours_is_never_deleted(self) -> None:
        # Incoming entries carry no manifest proof, so the name check is all that stands
        # between a hand-built ledger and deleting the destination or a sibling of it.
        victim = self.tmp / "victim"
        for name in (
            "",
            ".",
            "..",
            "../victim",
            str(victim),
            "a/b",
            "x\x00y",
            fx.TOO_LONG_NAME,
        ):
            with self.subTest(name=name):
                (self.destination / "a" / "b").mkdir(parents=True, exist_ok=True)
                victim.mkdir(exist_ok=True)
                (victim / "keep.txt").write_text("k", encoding="utf-8", newline="\n")
                ledger = Ledger((LedgerEntry(name, STATE_INCOMING, False, ""),))
                result = bp.prune(self.destination, ledger, 0)
                self.assertEqual((result.deleted, result.deferred), ((), (name,)))
                self.assertTrue((self.destination / "a" / "b").is_dir())
                self.assertTrue((victim / "keep.txt").is_file())
                self.assertTrue(os.path.isdir(self.tmp))


if __name__ == "__main__":
    unittest.main()
