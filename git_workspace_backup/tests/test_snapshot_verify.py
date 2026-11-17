#!/usr/bin/env python3
"""``--mode verify``'s judgment of one snapshot folder, on snapshots built by hand.

Each case builds a snapshot in its own temp destination, with a manifest written by the tool's
own writer and a ledger in a sandboxed state directory, then damages exactly one thing. Exit 0
needs both proofs: the manifest hashes to the ledger's entry, and every listed file to the
manifest. A missing ledger entry is never a pass, since then nothing vouches for the manifest.
"""

from __future__ import annotations

import errno
import io
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import _fixtures as fx  # noqa: F401 - installs the src/ bootstrap

import backup_fs
import backup_snapshot_verify as sv
from backup_checkout import Refused
from backup_fs import sha256_bytes
from backup_ledger import LEDGER_NAME, save_ledger, state_dir_for
from backup_manifest import MANIFEST_NAME
from backup_model import (
    STATE_INCOMING,
    STATE_PROMOTED,
    FailedItem,
    FileEntry,
    Ledger,
    LedgerEntry,
    Manifest,
)
from backup_seal import manifest_bytes

# mkfifo(3) is POSIX-only. The split is an ``if`` so a win32 type-check pass
# still sees the name; the win32 arm exists only for that pass.
if sys.platform == "win32":

    def _make_fifo(path: Path) -> None:  # pragma: only-on win32
        """Refuse: Windows has no FIFOs, and this tool never runs there."""
        raise OSError(errno.ENOSYS, "mkfifo needs POSIX, which Windows lacks")

else:  # pragma: only-on posix
    from os import mkfifo as _make_fifo

NAME = "2026-09-14T060000-fixture-mac"
#: The snapshot's files: a bundle in a subfolder exercises the nested names a manifest lists.
FILES = {
    "workspace.bundle": b"fixture bundle bytes",
    "extras.zip": b"fixture archive bytes",
    "RESTORE.md": b"# fixture restore document\n",
    "memory/fixture-slug.bundle": b"fixture memory bundle",
}
#: The three ways a snapshot comes to have no ledger entry, each named by the no-entry line.
NO_ENTRY_CAUSES = ("second machine", "restored machine", "pruned")


def _manifest(files: "dict[str, bytes]") -> Manifest:
    """A complete manifest listing *files* with their real hashes and sizes."""
    entries = tuple(
        FileEntry(name, sha256_bytes(data), len(data))
        for name, data in sorted(files.items())
    )
    return Manifest(
        created="2026-09-14T06:00:00+00:00",
        complete=True,
        bundles=(),
        files=entries,
        archive_members=(),
        dirty_worktrees=(),
        failed=(),
        skipped_symlinks=(),
        unclassified_ignored=(),
        machine="fixture-mac",
    )


