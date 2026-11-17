"""Grouped suite: `read_bytes_nofollow` reads only the regular file a path itself names.

Every failure here has one symptom: bytes were read through a symbolic link, a reparse
point, a non-file, or a file swapped in after its check, so a planted file could pass
for the one the caller named. Windows has no `O_NOFOLLOW`, so its branches are measured
on every host by fakes: the flag patched to nothing with a real link in place, and an
`lstat` answer carrying the reparse-point attribute. The swap is staged for real, by an
open seam that replaces the file between the check and the open.

Entry point assumptions: loaded by the folder's aggregator, or run alone. The insert
below reaches the subject one folder up, never the working directory.
"""

from __future__ import annotations

import os
import stat
import sys
import unittest
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import atomic_write  # noqa: E402 - import follows the sys.path bootstrap above
from _scratch import Scratch  # noqa: E402 - beside this module

DATA = b'{"planted": false}\n'
#: A cap no test file here reaches, for the tests that are not about the cap.
ROOMY = 1024
#: `O_NONBLOCK` where the host has it; Windows has none, and no named pipe to wait on.
NONBLOCK = getattr(os, "O_NONBLOCK", 0)
#: Windows' "a symbolic link needs a privilege this account lacks".
_NO_LINK_PRIVILEGE = 1314


def lstat_standin(real: Path, mode: int, attributes: int = 0) -> SimpleNamespace:
    """A status for *real*'s identity under another *mode* and Windows *attributes*."""
    info = real.stat()
    return SimpleNamespace(
        st_mode=mode,
        st_file_attributes=attributes,
        st_dev=info.st_dev,
        st_ino=info.st_ino,
    )


class _ReadCase(Scratch):
    """A scratch directory with one path under test, and a link maker that may skip."""

    def setUp(self) -> None:
        """Name the path every test here reads."""
        super().setUp()
        self.path = self.folder / "record.json"

    def planted_file(self) -> Path:
        """A real file elsewhere in the scratch directory, for a link to point at."""
        target = self.folder / "elsewhere.json"
        target.write_bytes(DATA)
        return target

    def link_path_to(self, target: Path) -> None:
        """Make the path a symbolic link to *target*, skipping where that is denied."""
        try:
            self.path.symlink_to(target)
        except OSError as exc:  # pragma: only-on win32
            if getattr(exc, "winerror", None) != _NO_LINK_PRIVILEGE:
                raise
            self.skipTest("this Windows account may not make a symbolic link")

    def make_pipe(self) -> None:
        """Make the path a named pipe, skipping on Windows, which has none.

        The check is `sys.platform` itself, so the type checker's win32 pass reads
        the `mkfifo` below as unreachable.
        """
        if sys.platform == "win32":
            self.skipTest("Windows has no named pipes")  # pragma: only-on win32
        os.mkfifo(self.path)  # pragma: only-on posix


