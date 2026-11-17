#!/usr/bin/env python3
"""The bounded hasher verify reads a promoted snapshot with: no-follow, chunked, stall-bounded.

A promoted snapshot can sit in iCloud Drive, where a file offloaded from this machine blocks on
its first read while iCloud fetches it, with no timeout to pass. So the hash runs on a watchdog
that abandons a read making no progress within the stall bound, rather than one deadline for
the whole file: a read that keeps returning data is never cut off. Every file here is in the
case's own resolved temp dir, so no symlink above the root takes part.
"""

from __future__ import annotations

import errno
import hashlib
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _fixtures as fx  # noqa: F401 - installs the src/ bootstrap

import backup_fs

# mkfifo(3) is POSIX-only. The split is an ``if`` so a win32 type-check pass
# still sees the name; the win32 arm exists only for that pass.
if sys.platform == "win32":

    def _make_fifo(path: Path) -> None:  # pragma: only-on win32
        """Refuse: Windows has no FIFOs, and this tool never runs there."""
        raise OSError(errno.ENOSYS, "mkfifo needs POSIX, which Windows lacks")

else:  # pragma: only-on posix
    _make_fifo = os.mkfifo

#: Bound for reads that are expected to finish: generous, so a loaded machine never trips it.
_BOUND_S = 30.0
#: Bound for reads that are expected to stall: short, so the suite stays quick.
_STALL_S = 0.05
#: How long a blocked fake read waits before giving up on its own, after the test moved on.
_RELEASE_S = 10.0
#: A stall bound for the arriving-data case, and the gap between its chunks: each gap is under
#: the bound, and together they span several, so only a progress bound lets it finish.
_ARRIVING_STALL_S = 1.0
_ARRIVING_GAP_S = 0.6 * _ARRIVING_STALL_S
_ARRIVING_CHUNKS = 6
#: How long the cancel test watches for a read the abandoned worker should not make.
_QUIET_S = 0.3


class BoundedHashTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()

    def test_a_file_of_several_chunks_hashes_as_hashlib_does(self) -> None:
        data = bytes(range(256)) * (3 * (1 << 20) // 256 + 7)
        (self.root / "big.bin").write_bytes(data)
        digest, size = backup_fs.hash_regular_file_bounded(
            self.root, "big.bin", _BOUND_S
        )
        self.assertEqual(digest, hashlib.sha256(data).hexdigest())
        self.assertEqual(size, len(data))

    def test_an_empty_file_hashes_to_the_empty_digest(self) -> None:
        (self.root / "empty").write_bytes(b"")
        self.assertEqual(
            backup_fs.hash_regular_file_bounded(self.root, "empty", _BOUND_S),
            (hashlib.sha256(b"").hexdigest(), 0),
        )

    def test_a_symlink_is_never_followed(self) -> None:
        (self.root / "real").write_bytes(b"same bytes")
        (self.root / "link").symlink_to(self.root / "real")
        with self.assertRaises(OSError) as caught:
            backup_fs.hash_regular_file_bounded(self.root, "link", _BOUND_S)
        self.assertEqual(caught.exception.errno, errno.ELOOP)

    def test_a_symlinked_directory_on_the_way_is_never_followed(self) -> None:
        (self.root / "real").mkdir()
        (self.root / "real" / "file").write_bytes(b"same bytes")
        (self.root / "via").symlink_to(self.root / "real")
        with self.assertRaises(OSError):
            backup_fs.hash_regular_file_bounded(self.root, "via/file", _BOUND_S)

    def test_a_missing_file_raises_file_not_found(self) -> None:
        with self.assertRaises(FileNotFoundError):
            backup_fs.hash_regular_file_bounded(self.root, "absent", _BOUND_S)

    def test_a_fifo_is_refused_without_waiting_for_a_writer(self) -> None:
        _make_fifo(self.root / "pipe")
        with self.assertRaises(OSError) as caught:
            backup_fs.hash_regular_file_bounded(self.root, "pipe", _BOUND_S)
        self.assertEqual(caught.exception.errno, errno.EINVAL)

    def test_a_read_making_no_progress_is_abandoned_as_stalled(self) -> None:
        (self.root / "offloaded").write_bytes(b"never arrives")
        release = threading.Event()

        def blocking_read(fd: int, count: int) -> bytes:
            release.wait(_RELEASE_S)
            return b""

        try:
            with self.assertRaises(backup_fs.ReadStalled) as caught:
                backup_fs.hash_regular_file_bounded(
                    self.root, "offloaded", _STALL_S, read=blocking_read
                )
        finally:
            release.set()
        self.assertIn("offloaded", str(caught.exception))

    def test_a_read_that_stops_after_a_chunk_is_abandoned_as_stalled(self) -> None:
        # Whether the first bound passes before or after the first chunk, no data arrives in
        # a later one, so the read is abandoned either way.
        (self.root / "slow").write_bytes(b"x")
        release = threading.Event()
        calls: list[int] = []

        def one_chunk_then_block(fd: int, count: int) -> bytes:
            calls.append(count)
            if len(calls) == 1:
                return b"chunk"
            release.wait(_RELEASE_S)
            return b""

        try:
            with self.assertRaises(backup_fs.ReadStalled):
                backup_fs.hash_regular_file_bounded(
                    self.root, "slow", _STALL_S, read=one_chunk_then_block
                )
        finally:
            release.set()

    def test_a_read_that_keeps_returning_data_is_never_cut_off(self) -> None:
        # Each chunk comes within the bound, but all of them take several bounds: a deadline on
        # the whole read would abandon it, a bound on progress does not.
        chunks = [bytes([index]) * 7 for index in range(_ARRIVING_CHUNKS)]
        (self.root / "arriving").write_bytes(b"x")
        pending = list(chunks)
        gap = threading.Event()

        def slow_read(fd: int, count: int) -> bytes:
            gap.wait(_ARRIVING_GAP_S)
            return pending.pop(0) if pending else b""

        digest, size = backup_fs.hash_regular_file_bounded(
            self.root, "arriving", _ARRIVING_STALL_S, read=slow_read
        )
        whole = b"".join(chunks)
        self.assertEqual(
            (digest, size), (hashlib.sha256(whole).hexdigest(), len(whole))
        )

    def test_an_abandoned_worker_stops_reading(self) -> None:
        (self.root / "late").write_bytes(b"x")
        release = threading.Event()
        again = threading.Event()
        calls: list[int] = []

        def late_then_more(fd: int, count: int) -> bytes:
            calls.append(count)
            if len(calls) == 1:
                release.wait(_RELEASE_S)
                return b"late chunk"
            again.set()
            return b""

        with self.assertRaises(backup_fs.ReadStalled):
            backup_fs.hash_regular_file_bounded(
                self.root, "late", _STALL_S, read=late_then_more
            )
        release.set()
        # The first read returns now, after the caller gave up: the worker must not read on.
        self.assertFalse(again.wait(_QUIET_S))

    def test_an_error_that_is_not_an_oserror_reaches_the_caller(self) -> None:
        # Never a partial digest: one would be reported as a changed file.
        (self.root / "odd").write_bytes(b"x")

        def broken_read(fd: int, count: int) -> bytes:
            raise ValueError("fixture defect")

        with self.assertRaises(ValueError):
            backup_fs.hash_regular_file_bounded(
                self.root, "odd", _BOUND_S, read=broken_read
            )

    def test_a_dataless_file_is_reported_offloaded_without_a_read(self) -> None:
        # macOS marks a file iCloud evicted SF_DATALESS; a test cannot make one, so the
        # fstat the opener returns is replaced with one carrying the flag.
        (self.root / "evicted").write_bytes(b"x")
        real_open = backup_fs._open_regular

        def dataless_open(root: Path, rel: str) -> "tuple[int, object]":
            fd, _ = real_open(root, rel)
            return fd, SimpleNamespace(st_flags=backup_fs.SF_DATALESS)

        def never_read(fd: int, count: int) -> bytes:
            raise AssertionError("a dataless file must not be read")

        with mock.patch.object(backup_fs, "_open_regular", dataless_open):
            with self.assertRaises(backup_fs.FileOffloaded):
                backup_fs.hash_regular_file_bounded(
                    self.root, "evicted", _BOUND_S, read=never_read
                )

    def test_an_error_raised_by_a_read_reaches_the_caller(self) -> None:
        (self.root / "bad").write_bytes(b"x")

        def failing_read(fd: int, count: int) -> bytes:
            raise OSError(errno.EIO, "fixture I/O error")

        with self.assertRaises(OSError) as caught:
            backup_fs.hash_regular_file_bounded(
                self.root, "bad", _BOUND_S, read=failing_read
            )
        self.assertEqual(caught.exception.errno, errno.EIO)


if __name__ == "__main__":
    unittest.main()
