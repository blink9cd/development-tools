#!/usr/bin/env python3
"""`atomic_write`: one replacement a reader can never catch half-written.

Entry point assumptions: standalone, or discovered by a test runner. The
insert below reaches the subject beside this file, never the working directory.
"""

from __future__ import annotations

import fnmatch
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import atomic_write  # noqa: E402 - import follows the sys.path bootstrap above


class _Scratch(unittest.TestCase):
    """A throwaway directory per test; nothing here touches the real tree."""

    def setUp(self) -> None:
        """Make the directory and schedule its removal."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def _leftovers(self) -> list[str]:
        """Every name in the scratch directory, for the cleanup assertions."""
        return sorted(entry.name for entry in self.folder.iterdir())


class ContentTest(_Scratch):
    """What lands, and in what bytes."""

    def test_a_new_file_gets_exactly_the_bytes_given(self) -> None:
        target = self.folder / "fresh.bin"
        atomic_write.replace_bytes(target, b"\x00\xffdata")
        self.assertEqual(target.read_bytes(), b"\x00\xffdata")

    def test_an_existing_file_is_replaced_whole(self) -> None:
        target = self.folder / "old.txt"
        target.write_text(
            "the previous contents, which are longer\n",
            encoding="utf-8",
            newline="\n",
        )
        atomic_write.replace_text(target, "new\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "new\n")

    def test_text_is_written_as_utf8_with_lf_whatever_the_host_does(self) -> None:
        # Pinned rather than left to the platform: committed outputs are
        # byte-compared against a fresh run, and host text mode writes CRLF on
        # Windows. Encoding here and writing binary is what makes that true
        # without every caller remembering `newline="\n"`.
        target = self.folder / "lines.txt"
        atomic_write.replace_text(target, "one\ntwo\né\n")
        self.assertEqual(target.read_bytes(), "one\ntwo\né\n".encode("utf-8"))

    def test_the_staging_file_is_opened_in_binary_mode(self) -> None:
        # Writing bytes is only half of that. On Windows a descriptor from
        # os.open is in the C runtime's text mode unless O_BINARY is passed, and
        # the runtime then writes every "\n" as "\r\n" beneath the "wb" handle,
        # so the test above passes on a POSIX host and fails on the one
        # host it guards. The flag exists only on Windows, so it is faked at
        # Windows' own value. The recording open strips that bit again, because
        # on this host it may mean something else (0x8000 is O_EVTONLY on
        # macOS), and puts back the host's own flag, so the bytes check below
        # holds on Windows as well.
        fake_binary = 0x8000
        host_binary = getattr(os, "O_BINARY", 0)
        seen: list[int] = []
        real_open = os.open

        def recording_open(path: Path, flags: int, mode: int = 0o777) -> int:
            seen.append(flags)
            return real_open(path, (flags & ~fake_binary) | host_binary, mode)

        target = self.folder / "lines.txt"
        with (
            mock.patch.object(atomic_write.os, "O_BINARY", fake_binary, create=True),
            mock.patch.object(atomic_write.os, "open", recording_open),
        ):
            atomic_write.replace_text(target, "one\ntwo\n")
        self.assertEqual([flags & fake_binary for flags in seen], [fake_binary])
        self.assertEqual(target.read_bytes(), b"one\ntwo\n")


@unittest.skipIf(os.name == "nt", "POSIX mode bits are not meaningful on NTFS")
class ModeTest(_Scratch):
    """The mode the file is published with."""

    def setUp(self) -> None:
        """Scratch directory, plus a strict umask for the duration.

        Load-bearing, and it became so on the rewrite that added `PRESERVE`.
        Staging moved from `mkstemp` (which opens at an explicit 0600 no umask
        can widen, so deleting the `fchmod` failed this test under 0o022 and
        0o077 alike) to a path-based `os.open` at `_NEW_FILE_MODE`, which the
        umask reduces. Under the everyday 0o022 that lands on 0644: the right
        answer for the wrong reason. Measured on the rewrite, the mutant with
        the `fchmod` deleted passes at 0o022 and fails only here at 0o077.

        The note this replaces recorded the `mkstemp` measurement and predicted
        exactly that, which is why the umask was still in place to catch it.
        """
        super().setUp()
        previous = os.umask(0o077)
        self.addCleanup(os.umask, previous)

    def test_a_written_file_is_readable_by_a_server(self) -> None:
        target = self.folder / "served.html"
        atomic_write.replace_text(target, "<p>hello</p>\n")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_a_caller_may_ask_for_a_narrower_mode(self) -> None:
        target = self.folder / "private.txt"
        atomic_write.replace_text(target, "not for a server\n", mode=0o600)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)


class WindowsHostTest(_Scratch):
    """A host with no POSIX mode bits still gets its file."""

    def test_a_host_without_fchmod_still_writes_the_file(self) -> None:
        """Windows below Python 3.13 has no `os.fchmod`, so no mode is set there.

        The host is stood in by name: `sys.platform` reads "win32" and `os.fchmod`
        is absent, as both are on Windows below 3.13. An unguarded call
        raises `AttributeError`, not the `OSError` a caller is told to expect,
        and the target never arrives. The POSIX half, that the mode is still set
        where there is one, is `ModeTest`'s.
        """
        target = self.folder / "out.txt"
        # Removed through the module's namespace rather than read as an attribute,
        # so this also type-checks for the Windows floor, where the name never
        # existed; `patch.dict` puts the namespace back whole afterward.
        with mock.patch.dict(os.__dict__), mock.patch.object(sys, "platform", "win32"):
            os.__dict__.pop("fchmod", None)
            atomic_write.replace_text(target, "whole\n")
        self.assertEqual(target.read_bytes(), b"whole\n")
        self.assertEqual(self._leftovers(), ["out.txt"])


class PreservedModeTest(_Scratch):
    """The mode a caller gets by asking for `PRESERVE` rather than a number.

    Five of the writers this module replaces do not stamp a mode on: they keep
    whatever the file already carried, and a plain `write_text`, which sets no
    mode either, behaves the same way by construction. Forcing those onto
    `SERVED_MODE` would strip an execute bit from a file that had one and ignore
    the umask on a file that had none, so the policy is a caller's choice rather
    than this module's.

    Every assertion compares a mode read back from disk against another read
    back the same way, never a literal: Windows' `chmod` only toggles the
    read-only flag, so a literal would pin the author's platform rather than the
    running one.
    """

    def seeded(self, name: str, mode: int) -> tuple[Path, int]:
        """A file at `name` carrying `mode`, and that mode as the filesystem stored it."""
        path = self.written_plainly(name)
        os.chmod(path, mode)
        return path, stat.S_IMODE(path.stat().st_mode)

    def written_plainly(self, name: str) -> Path:
        """A file written the way a caller would have written it without this tool."""
        path = self.folder / name
        path.write_bytes(b"plain\n")
        return path

    def test_a_rewrite_keeps_the_files_own_bits(self) -> None:
        # 0o755 is the case a flat SERVED_MODE would silently break: a file
        # someone made executable comes back without the bit, and nothing in the
        # tree looks wrong afterward.
        for name, mode in (
            ("served.html", 0o644),
            ("regenerate.sh", 0o755),
            ("group.json", 0o640),
            ("private.txt", 0o600),
        ):
            with self.subTest(seeded=oct(mode)):
                path, before = self.seeded(name, mode)
                atomic_write.replace_text(
                    path, "rewritten\n", mode=atomic_write.PRESERVE
                )
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), before)

    def test_a_new_file_matches_what_a_plain_write_gives(self) -> None:
        # A file that does not exist yet has no bits to keep, and every caller
        # of this policy reaches that path: a first run, a fresh checkout, a
        # year page created in January.
        fresh = self.folder / "fresh.html"
        atomic_write.replace_text(fresh, "new\n", mode=atomic_write.PRESERVE)
        self.assertEqual(
            stat.S_IMODE(fresh.stat().st_mode),
            stat.S_IMODE(self.written_plainly("plain.html").stat().st_mode),
        )

    def test_a_new_file_follows_the_umask_in_both_directions(self) -> None:
        """Two masks, because one cannot tell a creation mode from a literal.

        0o002 leaves group-write standing, which separates asking the kernel for
        0o666 from asking for a hardcoded 0o644: under 0o027 both land 0o640
        and the assertion says nothing. 0o077 catches the opposite mistake, a
        mode stamped on after creation rather than filtered by the mask.
        """
        for mask in (0o002, 0o077):
            with self.subTest(umask=oct(mask)):
                previous = os.umask(mask)
                try:
                    fresh = self.folder / f"masked-{mask:03o}.html"
                    atomic_write.replace_text(
                        fresh, "new\n", mode=atomic_write.PRESERVE
                    )
                    plain = self.written_plainly(f"plain-{mask:03o}.html")
                    self.assertEqual(
                        stat.S_IMODE(fresh.stat().st_mode),
                        stat.S_IMODE(plain.stat().st_mode),
                    )
                finally:
                    os.umask(previous)


class StagingTest(_Scratch):
    """Where the half-written file lives, and what happens to it when the
    write does not finish."""

    def test_the_staging_file_sits_in_the_targets_own_directory(self) -> None:
        # `os.replace` is atomic only within one filesystem, so staging in the
        # system temp dir would silently become a copy across a mount boundary,
        # and a copy is exactly the half-written window this module exists to
        # close.
        target = self.folder / "beside.txt"
        seen: list[Path] = []
        with mock.patch.object(
            atomic_write.os,
            "replace",
            side_effect=lambda src, dst: seen.append(Path(src)),
        ):
            atomic_write.replace_text(target, "staged\n")
        self.assertEqual([path.parent for path in seen], [self.folder])

    def test_a_failed_replace_leaves_nothing_behind(self) -> None:
        # The measured failure: a hand-written copy with no cleanup at all
        # stranded a `.tmp` beside committed data whenever a replace failed.
        target = self.folder / "target.txt"
        with (
            mock.patch.object(atomic_write.os, "replace", side_effect=OSError("no")),
            self.assertRaises(OSError),
        ):
            atomic_write.replace_text(target, "never lands\n")
        self.assertEqual(self._leftovers(), [])

    def test_a_failed_write_leaves_nothing_behind(self) -> None:
        target = self.folder / "target.txt"
        with (
            mock.patch.object(atomic_write.os, "fsync", side_effect=OSError("disk")),
            self.assertRaises(OSError),
        ):
            atomic_write.replace_text(target, "never lands\n")
        self.assertEqual(self._leftovers(), [])

    def test_an_interrupt_mid_write_leaves_nothing_behind(self) -> None:
        # Caught as BaseException rather than Exception on purpose: a Ctrl-C
        # between the write and the rename would otherwise strand the staging
        # file, and an operator who interrupts a build is exactly the reader
        # who then finds it.
        target = self.folder / "target.txt"
        with (
            mock.patch.object(atomic_write.os, "fsync", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            atomic_write.replace_text(target, "never lands\n")
        self.assertEqual(self._leftovers(), [])


class StagingNameTest(_Scratch):
    """The published name builder, which other tools derive ignore patterns from."""

    def test_a_staging_file_matches_the_builder_given_wildcards(self) -> None:
        # The property a caller's ignore rule relies on: hand the builder a glob
        # wildcard for each part and the pattern matches every name `_stage` makes.
        # Pinned through a real staging file, so a respelling inside `_stage` that
        # bypassed the builder would fail here rather than in a corpus.
        target = self.folder / "note.md"
        names: list[str] = []

        def check(staged: Path) -> bool:
            names.append(staged.name)
            return True

        atomic_write.replace_text(target, "new\n", before_rename=check)
        [name] = names
        self.assertTrue(fnmatch.fnmatchcase(name, atomic_write.staging_name("*", "*")))
        self.assertTrue(
            fnmatch.fnmatchcase(name, atomic_write.staging_name(target.name, "*"))
        )

    def test_the_name_carries_the_target_and_the_suffix(self) -> None:
        name = atomic_write.staging_name("note.md", "0123")
        self.assertIn("note.md", name)
        self.assertTrue(name.endswith(atomic_write.STAGING_SUFFIX))


class BeforeRenameTest(_Scratch):
    """The optional check a caller runs on the finished staging file, and its veto.

    It exists for the writers that re-check their target as late as possible: a
    digest taken at read time, compared again just before the swap. Run any earlier
    and the window it guards widens by whatever work is still left; run after the
    rename and it guards nothing.
    """

    def setUp(self) -> None:
        """Scratch directory, an existing target, and the strict umask `ModeTest` uses.

        The umask is what lets the mode the check sees prove anything: under 0o077 a
        staging file the `fchmod` had not reached yet would read 0o600, not 0o644.
        """
        super().setUp()
        previous = os.umask(0o077)
        self.addCleanup(os.umask, previous)
        self.target = self.folder / "note.md"
        self.target.write_bytes(b"old\n")

    def test_the_check_runs_once_on_the_finished_staging_file(self) -> None:
        seen: list[tuple[Path, bytes, bytes]] = []

        def check(staged: Path) -> bool:
            seen.append((staged, staged.read_bytes(), self.target.read_bytes()))
            return True

        landed = atomic_write.replace_text(self.target, "new\n", before_rename=check)
        self.assertTrue(landed)
        self.assertEqual(self.target.read_bytes(), b"new\n")
        [(staged, staged_bytes, target_bytes)] = seen
        self.assertEqual(staged.parent, self.folder)
        self.assertTrue(staged.name.endswith(atomic_write.STAGING_SUFFIX))
        self.assertEqual((staged_bytes, target_bytes), (b"new\n", b"old\n"))
        self.assertEqual(self._leftovers(), ["note.md"])

    @unittest.skipIf(os.name == "nt", "POSIX mode bits are not meaningful on NTFS")
    def test_the_staging_file_already_carries_its_final_mode(self) -> None:
        modes: list[int] = []

        def check(staged: Path) -> bool:
            modes.append(stat.S_IMODE(staged.stat().st_mode))
            return True

        atomic_write.replace_text(self.target, "new\n", before_rename=check)
        self.assertEqual(modes, [atomic_write.SERVED_MODE])

    def test_the_check_falls_after_the_fsync_and_only_the_rename_follows(self) -> None:
        # The ordering IS the guarantee: everything that finishes the staging file is
        # behind the check, and the one step left after it is the swap itself.
        events: list[str] = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(descriptor: int) -> None:
            events.append("fsync")
            real_fsync(descriptor)

        def replace(src: Path, dst: Path) -> None:
            events.append("replace")
            real_replace(src, dst)

        def check(_staged: Path) -> bool:
            events.append("check")
            return True

        with (
            mock.patch.object(atomic_write.os, "fsync", fsync),
            mock.patch.object(atomic_write.os, "replace", replace),
        ):
            atomic_write.replace_text(self.target, "new\n", before_rename=check)
        self.assertEqual(events, ["fsync", "check", "replace"])

    def test_a_veto_leaves_the_target_untouched_and_removes_the_staging(self) -> None:
        with mock.patch.object(atomic_write.os, "replace") as replace:
            landed = atomic_write.replace_text(
                self.target, "new\n", before_rename=lambda _staged: False
            )
        self.assertFalse(landed)
        replace.assert_not_called()
        self.assertEqual(self.target.read_bytes(), b"old\n")
        self.assertEqual(self._leftovers(), ["note.md"])

    def test_a_raising_check_propagates_and_removes_the_staging(self) -> None:
        class Refused(RuntimeError):
            pass

        def check(_staged: Path) -> bool:
            raise Refused("changed under us")

        with self.assertRaises(Refused):
            atomic_write.replace_bytes(self.target, b"new\n", before_rename=check)
        self.assertEqual(self.target.read_bytes(), b"old\n")
        self.assertEqual(self._leftovers(), ["note.md"])

    def test_without_a_check_a_replace_reports_that_it_landed(self) -> None:
        self.assertTrue(atomic_write.replace_bytes(self.target, b"new\n"))
        self.assertEqual(self.target.read_bytes(), b"new\n")


if __name__ == "__main__":  # pragma: no cover - direct execution entry point
    unittest.main()