class AcceptedReadTest(_ReadCase):
    """What a regular file gives back."""

    def test_a_regular_file_is_read_whole(self) -> None:
        self.path.write_bytes(DATA)
        got = atomic_write.read_bytes_nofollow(self.path, max_bytes=ROOMY)
        self.assertEqual(got.data, DATA)

    def test_the_bytes_are_the_files_own_with_no_newline_translation(self) -> None:
        raw = b"one\r\ntwo\n\x1a\x00\xff"
        self.path.write_bytes(raw)
        got = atomic_write.read_bytes_nofollow(self.path, max_bytes=ROOMY)
        self.assertEqual(got.data, raw)

    def test_an_empty_file_is_read_as_no_bytes_under_a_zero_cap(self) -> None:
        self.path.write_bytes(b"")
        got = atomic_write.read_bytes_nofollow(self.path, max_bytes=0)
        self.assertEqual(got.data, b"")

    def test_the_status_is_the_descriptors_not_the_earlier_check(self) -> None:
        # The time changes between the lstat and the open, so only a status read from
        # the open descriptor carries the new one: the lstat's is already stale.
        self.path.write_bytes(DATA)
        os.utime(self.path, (1000.0, 1000.0))
        real_open = os.open

        def touch_then_open(path: os.PathLike[str], flags: int, *rest: int) -> int:
            os.utime(self.path, (2000.0, 2000.0))
            return real_open(path, flags, *rest)

        with mock.patch.object(atomic_write.os, "open", touch_then_open):
            got = atomic_write.read_bytes_nofollow(self.path, max_bytes=ROOMY)
        self.assertEqual((got.data, got.status.st_mtime), (DATA, 2000.0))

    def test_a_read_that_comes_back_short_is_continued_to_the_end(self) -> None:
        self.path.write_bytes(DATA)
        real_read = os.read

        def one_byte(descriptor: int, _wanted: int) -> bytes:
            return real_read(descriptor, 1)

        with mock.patch.object(atomic_write.os, "read", one_byte):
            got = atomic_write.read_bytes_nofollow(self.path, max_bytes=len(DATA))
        self.assertEqual(got.data, DATA)

    def test_the_descriptor_is_closed_once_after_a_good_read(self) -> None:
        self.path.write_bytes(DATA)
        with mock.patch.object(atomic_write.os, "close", wraps=os.close) as close:
            atomic_write.read_bytes_nofollow(self.path, max_bytes=ROOMY)
        close.assert_called_once()


class CapTest(_ReadCase):
    """The byte cap: a file at it is read, a file over it is refused."""

    def test_a_file_exactly_at_the_cap_is_read(self) -> None:
        self.path.write_bytes(DATA)
        got = atomic_write.read_bytes_nofollow(self.path, max_bytes=len(DATA))
        self.assertEqual(got.data, DATA)

    def test_a_file_one_byte_over_the_cap_is_refused_and_closed(self) -> None:
        self.path.write_bytes(DATA)
        with (
            mock.patch.object(atomic_write.os, "close", wraps=os.close) as close,
            self.assertRaisesRegex(atomic_write.ReadRefused, "is over 18 bytes"),
        ):
            atomic_write.read_bytes_nofollow(self.path, max_bytes=len(DATA) - 1)
        close.assert_called_once()

    def test_no_more_than_one_byte_past_the_cap_is_ever_asked_for(self) -> None:
        self.path.write_bytes(b"x" * 500)
        asked: list[int] = []
        real_read = os.read

        def counting(descriptor: int, wanted: int) -> bytes:
            asked.append(wanted)
            return real_read(descriptor, wanted)

        with (
            mock.patch.object(atomic_write.os, "read", counting),
            self.assertRaises(atomic_write.ReadRefused),
        ):
            atomic_write.read_bytes_nofollow(self.path, max_bytes=10)
        self.assertEqual(sum(asked), 11)

    def test_a_negative_cap_is_a_callers_error_before_anything_is_touched(self) -> None:
        with (
            mock.patch.object(Path, "lstat") as checked,
            self.assertRaisesRegex(ValueError, "max_bytes"),
        ):
            atomic_write.read_bytes_nofollow(self.path, max_bytes=-1)
        checked.assert_not_called()


