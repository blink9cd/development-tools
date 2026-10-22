#!/usr/bin/env python3
"""The zip holds exactly what drift calls covered, under names that stay inside their prefix."""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import _fixtures as fx  # noqa: F401 - installs the src/ bootstrap

import backup_drift as bd
import backup_zip as bz
from backup_fs import sha256_bytes

#: How long a reader test waits before calling a read blocked. A refused FIFO returns at
#: once; a blocked open never returns, so any generous bound tells the two apart.
_BLOCKED_AFTER_S = 10.0
#: The zip format's first and last member times as read back. A zip stores seconds in
#: two-second steps, so the last representable time, 23:59:59, reads back as 23:59:58.
_ZIP_FIRST = (1980, 1, 1, 0, 0, 0)
_ZIP_LAST = (2107, 12, 31, 23, 59, 58)
#: An mtime past the zip format's last year: 2200-01-01 UTC.
_FAR_FUTURE_S = 7258118400.0
#: A zip member keeps the Unix mode in the high 16 bits of its external attributes.
_MODE_SHIFT = 16
_MODE_MASK = 0xFFFF
_UNREADABLE_DIRECTORY = "unreadable directory: "
#: Larger than any plausible single read chunk, so a read that stops after one is caught.
_LARGER_THAN_ANY_READ_CHUNK = (1 << 20) + 1
#: Width of the counter hashed into each block of non-repeating test content.
_COUNTER_BYTES = 8

# mkfifo(3) is POSIX-only. The split is an ``if`` so a win32 type-check pass
# still sees the name; the win32 arm exists only for that pass.
if sys.platform == "win32":

    def _make_fifo(path: Path) -> None:  # pragma: only-on win32
        """Refuse: Windows has no FIFOs, and this tool never runs there."""
        raise OSError(errno.ENOSYS, "mkfifo needs POSIX, which Windows lacks")

else:  # pragma: only-on posix
    _make_fifo = os.mkfifo


def _regular_files(root: Path) -> set[str]:
    """Every regular file under *root*, relative and POSIX, found without following links.

    The cross-check's own listing, independent of the walk it checks.
    """
    found: set[str] = set()
    for dirpath, _, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            path = Path(dirpath) / name
            if stat.S_ISREG(os.lstat(path).st_mode):
                found.add(path.relative_to(root).as_posix())
    return found


def _relative(name: str) -> str:
    """A member or failure name without its prefix component."""
    return name.split("/", 1)[1]


