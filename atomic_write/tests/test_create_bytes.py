"""Grouped suite: `create_bytes` makes a file once, whole, and never writes over one.

Every failure here has one symptom: a file that had to be created exactly once was
written over, written through a link, or left half-made beside a stranded staging file.

Entry point assumptions: loaded by the folder's aggregator, or run alone. The insert
below reaches the subject one folder up, never the working directory.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import atomic_write  # noqa: E402 - import follows the sys.path bootstrap above
from _scratch import Scratch  # noqa: E402 - beside this module

#: How long a racing writer waits for its partner: bounds a hang, never a healthy round.
_RACE_TIMEOUT_SECONDS = 10.0


class CreateBytesTest(Scratch):
    """`create_bytes`: a whole new file, or `FileExistsError` when anything is there."""

    #: Rounds of the two-writer race: one round can pass by luck, a run of them cannot.
    RACE_ROUNDS = 40

    def test_a_new_file_gets_exactly_the_bytes_and_no_staging_stays(self) -> None:
        target = self.folder / "token"
        atomic_write.create_bytes(target, b"\x00\xffdata")
        self.assertEqual(target.read_bytes(), b"\x00\xffdata")
        self.assertEqual(self.leftovers(), ["token"])

    @unittest.skipIf(os.name == "nt", "symlinks need a privilege on Windows")
    def test_anything_at_the_target_is_refused_and_left_as_it_was(self) -> None:
        # A dangling symlink too: the file it points at must never be created.
        regular, link = self.folder / "file", self.folder / "link"
        regular.write_bytes(b"first\n")
        link.symlink_to(self.folder / "elsewhere")
        for target in (regular, link):
            with self.subTest(target=target.name), self.assertRaises(FileExistsError):
                atomic_write.create_bytes(target, b"never lands\n")
        self.assertEqual((regular.read_bytes(), link.is_symlink()), (b"first\n", True))
        self.assertEqual(self.leftovers(), ["file", "link"])

    def test_a_refused_link_surfaces_and_leaves_nothing(self) -> None:
        # No hard links (FAT, exFAT, some SMB): no fallback, the link's error surfaces.
        refusal = PermissionError(errno.EPERM, "Operation not permitted")
        with (
            mock.patch.object(atomic_write.os, "link", side_effect=refusal),
            self.assertRaises(PermissionError) as caught,
        ):
            atomic_write.create_bytes(self.folder / "token", b"never lands\n")
        self.assertIs(caught.exception, refusal)
        self.assertEqual(self.leftovers(), [])

    def test_an_interrupt_mid_write_leaves_nothing_behind(self) -> None:
        with (
            mock.patch.object(atomic_write.os, "fsync", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            atomic_write.create_bytes(self.folder / "token", b"never lands\n")
        self.assertEqual(self.leftovers(), [])

    @unittest.skipIf(os.name == "nt", "POSIX mode bits are not meaningful on NTFS")
    def test_the_mode_is_set_rather_than_left_to_the_umask(self) -> None:
        # Under umask 0o022 an unset mode lands 0o644: only `fchmod` gives 0o664.
        previous = os.umask(0o022)
        self.addCleanup(os.umask, previous)
        target = self.folder / "token"
        atomic_write.create_bytes(target, b"data\n", mode=0o664)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o664)

    def test_of_two_concurrent_writers_exactly_one_wins(self) -> None:
        for round_number in range(self.RACE_ROUNDS):
            with self.subTest(round=round_number):
                target = self.folder / f"token-{round_number}"
                winner, loser = self._race(target)
                self.assertIsInstance(winner, bytes)
                self.assertIsInstance(loser, FileExistsError)
                self.assertEqual(target.read_bytes(), winner)
        self.assertEqual(len(self.leftovers()), self.RACE_ROUNDS)

    def _race(self, target: Path) -> list[bytes | BaseException]:
        """Two writers released at once onto `target`: the winner's bytes, then the loss."""
        barrier = threading.Barrier(2)

        def write(data: bytes) -> bytes:
            barrier.wait(timeout=_RACE_TIMEOUT_SECONDS)
            atomic_write.create_bytes(target, data)
            return data

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(write, data) for data in (b"a\n", b"b\n")]
        outcomes = [
            future.exception(timeout=0) or future.result(timeout=0)
            for future in futures
        ]
        return sorted(outcomes, key=lambda outcome: isinstance(outcome, BaseException))


if __name__ == "__main__":  # pragma: no cover - direct execution entry point
    unittest.main()