class RefusedPathTest(_ReadCase):
    """What the path's own status refuses, before anything is opened."""

    def test_a_missing_file_is_file_not_found_not_a_refusal(self) -> None:
        with self.assertRaises(FileNotFoundError) as caught:
            atomic_write.read_bytes_nofollow(self.path, max_bytes=ROOMY)
        self.assertNotIsInstance(caught.exception, atomic_write.ReadRefused)

    def test_a_refusal_is_an_os_error(self) -> None:
        self.assertTrue(issubclass(atomic_write.ReadRefused, OSError))

    def test_a_link_is_refused_and_its_target_never_opened(self) -> None:
        self.link_path_to(self.planted_file())
        with (
            mock.patch.object(atomic_write.os, "open", wraps=os.open) as opened,
            self.assertRaisesRegex(
                atomic_write.ReadRefused,
                "record.json is a symbolic link or reparse point",
            ),
        ):
            atomic_write.read_bytes_nofollow(self.path, max_bytes=ROOMY)
        opened.assert_not_called()

    def test_a_link_is_refused_where_the_open_would_follow_it(self) -> None:
        # Windows' case on every host: no O_NOFOLLOW, so only the lstat refuses it.
        self.link_path_to(self.planted_file())
        with (
            mock.patch.object(atomic_write, "_O_NOFOLLOW", 0),
            self.assertRaisesRegex(
                atomic_write.ReadRefused, "symbolic link or reparse point"
            ),
        ):
            atomic_write.read_bytes_nofollow(self.path, max_bytes=ROOMY)

    def test_a_reparse_point_is_refused_before_any_open(self) -> None:
        # The attributes are what refuse it: its mode says regular file and its
        # identity is a real file's, so a check that dropped them would open it.
        self.path.write_bytes(DATA)
        standin = lstat_standin(
            self.path, stat.S_IFREG | 0o600, atomic_write.REPARSE_POINT
        )
        with (
            mock.patch.object(Path, "lstat", return_value=standin),
            mock.patch.object(atomic_write.os, "open") as opened,
            self.assertRaisesRegex(
                atomic_write.ReadRefused, "symbolic link or reparse point"
            ),
        ):
            atomic_write.read_bytes_nofollow(self.path, max_bytes=ROOMY)
        opened.assert_not_called()

    def test_a_folder_is_refused_before_any_open(self) -> None:
        self.path.mkdir()
        with (
            mock.patch.object(atomic_write.os, "open", wraps=os.open) as opened,
            self.assertRaisesRegex(atomic_write.ReadRefused, "is not a regular file"),
        ):
            atomic_write.read_bytes_nofollow(self.path, max_bytes=ROOMY)
        opened.assert_not_called()

    def test_a_named_pipe_is_refused_without_waiting_for_a_writer(self) -> None:
        # The open is a stand-in that only fails: were the check ever lost, a real
        # open of this pipe is the one call here that could wait forever.
        self.make_pipe()
        never = AssertionError("the pipe was opened")
        with (
            mock.patch.object(atomic_write.os, "open", side_effect=never) as opened,
            self.assertRaisesRegex(atomic_write.ReadRefused, "is not a regular file"),
        ):
            atomic_write.read_bytes_nofollow(self.path, max_bytes=ROOMY)
        opened.assert_not_called()

    def test_a_link_is_link_like_and_so_is_a_reparse_point(self) -> None:
        self.assertEqual(atomic_write.REPARSE_POINT, 0x400)
        self.assertTrue(atomic_write.is_link_like(stat.S_IFLNK | 0o777, 0))
        self.assertTrue(atomic_write.is_link_like(stat.S_IFREG | 0o600, 0x400))
        self.assertFalse(atomic_write.is_link_like(stat.S_IFREG | 0o600, 0))
        self.assertFalse(atomic_write.is_link_like(stat.S_IFREG | 0o600, 0x800))


