#!/usr/bin/env python3
"""``--mode verify`` against a hostile snapshot folder: names, sizes and leftovers.

Each case builds the same hand-made snapshot ``test_snapshot_verify`` builds, then plants one
thing a verify must survive: a name holding terminal control characters (in the manifest or
on disk), a manifest name no filesystem can encode, a listed file swapped for a far larger
one, an iCloud placeholder beside a file that is present, or an unlisted folder.
"""

from __future__ import annotations

import os
import unittest
from dataclasses import replace
from pathlib import Path

import _fixtures as fx  # noqa: F401 - installs the src/ bootstrap
import test_snapshot_verify as base

import backup_fs
import backup_json
from backup_fs import sha256_bytes
from backup_manifest import MANIFEST_NAME
from backup_model import FileEntry
from backup_seal import manifest_bytes
from backup_snapshot_verify import hash_listed_file as sv_hash

#: A name that, printed raw, ends the line it is on, erases what a terminal showed of it
#: (ESC [2K, then a carriage return), and starts a forged success line of its own.
HOSTILE = "planted\x1b[2K\r\nverified: forged (4 files)\x07"
#: Every code point a terminal acts on that the hostile names above carry.
_CONTROLS = ("\x1b", "\r", "\x07")
#: The size of the sparse file swapped in for a listed one: far beyond what any read should
#: touch once the size is known to differ.
_SPARSE_SIZE = 1 << 30


class HostileNameTests(base.SnapshotCase):
    def assert_no_forged_line(self) -> None:
        """Nothing printed holds a raw control character or a line opening ``verified:``."""
        printed = self.printed
        for control in _CONTROLS:
            self.assertNotIn(control, printed)
        # Every printed line ends in the one newline print adds; none is inside a name.
        lines = printed.split("\n")
        self.assertFalse(
            [line for line in lines if line.startswith("verified:")], printed
        )

    def test_a_control_character_manifest_name_is_printed_escaped(self) -> None:
        self.manifest = _manifest_listing_also(HOSTILE)
        (self.folder / MANIFEST_NAME).write_bytes(self.manifest)
        self.record_ledger(sha256_bytes(self.manifest))
        self.assertEqual(self.verify(), 1)
        self.assertIn("missing: planted\\x1b[2K\\x0d\\x0averified:", self.printed)
        self.assert_no_forged_line()

    def test_a_control_character_name_on_disk_is_printed_escaped(self) -> None:
        (self.folder / HOSTILE).write_bytes(b"planted")
        self.assertEqual(self.verify(), 1)
        self.assertIn("unlisted: planted\\x1b[2K", self.printed)
        self.assert_no_forged_line()

    def test_an_unjudged_folder_name_is_printed_escaped(self) -> None:
        self.assertEqual(self.verify(self.destination / HOSTILE), 2)
        self.assertIn("planted\\x1b[2K", self.err.getvalue())
        self.assert_no_forged_line()


def _manifest_listing_also(name: str) -> bytes:
    """The fixture manifest's bytes, listing *name* (empty, never written) beside its files."""
    manifest = base._manifest(base.FILES)
    listed = (*manifest.files, FileEntry(name, "0" * 64, 0))
    return manifest_bytes(replace(manifest, files=listed))


class UnencodableNameTests(base.SnapshotCase):
    def test_a_lone_surrogate_manifest_name_is_not_judged_without_a_traceback(
        self,
    ) -> None:
        marker = "surrogate-marker"
        data = _manifest_listing_also(marker)
        # JSON may spell a lone surrogate as an escape; no filesystem name can hold one.
        self.manifest = data.replace(marker.encode(), b"\\ud800")
        (self.folder / MANIFEST_NAME).write_bytes(self.manifest)
        self.record_ledger(sha256_bytes(self.manifest))
        self.assertEqual(self.verify(), 2)
        self.assertIn("not a manifest this tool wrote", self.err.getvalue())

    def test_relative_posix_name_refuses_a_lone_surrogate(self) -> None:
        with self.assertRaises(backup_json.ShapeError):
            backup_json.relative_posix_name({"name": "a/\ud800"}, "name")

    def test_relative_posix_name_keeps_a_name_decoded_from_undecodable_bytes(
        self,
    ) -> None:
        # os.fsdecode spells a byte that is not UTF-8 as a surrogate it encodes back.
        name = os.fsdecode(b"memory/\xff.bundle")
        self.assertEqual(backup_json.relative_posix_name({"name": name}, "name"), name)


