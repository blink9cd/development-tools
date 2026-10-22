#!/usr/bin/env python3
"""Verification never passes what it did not prove, and leaves its scratch as it found it.

The restore runs bounded git against a mirror it made itself, in a directory of its own
under the caller's scratch root, and removes that directory whatever the verdict. A name
read from a manifest -- a bundle's, a worktree head's, a snapshot file's -- is checked before
it becomes a path or a refspec, because a glob in a refspec or a ``..`` in a path would
turn a check into a pass it never earned.
"""

from __future__ import annotations

import errno
import struct
import tempfile
import unittest
import zipfile
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest import mock

import _fixtures as fx

import backup_bundle as bb
import backup_proc
import backup_verify as bv
from backup_fs import sha256_bytes
from backup_model import BundleEntry, FailedItem, FileEntry, StashEntry, WorktreeHead

_BUNDLE = "repo.bundle"
#: Central-directory header signature, and where its flags and method fields sit.
_CENTRAL_HEADER = b"PK\x01\x02"
_FLAGS_OFFSET = 8
_METHOD_OFFSET = 10
_ENCRYPTED_FLAG = 0x1
_UNSUPPORTED_METHOD = 99
#: A local file header is 30 fixed bytes; its name and extra-field lengths sit at 26 and 28.
_LOCAL_HEADER_SIZE = 30
_LOCAL_NAME_LENGTHS_OFFSET = 26
#: A deflate block header whose type bits are 11, a type zlib refuses.
_INVALID_DEFLATE_BLOCK = 0xFF
#: A member name zipfile flags as UTF-8, and two bytes no UTF-8 decoder accepts, as long as
#: its last two encoded bytes so the corrupted archive keeps every recorded length.
_UTF8_FLAGGED_NAME = "home/é"
_INVALID_UTF8 = b"\xff\xfe"


class _Recorder:
    """A RunCmd that records every call, then delegates to *inner*."""

    def __init__(self, inner: backup_proc.RunCmd = backup_proc.run_cmd) -> None:
        self.inner = inner
        self.calls: list[tuple[list[str], "Path | None", float]] = []

    def __call__(
        self, argv: Sequence[str], cwd: "Path | None", timeout: float
    ) -> "tuple[int, str, str]":
        self.calls.append((list(argv), cwd, timeout))
        return self.inner(argv, cwd, timeout)


def _answering(code: int) -> backup_proc.RunCmd:
    """A runner that answers every command with exit *code* and no output at all."""

    def run(
        argv: Sequence[str], cwd: "Path | None", timeout: float
    ) -> "tuple[int, str, str]":
        return code, "", ""

    return run