class SwappedFileTest(_ReadCase):
    """What the open descriptor refuses: the file changed after the path was checked."""

    def _open_after(self, change: Callable[[], None]) -> Callable[..., int]:
        """An `os.open` stand-in that calls *change* between the check and the open.

        It fails the test, before the real open, when the flags would wait: a pipe
        swapped in and opened without `O_NONBLOCK` blocks until a writer arrives,
        which here is never, so the run would hang where it should go red.
        """
        real_open = os.open

        def changed_then_open(path: os.PathLike[str], flags: int, *rest: int) -> int:
            change()
            if flags & NONBLOCK != NONBLOCK:
                raise AssertionError("the open would wait: O_NONBLOCK is not set")
            return real_open(path, flags, *rest)

        return changed_then_open

    def test_a_file_swapped_after_its_check_is_refused_and_closed(self) -> None:
        self.path.write_bytes(b"{}")
        planted = self.folder / "planted.json"
        planted.write_bytes(DATA)
        seam = atomic_write.os
        swap = self._open_after(lambda: os.replace(planted, self.path))
        with (
            mock.patch.object(seam, "open", swap),
            mock.patch.object(seam, "close", wraps=os.close) as close,
            mock.patch.object(seam, "read", wraps=os.read) as read,
            self.assertRaisesRegex(
                atomic_write.ReadRefused,
                "record.json was replaced between its check and its open",
            ),
        ):
            atomic_write.read_bytes_nofollow(self.path, max_bytes=ROOMY)
        close.assert_called_once()
        read.assert_not_called()

    def test_a_pipe_swapped_in_is_refused_without_waiting_for_a_writer(self) -> None:
        self.path.write_bytes(DATA)

        def swap() -> None:
            self.path.unlink()
            self.make_pipe()

        seam = atomic_write.os
        with (
            mock.patch.object(seam, "open", self._open_after(swap)),
            mock.patch.object(seam, "close", wraps=os.close) as close,
            # Either line may be the one that refuses it: a file system that hands
            # the removed file's number to the pipe passes the identity check, and
            # the descriptor's type is then what catches it.
            self.assertRaisesRegex(
                atomic_write.ReadRefused, "was replaced|is no longer a regular file"
            ),
        ):
            atomic_write.read_bytes_nofollow(self.path, max_bytes=ROOMY)
        close.assert_called_once()

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "this host's open follows a link")
    def test_a_link_swapped_in_is_refused_by_the_open_where_the_host_can(
        self,
    ) -> None:
        # The message and the close count are what tell this open from one that
        # followed the link: that one is refused too, but later, by the identity
        # check, with a descriptor to close.
        self.path.write_bytes(b"{}")
        target = self.planted_file()

        def swap() -> None:
            self.path.unlink()
            self.link_path_to(target)

        seam = atomic_write.os
        with (
            mock.patch.object(seam, "open", self._open_after(swap)),
            mock.patch.object(seam, "close", wraps=os.close) as close,
            mock.patch.object(seam, "read", wraps=os.read) as read,
            self.assertRaisesRegex(
                atomic_write.ReadRefused,
                "record.json is a symbolic link, or its path loops",
            ) as caught,
        ):
            atomic_write.read_bytes_nofollow(self.path, max_bytes=ROOMY)
        self.assertIsInstance(caught.exception.__cause__, OSError)
        close.assert_not_called()
        read.assert_not_called()

    def test_a_link_swapped_in_is_refused_where_the_open_follows_it(self) -> None:
        # Windows' case on every host: the open follows the link to the planted
        # file, and the descriptor's identity is then not the checked file's.
        self.path.write_bytes(b"{}")
        target = self.planted_file()

        def swap() -> None:
            self.path.unlink()
            self.link_path_to(target)

        seam = atomic_write.os
        with (
            mock.patch.object(atomic_write, "_O_NOFOLLOW", 0),
            mock.patch.object(seam, "open", self._open_after(swap)),
            mock.patch.object(seam, "close", wraps=os.close) as close,
            mock.patch.object(seam, "read", wraps=os.read) as read,
            self.assertRaisesRegex(atomic_write.ReadRefused, "was replaced"),
        ):
            atomic_write.read_bytes_nofollow(self.path, max_bytes=ROOMY)
        close.assert_called_once()
        read.assert_not_called()

    def test_a_non_file_under_the_checked_identity_is_refused_and_closed(self) -> None:
        # A recycled inode number: the file is removed and something that is not a
        # file takes its number, so the identity matches and only the descriptor's
        # own type refuses it.
        self.path.write_bytes(DATA)
        pipe = lstat_standin(self.path, stat.S_IFIFO | 0o600)
        seam = atomic_write.os
        with (
            mock.patch.object(seam, "fstat", return_value=pipe),
            mock.patch.object(seam, "close", wraps=os.close) as close,
            mock.patch.object(seam, "read", wraps=os.read) as read,
            self.assertRaisesRegex(
                atomic_write.ReadRefused, "is no longer a regular file"
            ),
        ):
            atomic_write.read_bytes_nofollow(self.path, max_bytes=ROOMY)
        close.assert_called_once()
        read.assert_not_called()