class SizeFirstTests(base.SnapshotCase):
    def test_a_listed_file_of_another_size_is_reported_before_any_read(self) -> None:
        target = self.folder / "extras.zip"
        target.unlink()
        with target.open("wb") as handle:
            handle.truncate(_SPARSE_SIZE)
        reads: "list[int]" = []

        def recording_read(fd: int, count: int) -> bytes:
            reads.append(count)
            return os.read(fd, count)

        def hash_extras(root: Path, rel: str, size: int) -> "tuple[str, int]":
            read = recording_read if rel.endswith("extras.zip") else os.read
            return backup_fs.hash_regular_file_bounded(
                root, rel, 30.0, expected_size=size, read=read
            )

        self.assertEqual(self.verify(hash_file=hash_extras), 1)
        self.assertIn(
            f"changed: extras.zip holds {_SPARSE_SIZE} bytes, the manifest lists size "
            f"{len(base.FILES['extras.zip'])}",
            self.err.getvalue(),
        )
        self.assertEqual(reads, [])

    def test_a_file_that_grows_while_it_is_read_is_still_caught(self) -> None:
        # The size matched when the file was opened; the bytes read did not stop there.
        def grown(root: Path, rel: str, size: int) -> "tuple[str, int]":
            digest, read = sv_hash(root, rel, size)
            return digest, read + 1 if rel.endswith("RESTORE.md") else read

        self.assertEqual(self.verify(hash_file=grown), 1)
        self.assertIn(
            f"changed: RESTORE.md holds {len(base.FILES['RESTORE.md']) + 1} bytes",
            self.err.getvalue(),
        )

    def test_the_real_seam_reports_a_swapped_sparse_file_by_its_size(self) -> None:
        target = self.folder / "workspace.bundle"
        target.unlink()
        with target.open("wb") as handle:
            handle.truncate(_SPARSE_SIZE)
        self.assertEqual(self.verify(), 1)
        self.assertIn(f"workspace.bundle holds {_SPARSE_SIZE} bytes", self.printed)

    def test_a_matching_size_is_still_hashed(self) -> None:
        digest, size = backup_fs.hash_regular_file_bounded(
            self.folder,
            "extras.zip",
            30.0,
            expected_size=len(base.FILES["extras.zip"]),
        )
        self.assertEqual(size, len(base.FILES["extras.zip"]))
        self.assertEqual(digest, sha256_bytes(base.FILES["extras.zip"]))


class LeftoverTests(base.SnapshotCase):
    def test_a_placeholder_beside_a_present_file_is_unlisted(self) -> None:
        (self.folder / ".extras.zip.icloud").write_bytes(b"planted placeholder")
        self.assertEqual(self.verify(), 1)
        self.assertIn("unlisted: .extras.zip.icloud", self.err.getvalue())

    def test_an_unlisted_empty_folder_is_reported(self) -> None:
        (self.folder / "planted").mkdir()
        self.assertEqual(self.verify(), 1)
        self.assertIn("unlisted: planted/", self.err.getvalue())

    def test_an_unlisted_folder_is_reported_once_and_not_entered(self) -> None:
        (self.folder / "planted" / "deeper").mkdir(parents=True)
        (self.folder / "planted" / "deeper" / "file").write_bytes(b"planted")
        self.assertEqual(self.verify(), 1)
        unlisted = [
            line
            for line in self.err.getvalue().splitlines()
            if line.startswith("unlisted:")
        ]
        self.assertEqual(unlisted, ["unlisted: planted/"])

    def test_an_unlisted_folder_beside_listed_ones_is_reported(self) -> None:
        (self.folder / "memory" / "planted").mkdir()
        self.assertEqual(self.verify(), 1)
        self.assertIn("unlisted: memory/planted/", self.err.getvalue())


if __name__ == "__main__":
    unittest.main()
