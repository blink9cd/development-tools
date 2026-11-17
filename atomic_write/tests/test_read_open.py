"""Grouped suite: how `read_bytes_nofollow` opens its file, and what a failed open is.

Every failure here has one symptom: the open asked the host for something other than a
read that never waits, never follows a link and never translates a byte, or its failure
reached the caller as the wrong thing: a link the host refused passed off as a disk
error, a disk error passed off as a refusal, or a descriptor left open behind either.
The opens that fail are stand-ins raising the host's own error numbers, so each host's
answer is measured on every host.

Entry point assumptions: loaded by the folder's aggregator, or run alone. The insert
below reaches the subject one folder up, never the working directory.
"""

from __future__ import annotations

import errno
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import atomic_write  # noqa: E402 - import follows the sys.path bootstrap above
from _scratch import Scratch  # noqa: E402 - beside this module

DATA = b'{"planted": false}\n'
#: A cap the test file here never reaches: no test in this suite is about the cap.
ROOMY = 1024
#: Every flag bit this host gives a meaning the read cares about, wanted or forbidden.
_HOST_FLAG_BITS = (
    getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NOCTTY", 0)
    | os.O_WRONLY
    | os.O_RDWR
    | os.O_CREAT
    | os.O_TRUNC
)
#: A stand-in for Windows' `O_BINARY` on a host without it: Windows' own 0x8000 unless
#: this host spends that bit on a flag above (some Linux builds number `O_NOFOLLOW`
#: there), and then the next bit up that is free.
FAKE_BINARY = next(1 << n for n in range(15, 31) if not (1 << n) & _HOST_FLAG_BITS)


class OpenAndCloseTest(Scratch):
    """How the file is opened, and that the descriptor never outlives the call."""

    def setUp(self) -> None:
        """Name the path every test here reads."""
        super().setUp()
        self.path = self.folder / "record.json"

    def flags_of_a_read(self) -> int:
        """The flags one good read hands `os.open`, on a host faked to have `O_BINARY`.

        The recording open takes the fake bit off again, because this host may give
        it another meaning, and puts back the host's own flag where there is one.
        """
        self.path.write_bytes(DATA)
        seen: list[int] = []
        real_open = os.open
        host_binary = getattr(os, "O_BINARY", 0)

        def recording_open(path: os.PathLike[str], flags: int) -> int:
            seen.append(flags)
            return real_open(path, (flags & ~FAKE_BINARY) | host_binary)

        with (
            mock.patch.object(atomic_write.os, "O_BINARY", FAKE_BINARY, create=True),
            mock.patch.object(atomic_write.os, "open", recording_open),
        ):
            atomic_write.read_bytes_nofollow(self.path, max_bytes=ROOMY)
        (flags,) = seen
        return flags

    def test_the_open_never_waits_and_never_follows_a_link(self) -> None:
        flags = self.flags_of_a_read()
        for name in ("O_NONBLOCK", "O_NOFOLLOW", "O_NOCTTY"):
            wanted = getattr(os, name, 0)
            with self.subTest(flag=name):
                self.assertEqual(flags & wanted, wanted)
        write_bits = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC
        self.assertEqual(flags & write_bits, 0)

    def test_the_open_is_binary_where_the_host_has_a_text_mode(self) -> None:
        # Windows' flag, faked on every host: without it `os.read` there turns
        # "\r\n" into "\n" and stops at a Ctrl-Z.
        self.assertEqual(self.flags_of_a_read() & FAKE_BINARY, FAKE_BINARY)

    def read_with_open_failing(self, failure: OSError) -> OSError:
        """What a read raises when its open raises *failure*, having closed nothing."""
        self.path.write_bytes(DATA)
        seam = atomic_write.os
        with (
            mock.patch.object(seam, "open", side_effect=failure),
            mock.patch.object(seam, "close", wraps=os.close) as close,
            self.assertRaises(OSError) as caught,
        ):
            atomic_write.read_bytes_nofollow(self.path, max_bytes=ROOMY)
        close.assert_not_called()
        return caught.exception

    def test_each_way_a_host_reports_a_link_at_the_open_is_a_refusal(self) -> None:
        # ELOOP is POSIX's answer, and Linux's and macOS's; EMLINK is FreeBSD's;
        # EFTYPE is NetBSD's, on the hosts that have that number at all.
        codes = {errno.ELOOP, errno.EMLINK, getattr(errno, "EFTYPE", errno.ELOOP)}
        for code in sorted(codes):
            failure = OSError(code, os.strerror(code))
            with (
                self.subTest(errno=errno.errorcode[code]),
                mock.patch.object(atomic_write, "_O_NOFOLLOW", 1),
            ):
                raised = self.read_with_open_failing(failure)
                self.assertIsInstance(raised, atomic_write.ReadRefused)
                self.assertIn("is a symbolic link, or its path loops", str(raised))
                self.assertIs(raised.__cause__, failure)

    def test_any_other_open_error_passes_through_and_is_not_a_refusal(self) -> None:
        for kind, code in (
            (PermissionError, errno.EACCES),
            (FileNotFoundError, errno.ENOENT),
            (OSError, errno.EIO),
        ):
            failure = kind(code, os.strerror(code))
            with self.subTest(errno=errno.errorcode[code]):
                self.assertIs(self.read_with_open_failing(failure), failure)

    def test_a_loop_error_is_not_a_refusal_where_the_open_follows_links(self) -> None:
        # Without O_NOFOLLOW the open never fails for a link at the path itself, so
        # the same number there means only what the host says it means.
        failure = OSError(errno.ELOOP, os.strerror(errno.ELOOP))
        with mock.patch.object(atomic_write, "_O_NOFOLLOW", 0):
            self.assertIs(self.read_with_open_failing(failure), failure)

    def test_the_descriptor_is_closed_when_the_read_itself_raises(self) -> None:
        self.path.write_bytes(DATA)
        failure = OSError("the disk went away")
        seam = atomic_write.os
        with (
            mock.patch.object(seam, "read", side_effect=failure),
            mock.patch.object(seam, "close", wraps=os.close) as close,
            self.assertRaises(OSError) as caught,
        ):
            atomic_write.read_bytes_nofollow(self.path, max_bytes=ROOMY)
        self.assertIs(caught.exception, failure)
        close.assert_called_once()

    def test_the_descriptor_is_closed_when_its_status_cannot_be_read(self) -> None:
        self.path.write_bytes(DATA)
        seam = atomic_write.os
        with (
            mock.patch.object(seam, "fstat", side_effect=KeyboardInterrupt),
            mock.patch.object(seam, "close", wraps=os.close) as close,
            self.assertRaises(KeyboardInterrupt),
        ):
            atomic_write.read_bytes_nofollow(self.path, max_bytes=ROOMY)
        close.assert_called_once()


if __name__ == "__main__":  # pragma: no cover - direct execution entry point
    unittest.main()