class NoIdentityTest(_ReadCase):
    """A volume that reports no file number, so the swap check has nothing to compare.

    Some Windows network and FAT volumes answer zero. Where the open cannot refuse a
    link either, nothing is left to catch one swapped in after the check, so the read
    is refused; where the open can, it still reads. Both hosts are stood in for on
    every host: the flag by its patchable name, the volume by status stand-ins.
    """

    def unnumbered(self) -> SimpleNamespace:
        """The path's own status, as a volume with no file numbers would report it."""
        info = self.path.lstat()
        return SimpleNamespace(
            st_mode=info.st_mode, st_file_attributes=0, st_dev=info.st_dev, st_ino=0
        )

    def test_no_identity_and_no_nofollow_is_refused_unread_and_closed(self) -> None:
        self.path.write_bytes(DATA)
        blank = self.unnumbered()
        real = self.path.lstat()
        seam = atomic_write.os
        for case, checked, opened in (
            ("the check has none", blank, real),
            ("the descriptor has none", real, blank),
            ("neither has one", blank, blank),
        ):
            with (
                self.subTest(case=case),
                mock.patch.object(atomic_write, "_O_NOFOLLOW", 0),
                mock.patch.object(Path, "lstat", return_value=checked),
                mock.patch.object(seam, "fstat", return_value=opened),
                mock.patch.object(seam, "close", wraps=os.close) as close,
                mock.patch.object(seam, "read", wraps=os.read) as read,
                self.assertRaisesRegex(
                    atomic_write.ReadRefused,
                    "record.json cannot be checked against a swap on this volume",
                ),
            ):
                atomic_write.read_bytes_nofollow(self.path, max_bytes=ROOMY)
            close.assert_called_once()
            read.assert_not_called()

    def test_a_link_swapped_in_on_such_a_volume_is_never_read(self) -> None:
        # The hole itself: the open follows the link, both statuses answer zero, and
        # a comparison of the two would pass the planted file as the checked one.
        target = self.planted_file()
        self.link_path_to(target)  # a probe: skips here where no link can be made
        self.path.unlink()
        self.path.write_bytes(b"{}")
        blank = self.unnumbered()
        real_open = os.open

        def swap_then_open(path: os.PathLike[str], flags: int, *rest: int) -> int:
            self.path.unlink()
            self.path.symlink_to(target)
            return real_open(path, flags, *rest)

        seam = atomic_write.os
        with (
            mock.patch.object(atomic_write, "_O_NOFOLLOW", 0),
            mock.patch.object(Path, "lstat", return_value=blank),
            mock.patch.object(seam, "fstat", return_value=blank),
            mock.patch.object(seam, "open", swap_then_open),
            mock.patch.object(seam, "read", wraps=os.read) as read,
            self.assertRaisesRegex(atomic_write.ReadRefused, "cannot be checked"),
        ):
            atomic_write.read_bytes_nofollow(self.path, max_bytes=ROOMY)
        read.assert_not_called()

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "this host's open follows a link")
    def test_no_identity_is_still_read_where_the_open_refuses_a_link(self) -> None:
        # The control: with O_NOFOLLOW the open itself refuses a swapped-in link, so
        # a volume without file numbers costs nothing there.
        self.path.write_bytes(DATA)
        blank = self.unnumbered()
        with (
            mock.patch.object(Path, "lstat", return_value=blank),
            mock.patch.object(atomic_write.os, "fstat", return_value=blank),
        ):
            got = atomic_write.read_bytes_nofollow(self.path, max_bytes=ROOMY)
        self.assertEqual(got.data, DATA)


if __name__ == "__main__":  # pragma: no cover - direct execution entry point
    unittest.main()