class SnapshotCase(unittest.TestCase):
    """A snapshot folder in a temp destination, and its ledger entry in sandboxed state."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.destination = self.tmp / "dest"
        self.folder = self.destination / NAME
        self.base = self.tmp / "home" / ".workspace_backup"
        self.state = state_dir_for(self.base, self.destination)
        for name, data in FILES.items():
            path = self.folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.manifest = manifest_bytes(_manifest(FILES))
        (self.folder / MANIFEST_NAME).write_bytes(self.manifest)
        self.record_ledger(sha256_bytes(self.manifest))
        self.out = io.StringIO()
        self.err = io.StringIO()

    def record_ledger(
        self, manifest_sha256: str, name: str = NAME, state: str = STATE_PROMOTED
    ) -> None:
        """Make the sandboxed ledger hold one entry for *name*, promoted by default."""
        entry = LedgerEntry(name, state, True, manifest_sha256)
        save_ledger(self.state, Ledger((entry,)))

    def verify(
        self,
        snapshot: "Path | None" = None,
        *,
        hash_file: sv.HashFileFn = sv.hash_listed_file,
        read_manifest: sv.ReadManifestFn = sv.read_manifest_bounded,
    ) -> int:
        """Verify *snapshot* (default: the built one) with fresh output streams."""
        self.out, self.err = io.StringIO(), io.StringIO()
        request = sv.VerifyRequest(
            snapshot=self.folder if snapshot is None else snapshot,
            destination=self.destination,
            state_base=self.base,
        )
        seams = sv.VerifySeams(hash_file=hash_file, read_manifest=read_manifest)
        return sv.verify_snapshot_folder(
            request, out=self.out, err=self.err, seams=seams
        )

    @property
    def printed(self) -> str:
        """Everything the last verify printed, on either stream."""
        return self.out.getvalue() + self.err.getvalue()


class VerifiedTests(SnapshotCase):
    def test_a_snapshot_matching_its_manifest_and_the_ledger_is_verified(self) -> None:
        self.assertEqual(self.verify(), 0)
        self.assertEqual(
            self.out.getvalue(), f"verified: {NAME} ({len(FILES)} files)\n"
        )
        self.assertEqual(self.err.getvalue(), "")

    def test_a_ds_store_is_noted_and_does_not_fail(self) -> None:
        (self.folder / ".DS_Store").write_bytes(b"finder")
        (self.folder / "memory" / ".DS_Store").write_bytes(b"finder")
        self.assertEqual(self.verify(), 0)
        out = self.out.getvalue()
        self.assertIn(f"verified: {NAME}", out)
        self.assertIn(".DS_Store", out)
        self.assertIn("memory/.DS_Store", out)
        self.assertEqual(self.err.getvalue(), "")


class VerifiedDetailTests(SnapshotCase):
    def test_a_single_file_is_counted_in_the_singular(self) -> None:
        for name in [n for n in FILES if n != "RESTORE.md"]:
            (self.folder / name).unlink()
        (self.folder / "memory").rmdir()
        one = {"RESTORE.md": FILES["RESTORE.md"]}
        self.manifest = manifest_bytes(_manifest(one))
        (self.folder / MANIFEST_NAME).write_bytes(self.manifest)
        self.record_ledger(sha256_bytes(self.manifest))
        self.assertEqual(self.verify(), 0)
        self.assertEqual(self.out.getvalue(), f"verified: {NAME} (1 file)\n")

    def test_an_incomplete_snapshot_is_verified_with_a_note_saying_so(self) -> None:
        failed = (FailedItem("memory/other.bundle", "fixture failure"),)
        incomplete = replace(_manifest(FILES), complete=False, failed=failed)
        self.manifest = manifest_bytes(incomplete)
        (self.folder / MANIFEST_NAME).write_bytes(self.manifest)
        self.record_ledger(sha256_bytes(self.manifest))
        self.assertEqual(self.verify(), 0)
        out = self.out.getvalue()
        self.assertTrue(out.startswith(f"verified: {NAME}"), out)
        self.assertIn("incomplete", out)
        self.assertIn("1 failed item", out)


class MismatchTests(SnapshotCase):
    def assert_problem(self, fragment: str, name: str) -> None:
        """Exit 1, a line naming *name* with *fragment*, and no claim of verification."""
        self.assertEqual(self.verify(), 1)
        lines = [line for line in self.err.getvalue().splitlines() if name in line]
        self.assertTrue(any(fragment in line for line in lines), self.printed)
        self.assertNotIn("verified", self.printed)

    def test_a_file_with_the_same_size_but_other_bytes_fails(self) -> None:
        data = FILES["extras.zip"]
        (self.folder / "extras.zip").write_bytes(data[::-1])
        self.assert_problem("sha256", "extras.zip")

    def test_a_file_of_another_size_fails(self) -> None:
        (self.folder / "RESTORE.md").write_bytes(b"# a rewritten document, longer\n")
        self.assert_problem("size", "RESTORE.md")

    def test_a_tampered_manifest_fails_against_the_ledger(self) -> None:
        # A valid manifest still, so only the ledger can tell it was rewritten.
        tampered = dict(FILES, **{"RESTORE.md": b"# a planted document\n"})
        (self.folder / "RESTORE.md").write_bytes(tampered["RESTORE.md"])
        (self.folder / MANIFEST_NAME).write_bytes(manifest_bytes(_manifest(tampered)))
        self.assert_problem("ledger", MANIFEST_NAME)

    def test_a_missing_listed_file_fails(self) -> None:
        (self.folder / "workspace.bundle").unlink()
        self.assert_problem("missing", "workspace.bundle")

    def test_an_unlisted_file_fails(self) -> None:
        (self.folder / "planted.sh").write_bytes(b"echo planted")
        self.assert_problem("unlisted", "planted.sh")

    def test_an_unlisted_file_in_a_subfolder_fails(self) -> None:
        (self.folder / "memory" / "planted.bundle").write_bytes(b"planted")
        self.assert_problem("unlisted", "memory/planted.bundle")

    def test_an_unlisted_symlink_is_reported_and_not_followed(self) -> None:
        (self.folder / "planted-link").symlink_to(self.tmp)
        self.assert_problem("unlisted", "planted-link")

    def test_a_listed_file_replaced_by_a_symlink_to_the_same_bytes_fails(self) -> None:
        elsewhere = self.tmp / "elsewhere.zip"
        elsewhere.write_bytes(FILES["extras.zip"])
        (self.folder / "extras.zip").unlink()
        (self.folder / "extras.zip").symlink_to(elsewhere)
        self.assert_problem("not a regular file", "extras.zip")

    def test_a_listed_folder_replaced_by_a_symlink_is_not_followed(self) -> None:
        elsewhere = self.tmp / "elsewhere"
        (self.folder / "memory").rename(elsewhere)
        (self.folder / "memory").symlink_to(elsewhere)
        self.assert_problem("not a regular file", "memory/fixture-slug.bundle")

    def test_a_listed_fifo_fails_without_waiting_for_a_writer(self) -> None:
        (self.folder / "extras.zip").unlink()
        _make_fifo(self.folder / "extras.zip")
        self.assert_problem("not a regular file", "extras.zip")

    def test_a_definite_mismatch_outranks_a_file_that_cannot_be_judged(self) -> None:
        (self.folder / "extras.zip").write_bytes(b"other")

        def stalled_for_restore(root: Path, rel: str, size: int) -> "tuple[str, int]":
            if rel.endswith("RESTORE.md"):
                raise backup_fs.ReadStalled(f"{rel}: no data")
            return sv.hash_listed_file(root, rel, size)

        self.assertEqual(self.verify(hash_file=stalled_for_restore), 1)
        self.assertIn("extras.zip", self.err.getvalue())
        self.assertIn("RESTORE.md", self.err.getvalue())


class NoLedgerEntryTests(SnapshotCase):
    def assert_not_authenticated(self) -> None:
        """The unauthenticated line, naming each cause, and never the word "verified"."""
        printed = self.printed
        self.assertIn("NOT authenticated", printed)
        for cause in NO_ENTRY_CAUSES:
            self.assertIn(cause, printed)
        self.assertNotIn("verified", printed)

    def test_matching_files_without_a_ledger_entry_exit_1(self) -> None:
        (self.state / LEDGER_NAME).unlink()
        self.assertEqual(self.verify(), 1)
        self.assert_not_authenticated()
        self.assertIn("consistent with its own manifest", self.err.getvalue())
        self.assertEqual(self.out.getvalue(), "")

    def test_a_ledger_naming_only_other_snapshots_is_no_entry(self) -> None:
        self.record_ledger(sha256_bytes(self.manifest), name="2026-09-13T060000")
        self.assertEqual(self.verify(), 1)
        self.assert_not_authenticated()

    def test_an_entry_that_is_not_promoted_is_no_entry(self) -> None:
        self.record_ledger(sha256_bytes(self.manifest), state=STATE_INCOMING)
        self.assertEqual(self.verify(), 1)
        self.assert_not_authenticated()

    def test_a_tampered_file_without_a_ledger_entry_names_the_mismatch(self) -> None:
        (self.state / LEDGER_NAME).unlink()
        (self.folder / "workspace.bundle").write_bytes(b"planted bundle bytes")
        self.assertEqual(self.verify(), 1)
        self.assert_not_authenticated()
        self.assertIn("workspace.bundle", self.err.getvalue())
        self.assertNotIn("consistent", self.printed)

    def test_an_unjudged_file_without_a_ledger_entry_exits_2(self) -> None:
        (self.state / LEDGER_NAME).unlink()

        def stalled(root: Path, rel: str, size: int) -> "tuple[str, int]":
            raise backup_fs.ReadStalled(f"{rel}: no data")

        self.assertEqual(self.verify(hash_file=stalled), 2)
        self.assert_not_authenticated()


class UnjudgedTests(SnapshotCase):
    def assert_unjudged(self, fragment: str, snapshot: "Path | None" = None) -> None:
        """Exit 2 with *fragment* on stderr, nothing on stdout, and no verification claim."""
        self.assertEqual(self.verify(snapshot), 2)
        self.assertIn(fragment, self.err.getvalue())
        self.assertEqual(self.out.getvalue(), "")
        self.assertNotIn("verified", self.printed)

    def test_a_symlinked_snapshot_folder_is_not_judged(self) -> None:
        real = self.tmp / "real-snapshot"
        self.folder.rename(real)
        self.folder.symlink_to(real)
        self.assert_unjudged("symlink")

    def test_a_folder_outside_the_destination_is_not_judged(self) -> None:
        elsewhere = self.tmp / "elsewhere" / NAME
        elsewhere.parent.mkdir()
        self.folder.rename(elsewhere)
        self.assert_unjudged("not directly in the destination", elsewhere)

    def test_a_folder_nested_deeper_in_the_destination_is_not_judged(self) -> None:
        nested = self.destination / "nested" / NAME
        nested.parent.mkdir()
        self.folder.rename(nested)
        self.assert_unjudged("not directly in the destination", nested)

    def test_a_folder_whose_name_is_not_a_snapshot_name_is_not_judged(self) -> None:
        renamed = self.destination / "not-a-snapshot"
        self.folder.rename(renamed)
        self.assert_unjudged("not a snapshot folder name", renamed)

    def test_a_missing_folder_is_not_judged(self) -> None:
        self.assert_unjudged(
            "no snapshot folder", self.destination / "2026-09-15T060000"
        )

    def test_a_folder_that_cannot_be_examined_is_not_judged(self) -> None:
        # Without search permission on the destination, its entries cannot be examined.
        self.destination.chmod(0o600)
        self.addCleanup(self.destination.chmod, 0o700)
        self.assert_unjudged("cannot be examined")

    def test_a_file_where_the_folder_should_be_is_not_judged(self) -> None:
        planted = self.destination / "2026-09-15T060000"
        planted.write_bytes(b"not a folder")
        self.assert_unjudged("not a directory", planted)

    def test_a_malformed_manifest_is_not_judged(self) -> None:
        (self.folder / MANIFEST_NAME).write_bytes(b"{not json")
        self.assert_unjudged("not a manifest this tool wrote")

    def test_a_missing_manifest_is_not_judged(self) -> None:
        (self.folder / MANIFEST_NAME).unlink()
        self.assert_unjudged(MANIFEST_NAME)

    def test_a_symlinked_manifest_is_not_followed(self) -> None:
        copy = self.tmp / "manifest-copy.json"
        copy.write_bytes(self.manifest)
        (self.folder / MANIFEST_NAME).unlink()
        (self.folder / MANIFEST_NAME).symlink_to(copy)
        self.assert_unjudged(MANIFEST_NAME)

    def test_a_ledger_this_tool_did_not_write_is_not_judged(self) -> None:
        (self.state / LEDGER_NAME).write_bytes(b"{}")
        self.assert_unjudged("ledger")

    def test_an_unreadable_ledger_is_not_judged(self) -> None:
        (self.state / LEDGER_NAME).unlink()
        (self.state / LEDGER_NAME).mkdir()
        self.assert_unjudged("ledger")

    def test_an_unreadable_file_is_not_judged_and_is_named(self) -> None:
        target = self.folder / "extras.zip"
        target.chmod(0)
        self.addCleanup(target.chmod, 0o600)
        self.assert_unjudged("extras.zip")
        self.assertIn("could not be read", self.err.getvalue())

    def test_an_unlistable_subfolder_is_not_judged(self) -> None:
        sub = self.folder / "memory"
        sub.chmod(0)
        self.addCleanup(sub.chmod, 0o700)
        self.assert_unjudged("memory")

    def test_a_read_that_stalls_is_not_judged_and_is_named(self) -> None:
        def stalled_for_bundle(root: Path, rel: str, size: int) -> "tuple[str, int]":
            if rel.endswith("workspace.bundle"):
                raise backup_fs.ReadStalled(f"{rel}: no data within the bound")
            return sv.hash_listed_file(root, rel, size)

        self.assertEqual(self.verify(hash_file=stalled_for_bundle), 2)
        self.assertIn("workspace.bundle", self.err.getvalue())
        self.assertIn("iCloud", self.err.getvalue())
        self.assertNotIn("verified", self.printed)

    def test_a_dataless_file_is_reported_offloaded_at_once(self) -> None:
        def dataless_bundle(root: Path, rel: str, size: int) -> "tuple[str, int]":
            if rel.endswith("workspace.bundle"):
                raise backup_fs.FileOffloaded(f"{rel} is dataless")
            return sv.hash_listed_file(root, rel, size)

        self.assertEqual(self.verify(hash_file=dataless_bundle), 2)
        self.assertIn("offloaded: workspace.bundle", self.err.getvalue())
        self.assertNotIn("verified", self.printed)

    def test_a_manifest_read_that_times_out_is_not_judged(self) -> None:
        self.assertEqual(self.verify(read_manifest=lambda destination, name: None), 2)
        self.assertIn(MANIFEST_NAME, self.err.getvalue())

    def test_an_offloaded_placeholder_is_not_judged_and_not_unlisted(self) -> None:
        # An offloaded file in older iCloud Drive is renamed to a hidden ".<name>.icloud".
        (self.folder / "extras.zip").rename(self.folder / ".extras.zip.icloud")
        self.assertEqual(self.verify(), 2)
        err = self.err.getvalue()
        self.assertIn("extras.zip", err)
        self.assertIn("iCloud", err)
        self.assertNotIn("unlisted", err)


class SnapshotPathTests(unittest.TestCase):
    def test_an_absolute_path_is_kept(self) -> None:
        home = Path("/opt/fixture-home")
        self.assertEqual(
            sv.snapshot_path("/opt/fixture-dest/x", home), Path("/opt/fixture-dest/x")
        )

    def test_a_home_path_is_expanded_against_the_given_home(self) -> None:
        home = Path("/opt/fixture-home")
        self.assertEqual(sv.snapshot_path("~/dest/x", home), home / "dest" / "x")

    def test_a_relative_path_is_refused(self) -> None:
        with self.assertRaises(Refused) as caught:
            sv.snapshot_path("dest/x", Path("/opt/fixture-home"))
        self.assertIn("--snapshot", str(caught.exception))

    def test_no_snapshot_is_refused(self) -> None:
        with self.assertRaises(Refused) as caught:
            sv.snapshot_path(None, Path("/opt/fixture-home"))
        self.assertIn("--snapshot", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