def _non_repeating_bytes(size: int) -> bytes:
    """*size* bytes with no repeated block: the sha256 digests of a running counter."""
    digest_size = hashlib.sha256().digest_size
    blocks = (
        hashlib.sha256(index.to_bytes(_COUNTER_BYTES, "big")).digest()
        for index in range(size // digest_size + 1)
    )
    return b"".join(blocks)[:size]


class ZipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.workspace = self.tmp / "workspace"
        (self.workspace / ".superpowers" / "sdd").mkdir(parents=True)
        self._file(self.workspace / ".superpowers" / "sdd" / "report.md", "r")
        self._file(self.workspace / ".superpowers" / "top.md", "t")
        (self.workspace / "src").mkdir()
        self._file(self.workspace / "src" / "__mock.html", "m")
        self._file(self.workspace / "src" / "keep.py", "k")
        self.home = self.tmp / "home"
        self.home.mkdir()
        self._file(self.home / ".zshrc", "z")
        self.out = self.tmp / "out" / "extras.zip"

    @staticmethod
    def _file(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    def _lock(self, directory: Path) -> None:
        """Make *directory* unlistable for this test, restoring it before cleanup."""
        directory.chmod(0)
        self.addCleanup(directory.chmod, 0o700)

    def _write(self, *sources: bz.ZipSource) -> bz.ZipResult:
        return bz.write_zip(self.out, sources)

    def _assert_archive_holds_exactly(self, result: bz.ZipResult) -> None:
        """The archive's names are the recorded member names, byte for byte and in order."""
        with zipfile.ZipFile(self.out) as archive:
            self.assertEqual(
                archive.namelist(), [member.name for member in result.members]
            )

    def test_listed_files_land_under_their_prefix_with_matching_hashes(self) -> None:
        result = self._write(
            bz.ZipSource("workspace", self.workspace, (".superpowers", "src/__*")),
            bz.ZipSource("home", self.home, (".zshrc",)),
        )
        names = [member.name for member in result.members]
        self.assertEqual(
            names,
            [
                "workspace/.superpowers/sdd/report.md",
                "workspace/.superpowers/top.md",
                "workspace/src/__mock.html",
                "home/.zshrc",
            ],
        )
        self.assertEqual((result.failed, result.skipped_symlinks), ((), ()))
        with zipfile.ZipFile(self.out) as archive:
            for member in result.members:
                self.assertEqual(sha256_bytes(archive.read(member.name)), member.sha256)

    def test_an_entry_matching_nothing_is_a_failed_item(self) -> None:
        result = self._write(bz.ZipSource("home", self.home, (".gitconfig",)))
        self.assertEqual([f.item for f in result.failed], ["home:.gitconfig"])

    def test_symlinks_are_recorded_and_never_followed(self) -> None:
        outside = self.tmp / "outside"
        (outside / "secret").mkdir(parents=True)
        self._file(outside / "secret" / "x", "x")
        (self.workspace / ".superpowers" / "dirlink").symlink_to(outside / "secret")
        (self.workspace / ".superpowers" / "filelink").symlink_to(
            outside / "secret" / "x"
        )
        (self.home / ".zprofile").symlink_to(outside / "secret" / "x")
        result = self._write(
            bz.ZipSource("workspace", self.workspace, (".superpowers",)),
            bz.ZipSource("home", self.home, (".zprofile",)),
        )
        self.assertEqual(
            result.skipped_symlinks,
            (
                "home/.zprofile",
                "workspace/.superpowers/dirlink",
                "workspace/.superpowers/filelink",
            ),
        )
        self.assertNotIn("x", [Path(m.name).name for m in result.members])
        self.assertEqual([f.item for f in result.failed], ["home:.zprofile"])

    def test_special_files_are_skipped_and_duplicates_written_once(self) -> None:
        _make_fifo(self.workspace / ".superpowers" / "pipe")
        _make_fifo(self.home / ".fifo")
        result = self._write(
            bz.ZipSource(
                "workspace", self.workspace, (".superpowers", ".superpowers/top.md")
            ),
            bz.ZipSource("home", self.home, (".fifo",)),
        )
        names = [member.name for member in result.members]
        self.assertEqual(names.count("workspace/.superpowers/top.md"), 1)
        self.assertNotIn("workspace/.superpowers/pipe", names)
        self.assertEqual([f.item for f in result.failed], ["home:.fifo"])

    def test_an_unreadable_file_is_a_failed_item(self) -> None:
        locked = self.home / ".zshrc"
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o600)
        result = self._write(bz.ZipSource("home", self.home, (".zshrc",)))
        self.assertEqual(result.members, ())
        self.assertEqual([f.item for f in result.failed], ["home/.zshrc"])

    def test_a_prefix_that_is_not_one_safe_component_is_refused_before_writing(
        self,
    ) -> None:
        # The valid source goes first: every prefix is checked before anything is written.
        for prefix in ("", ".", "..", "home/extra", "/home", "nul\x00"):
            with self.subTest(prefix=prefix):
                with self.assertRaises(ValueError):
                    self._write(
                        bz.ZipSource("workspace", self.workspace, ("src",)),
                        bz.ZipSource(prefix, self.home, (".zshrc",)),
                    )
                self.assertFalse(self.out.parent.exists())

    def test_a_pattern_matching_the_root_itself_names_no_member_by_the_root(
        self,
    ) -> None:
        result = self._write(bz.ZipSource("workspace", self.workspace, ("**",)))
        self.assertEqual(result.failed, ())
        self.assertIn("workspace/src/keep.py", [m.name for m in result.members])
        self._assert_archive_holds_exactly(result)

    def test_a_symlinked_root_is_walked_as_the_root_and_never_named_itself(
        self,
    ) -> None:
        # The root is configuration, so a symlinked one is listed; nothing beneath it is
        # followed, and no member is ever named for the root itself.
        linked = self.tmp / "linked"
        linked.symlink_to(self.workspace, target_is_directory=True)
        result = self._write(bz.ZipSource("workspace", linked, ("**",)))
        self.assertIn("workspace/src/keep.py", [m.name for m in result.members])
        self.assertEqual((result.failed, result.skipped_symlinks), ((), ()))
        self._assert_archive_holds_exactly(result)

    def test_an_unsafe_member_name_from_enumeration_is_failed_and_never_written(
        self,
    ) -> None:
        # Whatever enumeration yields, a name the manifest refuses is never written:
        # ZipInfo would silently rewrite "workspace/." to "workspace".
        found = bz.Expansion(
            files=(".", "src/keep.py"), symlinks=("",), unreadable_dirs=()
        )
        with mock.patch.object(bz, "expand", return_value=found):
            result = self._write(bz.ZipSource("workspace", self.workspace, ("**",)))
        self.assertEqual([m.name for m in result.members], ["workspace/src/keep.py"])
        self.assertEqual(
            sorted(f.item for f in result.failed), ["workspace/", "workspace/."]
        )
        self.assertEqual(result.skipped_symlinks, ())
        self._assert_archive_holds_exactly(result)

    def test_a_pattern_that_could_leave_its_root_is_a_failed_item(self) -> None:
        self._file(self.tmp / "outside.txt", "o")
        unsafe = ("", ".", "..", "../outside.txt", "/abs", "src//keep.py", "src/")
        result = self._write(bz.ZipSource("workspace", self.workspace, unsafe))
        self.assertEqual(result.members, ())
        self.assertEqual(
            [f.item for f in result.failed], [f"workspace:{p}" for p in unsafe]
        )
        self.assertTrue(all("unsafe pattern" in f.reason for f in result.failed))
        for pattern in unsafe:
            with self.subTest(pattern=pattern):
                with self.assertRaises(bz.PatternError):
                    bz.expand(self.workspace, pattern)

    def test_an_unreadable_directory_is_a_failed_item_and_its_siblings_are_archived(
        self,
    ) -> None:
        self._lock(self.workspace / ".superpowers" / "sdd")
        result = self._write(
            bz.ZipSource("workspace", self.workspace, (".superpowers",))
        )
        self.assertEqual(
            [m.name for m in result.members], ["workspace/.superpowers/top.md"]
        )
        self.assertEqual(
            [f.item for f in result.failed], ["workspace/.superpowers/sdd"]
        )
        self.assertTrue(result.failed[0].reason.startswith(_UNREADABLE_DIRECTORY))

    def test_files_under_an_unreadable_directory_never_vanish_unrecorded(self) -> None:
        # Path.glob swallows the error of a directory it cannot list, so both patterns once
        # archived the readable sibling and said nothing about the locked one.
        ws = self.tmp / "ws"
        self._file(ws / "a" / "ok" / "n.md", "n")
        self._file(ws / "a" / "locked" / "hidden.md", "h")
        self._lock(ws / "a" / "locked")
        for pattern in ("**/*.md", "a/*/*.md"):
            with self.subTest(pattern=pattern):
                result = self._write(bz.ZipSource("ws", ws, (pattern,)))
                self.assertEqual([m.name for m in result.members], ["ws/a/ok/n.md"])
                self.assertEqual(
                    [
                        (f.item, f.reason.startswith(_UNREADABLE_DIRECTORY))
                        for f in result.failed
                    ],
                    [("ws/a/locked", True)],
                )

    def test_a_symlinked_directory_on_the_way_to_a_pattern_is_skipped_not_followed(
        self,
    ) -> None:
        outside = self.tmp / "outside"
        self._file(outside / "secret.txt", "s")
        (self.workspace / "cfg").symlink_to(outside, target_is_directory=True)
        # A link whose target's type cannot even be read is recorded too: it might hide one.
        sealed = self.tmp / "sealed"
        (sealed / "inner").mkdir(parents=True)
        (self.workspace / "cfg2").symlink_to(sealed / "inner", target_is_directory=True)
        self._lock(sealed)
        patterns = ("cfg/secret.txt", "cfg2/secret.txt")
        result = self._write(bz.ZipSource("workspace", self.workspace, patterns))
        self.assertEqual(result.members, ())
        self.assertEqual(result.skipped_symlinks, ("workspace/cfg", "workspace/cfg2"))
        self.assertEqual(
            [f.item for f in result.failed], [f"workspace:{p}" for p in patterns]
        )

    def test_a_walk_never_lists_a_directory_no_pattern_can_reach(self) -> None:
        # A home root holds directories nothing may read. Listing one would report it as a
        # failure, and would read where this tool has no business reading.
        (self.home / "Private").mkdir()
        self._lock(self.home / "Private")
        self._file(self.home / ".config" / "tool" / "settings", "s")
        (self.home / ".config" / "other").mkdir()
        self._lock(self.home / ".config" / "other")
        (self.home / "elsewhere").symlink_to(self.tmp, target_is_directory=True)
        patterns = (".zshrc", ".config/tool/settings")
        result = self._write(bz.ZipSource("home", self.home, patterns))
        self.assertEqual(
            [m.name for m in result.members],
            ["home/.zshrc", "home/.config/tool/settings"],
        )
        self.assertEqual((result.failed, result.skipped_symlinks), ((), ()))

    def test_a_missing_root_is_an_unreadable_directory(self) -> None:
        result = self._write(bz.ZipSource("gone", self.tmp / "missing", (".zshrc",)))
        self.assertEqual([f.item for f in result.failed], ["gone", "gone:.zshrc"])
        self.assertTrue(result.failed[0].reason.startswith(_UNREADABLE_DIRECTORY))

    def test_the_member_reader_refuses_a_fifo_without_blocking(self) -> None:
        fifo = self.tmp / "fifo"
        _make_fifo(fifo)
        refused: list[OSError] = []

        def read() -> None:
            try:
                bz.read_regular_file(self.tmp, fifo.name)
            except OSError as exc:
                refused.append(exc)

        worker = threading.Thread(target=read, daemon=True)
        worker.start()
        worker.join(_BLOCKED_AFTER_S)
        self.assertFalse(worker.is_alive(), "opening a FIFO blocked")
        self.assertEqual(len(refused), 1)

    def test_the_member_reader_never_follows_a_symlink(self) -> None:
        link = self.tmp / "link"
        link.symlink_to(self.home / ".zshrc")
        with self.assertRaises(OSError):
            bz.read_regular_file(self.tmp, link.name)
        self.assertEqual(bz.read_regular_file(self.home, ".zshrc")[0], b"z")

    def test_a_fifo_or_symlink_swapped_in_after_the_walk_is_a_failed_item(self) -> None:
        # The walk skips both, so the swap is simulated by handing the writer their names.
        _make_fifo(self.home / "swapped-fifo")
        self._file(self.tmp / "outside.txt", "outside")
        (self.home / "swapped-link").symlink_to(self.tmp / "outside.txt")
        found = bz.Expansion(
            files=(".zshrc", "swapped-fifo", "swapped-link"),
            symlinks=(),
            unreadable_dirs=(),
        )
        with (
            mock.patch.object(bz, "expand", return_value=found),
            mock.patch.object(bz, "MEMBER_READ_TIMEOUT_S", _BLOCKED_AFTER_S),
        ):
            result = self._write(bz.ZipSource("home", self.home, ("*",)))
        self.assertEqual([m.name for m in result.members], ["home/.zshrc"])
        self.assertEqual(
            [(f.item, f.reason) for f in result.failed],
            [
                ("home/swapped-fifo", "unreadable or timed out"),
                ("home/swapped-link", "unreadable or timed out"),
            ],
        )

    def test_member_times_are_clamped_to_the_zip_range_and_modes_kept(self) -> None:
        old, new = self.home / "old", self.home / "new"
        self._file(old, "o")
        self._file(new, "n")
        os.utime(old, (0.0, 0.0))
        os.utime(new, (_FAR_FUTURE_S, _FAR_FUTURE_S))
        old.chmod(0o640)
        result = self._write(bz.ZipSource("home", self.home, ("old", "new")))
        self.assertEqual(result.failed, ())
        with zipfile.ZipFile(self.out) as archive:
            self.assertEqual(archive.getinfo("home/old").date_time, _ZIP_FIRST)
            self.assertEqual(archive.getinfo("home/new").date_time, _ZIP_LAST)
            self.assertEqual(
                archive.getinfo("home/old").external_attr >> _MODE_SHIFT,
                os.lstat(old).st_mode & _MODE_MASK,
            )

    def test_a_member_larger_than_any_read_chunk_is_archived_whole(self) -> None:
        # A single-chunk read would truncate the member, and its recorded hash would still
        # match the truncated bytes -- so size, hash and bytes are all held to the whole file.
        content = _non_repeating_bytes(_LARGER_THAN_ANY_READ_CHUNK)
        (self.home / "big").write_bytes(content)
        result = self._write(bz.ZipSource("home", self.home, ("big",)))
        self.assertEqual(
            [(m.name, m.size, m.sha256) for m in result.members],
            [("home/big", len(content), hashlib.sha256(content).hexdigest())],
        )
        with zipfile.ZipFile(self.out) as archive:
            archived = archive.read("home/big")
        # Not assertEqual: its failure message would print a megabyte of bytes twice.
        self.assertTrue(
            archived == content, "the archived member differs from its file"
        )

    def test_a_middle_directory_swapped_for_a_symlink_after_the_walk_is_never_followed(
        self,
    ) -> None:
        # The walk lists d/a.md; before the read, d becomes a link to a directory outside the
        # root that holds its own a.md. Guarding only the last component would archive it.
        ws = self.tmp / "ws"
        self._file(ws / "d" / "a.md", "inside")
        outside = self.tmp / "elsewhere"
        self._file(outside / "a.md", "OUTSIDE")
        walk = bz.expand

        def walk_then_swap(root: Path, pattern: str) -> bz.Expansion:
            found = walk(root, pattern)
            shutil.rmtree(ws / "d")
            (ws / "d").symlink_to(outside, target_is_directory=True)
            return found

        with mock.patch.object(bz, "expand", side_effect=walk_then_swap):
            result = self._write(bz.ZipSource("ws", ws, ("d/a.md",)))
        self.assertEqual(result.members, ())
        self.assertEqual(
            [(f.item, f.reason) for f in result.failed],
            [("ws/d/a.md", "unreadable or timed out")],
        )
        self._assert_archive_holds_exactly(result)

    def test_what_is_archived_is_exactly_what_drift_calls_covered(self) -> None:
        ws = self.tmp / "ws"
        for rel in (
            "a/ok/n.md",
            "a/locked/hidden.md",
            "a/deep/x/y.txt",
            "notes.txt",
            "src/__mock.html",
            "src/keep.py",
            "top.md",
        ):
            self._file(ws / rel, rel)
        self._file(self.tmp / "outside" / "secret.txt", "s")
        (ws / "cfg").symlink_to(self.tmp / "outside", target_is_directory=True)
        (ws / "link.txt").symlink_to(ws / "notes.txt")
        patterns = ("**/*.md", "a/*/*.md", "src/__**", "a/deep", "cfg/secret.txt")
        covered = {
            rel
            for rel in _regular_files(ws)
            if not bd.unclassified([rel], extras=patterns, regenerable=())
        }
        self.assertIn("a/locked/hidden.md", covered)
        self._lock(ws / "a" / "locked")

        result = self._write(bz.ZipSource("ws", ws, patterns))
        archived = {_relative(m.name) for m in result.members}
        unreadable = [
            _relative(f.item)
            for f in result.failed
            if f.reason.startswith(_UNREADABLE_DIRECTORY)
        ]
        skipped = [_relative(name) for name in result.skipped_symlinks]
        # Recorded once, though two patterns reach it.
        self.assertEqual((unreadable, skipped), (["a/locked"], ["cfg"]))
        self.assertLessEqual(archived, covered)
        self.assertIn("src/__mock.html", archived)
        accounted = [f"{name}/" for name in unreadable + skipped]
        self.assertEqual(
            {rel for rel in covered - archived if not rel.startswith(tuple(accounted))},
            set(),
        )