class _Scratch(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(fx.isolated_git_env())
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.scratch = self.tmp / "scratch"
        self.scratch.mkdir()


class RestoreGuardTests(_Scratch):
    def setUp(self) -> None:
        super().setUp()
        self.repo = fx.make_repo(self.tmp)
        self._add_detached_worktree("wt")
        self.snapshot = self.tmp / "snapshot"
        self.entry = self._bundle(self.repo, _BUNDLE)

    def _add_detached_worktree(self, name: str) -> None:
        worktree = str(self.tmp / name)
        fx.git(self.repo, "worktree", "add", "--quiet", "--detach", worktree)
        fx.add_commit(self.tmp / name, f"only on {name}")

    def _bundle(self, repo: Path, rel_name: str) -> BundleEntry:
        result = bb.create_bundle(
            repo,
            self.snapshot,
            rel_name,
            label=rel_name,
            require_refs=True,
            run=backup_proc.run_cmd,
        )
        assert result.entry is not None, result.failure
        return result.entry

    def _verify(
        self, run: backup_proc.RunCmd, entry: "BundleEntry | None" = None
    ) -> "FailedItem | None":
        chosen = self.entry if entry is None else entry
        return bv.verify_bundle(self.snapshot, chosen, self.scratch, run=run)

    def _scratch_names(self) -> list[str]:
        return sorted(path.name for path in self.scratch.iterdir())

    def test_the_restore_runs_bounded_git_against_the_mirror_it_made(self) -> None:
        recorder = _Recorder()
        self.assertIsNone(self._verify(recorder))
        bundle = str(self.snapshot / _BUNDLE)
        mirror = recorder.calls[0][0][-1]
        workdir = Path(mirror).parent
        git_dir = f"--git-dir={mirror}"
        restored = "refs/backup/worktrees/wt/HEAD"
        resolve = ["git", git_dir, "rev-parse", "--verify", "--end-of-options"]
        self.assertEqual(
            [argv for argv, _, _ in recorder.calls],
            [
                ["git", "clone", "--quiet", "--mirror", "--", bundle, mirror],
                ["git", git_dir, "fsck", "--full"],
                ["git", git_dir, "for-each-ref", "--format=%(refname)", "refs/"],
                [*resolve, "HEAD^{commit}"],
                [
                    "git",
                    git_dir,
                    "fetch",
                    "--quiet",
                    "--",
                    bundle,
                    f"worktrees/wt/HEAD:{restored}",
                ],
                [*resolve, f"{restored}^{{commit}}"],
            ],
        )
        self.assertEqual(workdir.parent, self.scratch)
        self.assertEqual({cwd for _, cwd, _ in recorder.calls}, {workdir})
        self.assertEqual(
            {timeout for _, _, timeout in recorder.calls}, {backup_proc.GIT_TIMEOUT_S}
        )
        self.assertEqual(self._scratch_names(), [])

    def test_a_failed_restore_leaves_nothing_behind_and_reuses_nothing(self) -> None:
        # A leftover where a mirror named after the bundle would go: never reused, and
        # never removed either, since this verification did not create it.
        stale = self.scratch / f"{_BUNDLE}.mirror"
        stale.mkdir()
        (stale / "HEAD").write_text(
            "stale half-clone\n", encoding="utf-8", newline="\n"
        )
        failure = self._verify(fx.failing_when("fsck"))
        assert failure is not None
        self.assertIn("fsck failed", failure.reason)
        self.assertEqual(self._scratch_names(), [stale.name])
        self.assertIsNone(self._verify(backup_proc.run_cmd))
        self.assertEqual(self._scratch_names(), [stale.name])
        self.assertEqual(
            (stale / "HEAD").read_text(encoding="utf-8"), "stale half-clone\n"
        )

    def test_a_timeout_a_missing_git_or_an_empty_restore_is_never_a_pass(self) -> None:
        for code in (backup_proc.TIMEOUT_RC, backup_proc.NOT_STARTED_RC):
            with self.subTest(code=code):
                failure = self._verify(_answering(code))
                assert failure is not None
                self.assertIn(f"exit {code}", failure.reason)
        # Every step "succeeds" and prints nothing: a restore holding no refs is not one,
        # even for an entry that claims to need none.
        for refs in (self.entry.refs, ()):
            with self.subTest(refs=refs):
                failure = self._verify(
                    _answering(0), replace(self.entry, refs=refs, worktree_heads=())
                )
                assert failure is not None
                self.assertIn("lists no refs", failure.reason)
        self.assertEqual(self._scratch_names(), [])

    def test_a_recorded_sha_the_restore_does_not_hold_fails(self) -> None:
        # Both recorded shas are real commits in the bundle, just not the right ones: a
        # restore anchors an unreachable head by its recorded sha, so it must be proved.
        head_sha = self.entry.head
        wt_sha = self.entry.worktree_heads[0].sha
        cases = (
            (
                replace(
                    self.entry, worktree_heads=(WorktreeHead("wt", head_sha, False),)
                ),
                f"worktree head wt restored as {wt_sha}, recorded {head_sha}",
            ),
            (
                replace(self.entry, head=wt_sha),
                f"HEAD restored as {head_sha}, recorded {wt_sha}",
            ),
        )
        for entry, expected in cases:
            with self.subTest(expected):
                failure = self._verify(backup_proc.run_cmd, entry)
                assert failure is not None
                self.assertIn(expected, failure.reason)

    def test_a_recorded_stash_entry_the_mirror_does_not_hold_fails(self) -> None:
        absent = "b" * 40
        cases = (
            (StashEntry(absent, "x"), "rev-parse of stash entry " + absent),
            (StashEntry("--all", "x"), "stash entry '--all' is not a full object id"),
        )
        for stash, expected in cases:
            with self.subTest(expected):
                entry = replace(self.entry, stashes=(stash,))
                failure = self._verify(backup_proc.run_cmd, entry)
                assert failure is not None
                self.assertIn(expected, failure.reason)

    def test_every_recorded_stash_entry_verifies(self) -> None:
        for index in range(2):
            (self.repo / "README.md").write_text(
                f"stashed {index}\n", encoding="utf-8", newline="\n"
            )
            fx.git(self.repo, "stash", "--quiet")
        entry = self._bundle(self.repo, "stashes.bundle")
        self.assertEqual(len(entry.stashes), 2)
        self.assertIsNone(self._verify(backup_proc.run_cmd, entry))

    def test_a_restored_ref_that_cannot_be_resolved_fails(self) -> None:
        failure = self._verify(fx.failing_when("rev-parse"))
        assert failure is not None
        self.assertIn("rev-parse of HEAD failed (exit 1)", failure.reason)

    def test_a_bundle_that_recorded_no_head_verifies_without_one(self) -> None:
        # An unborn HEAD beside a real branch: the bundle records no HEAD, the mirror's HEAD
        # resolves to nothing, and there is no recorded head for it to contradict.
        unborn = fx.make_repo(self.tmp, name="unborn")
        fx.git(unborn, "checkout", "--quiet", "--orphan", "fresh")
        entry = self._bundle(unborn, "unborn.bundle")
        self.assertEqual(entry.head, "")
        self.assertIsNone(self._verify(backup_proc.run_cmd, entry))

    def test_a_worktree_whose_name_ends_in_a_dot_verifies(self) -> None:
        # A ref name may not END in a dot, so the name must sit mid-refname in the restore.
        self._add_detached_worktree("x.")
        entry = self._bundle(self.repo, "dot.bundle")
        self.assertIn("x.", [head.name for head in entry.worktree_heads])
        self.assertIsNone(self._verify(backup_proc.run_cmd, entry))

    def test_a_worktree_head_name_git_could_not_have_written_is_refused(self) -> None:
        sha = self.entry.worktree_heads[0].sha
        # "*" is the one that matters most: as a refspec it is a glob, and the fetch
        # passes by restoring whichever worktree heads the bundle happens to hold.
        names: tuple[str, ...] = (
            "*",
            "a:b",
            "a b",
            "a/b",
            "",
            "..",
            "a..b",
            ".hidden",
            "x.lock",
        )
        names += ("a@{1}", "a\\b", "a?", "a[", "a~1", "a^", "tab\t", "del\x7f")
        for name in names:
            with self.subTest(name=name):
                recorder = _Recorder()
                heads = (WorktreeHead(name, sha, False),)
                failure = self._verify(
                    recorder, replace(self.entry, worktree_heads=heads)
                )
                assert failure is not None
                self.assertIn("not a single ref component", failure.reason)
                self.assertNotIn("fetch", [argv[2] for argv, _, _ in recorder.calls])

    def test_an_unsafe_bundle_name_is_refused_before_any_command(self) -> None:
        # Each of these, joined onto the snapshot, reaches the real bundle.
        for name in (
            f"../snapshot/{_BUNDLE}",
            str(self.snapshot / _BUNDLE),
            f"{_BUNDLE}/.",
        ):
            with self.subTest(name=name):
                recorder = _Recorder()
                failure = self._verify(recorder, replace(self.entry, name=name))
                assert failure is not None
                self.assertIn("unsafe bundle name", failure.reason)
                self.assertEqual(recorder.calls, [])

    def test_a_scratch_root_that_does_not_exist_is_a_failure(self) -> None:
        recorder = _Recorder()
        absent = self.tmp / "absent"
        failure = bv.verify_bundle(self.snapshot, self.entry, absent, run=recorder)
        assert failure is not None
        self.assertIn("scratch", failure.reason)
        self.assertEqual(recorder.calls, [])
        self.assertFalse(absent.exists())

    def test_a_cleanup_failure_is_reported_beside_the_verdict(self) -> None:
        busy = OSError(errno.EBUSY, "fixture busy")
        with mock.patch.object(bv.shutil, "rmtree", side_effect=busy):
            passed = self._verify(backup_proc.run_cmd)
            failed = self._verify(fx.failing_when("fsck"))
        assert passed is not None and failed is not None
        self.assertIn("scratch cleanup failed", passed.reason)
        self.assertIn("fsck failed", failed.reason)
        self.assertIn("scratch cleanup failed", failed.reason)


class DamagedArchiveTests(_Scratch):
    def _zip(self, name: str, data: bytes) -> tuple[Path, bytearray]:
        path = self.tmp / name
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("home/x", data)
        return path, bytearray(path.read_bytes())

    def test_each_damaged_archive_shape_is_unreadable_not_a_crash(self) -> None:
        path, method = self._zip("method.zip", b"fixture" * 800)
        central = method.index(_CENTRAL_HEADER)
        struct.pack_into("<H", method, central + _METHOD_OFFSET, _UNSUPPORTED_METHOD)
        _, encrypted = self._zip("encrypted.zip", b"fixture" * 800)
        flags_at = encrypted.index(_CENTRAL_HEADER) + _FLAGS_OFFSET
        (flags,) = struct.unpack_from("<H", encrypted, flags_at)
        struct.pack_into("<H", encrypted, flags_at, flags | _ENCRYPTED_FLAG)
        _, stream = self._zip("stream.zip", b"fixture" * 800)
        name_length, extra_length = struct.unpack_from(
            "<HH", stream, _LOCAL_NAME_LENGTHS_OFFSET
        )
        stream[_LOCAL_HEADER_SIZE + name_length + extra_length] = _INVALID_DEFLATE_BLOCK
        for label, data in (
            ("unsupported method", method),
            ("encrypted flag", encrypted),
            ("corrupt deflate stream", stream),
        ):
            with self.subTest(label):
                path.write_bytes(bytes(data))
                failure = bv.verify_archive(path, ())
                assert failure is not None
                self.assertIn("unreadable archive", failure.reason)

    def test_a_corrupt_utf8_name_or_a_stream_cut_short_is_unreadable_not_a_crash(
        self,
    ) -> None:
        # A non-ASCII name sets the UTF-8 flag, so zipfile decodes the name strictly: invalid
        # bytes there raise UnicodeDecodeError, a ValueError, while the archive is opened.
        path = self.tmp / "name.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(_UTF8_FLAGGED_NAME, b"fixture")
        intact = path.read_bytes()
        encoded = _UTF8_FLAGGED_NAME.encode("utf-8")
        invalid = encoded[: -len(_INVALID_UTF8)] + _INVALID_UTF8
        self.assertIn(encoded, intact)
        cut_short = mock.patch.object(
            zipfile.ZipFile, "testzip", side_effect=EOFError("fixture cut short")
        )
        cases = (
            ("invalid UTF-8 in a flagged name", intact.replace(encoded, invalid)),
            ("a member stream cut short", intact),
        )
        for label, damaged in cases:
            with self.subTest(label):
                path.write_bytes(damaged)
                stream_ends = cut_short if damaged == intact else nullcontext()
                with stream_ends:
                    failure = bv.verify_archive(path, ())
                assert failure is not None
                self.assertIn("unreadable archive", failure.reason)


class SnapshotHashTests(_Scratch):
    def setUp(self) -> None:
        super().setUp()
        self.snapshot = self.tmp / "snap"
        (self.snapshot / "sub").mkdir(parents=True)
        (self.snapshot / "sub" / "a.bin").write_bytes(b"a")
        self.outside = self.tmp / "outside.bin"
        self.outside.write_bytes(b"outside")

    def test_hashing_refuses_unsafe_names_without_reading_them(self) -> None:
        # Each unsafe name but the empty one, joined onto the snapshot, reaches a real file.
        unsafe = ["../outside.bin", str(self.outside), "./sub/a.bin", "sub/a.bin/.", ""]
        names = [*unsafe, "missing.bin", "sub/a.bin"]
        hashed = bv.hash_named_files(self.snapshot, names)
        self.assertEqual(hashed.files, (FileEntry("sub/a.bin", sha256_bytes(b"a"), 1),))
        reasons = [(failure.item, failure.reason) for failure in hashed.failed]
        self.assertEqual(
            [item for item, _ in reasons],
            [f"hash:{name}" for name in [*unsafe, "missing.bin"]],
        )
        for _, reason in reasons[:-1]:
            self.assertIn("unsafe file name", reason)
        self.assertIn("unreadable", reasons[-1][1])

    def test_links_and_unlistable_directories_are_recorded_never_followed(self) -> None:
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "b.bin").write_bytes(b"b")
        (self.snapshot / "link.bin").symlink_to(self.outside)
        (self.snapshot / "dirlink").symlink_to(elsewhere, target_is_directory=True)
        locked = self.snapshot / "locked"
        locked.mkdir()
        (locked / "c.bin").write_bytes(b"c")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        hashed = bv.hash_snapshot_files(self.snapshot, exclude="MANIFEST.json")
        self.assertEqual([entry.name for entry in hashed.files], ["sub/a.bin"])
        reasons = {failure.item: failure.reason for failure in hashed.failed}
        self.assertEqual(
            sorted(reasons), ["hash:dirlink", "hash:link.bin", "hash:locked"]
        )
        self.assertIn("not a regular file", reasons["hash:dirlink"])
        self.assertIn("not a regular file", reasons["hash:link.bin"])
        self.assertIn("unlistable directory", reasons["hash:locked"])
        absent = bv.hash_snapshot_files(self.tmp / "absent", exclude="MANIFEST.json")
        self.assertEqual(absent.files, ())
        self.assertEqual([f.item for f in absent.failed], ["hash:."])


if __name__ == "__main__":
    unittest.main()
