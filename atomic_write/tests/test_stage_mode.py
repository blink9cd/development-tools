"""Grouped suite: the mode a staging file is created with, and the mode it ends at.

Every failure here has one symptom: for a moment a file being written sat on disk under
a wider mode than the one it was to be published with, so another account could open
it then and keep reading after the mode was narrowed; or closing that window moved the
mode a finished file lands at. The creation mode is read at the `os.open` seam and,
on a host with mode bits, from the file itself at the moment its mode is set.

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
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import atomic_write  # noqa: E402 - import follows the sys.path bootstrap above
from _scratch import Scratch  # noqa: E402 - beside this module

#: Readable and writable by the account that made the file, and by no other.
PRIVATE = 0o600
#: What a plain `open(path, "wb")` asks for on a new file, before the umask.
PLAIN = 0o666


class CreationModeTest(Scratch):
    """What the staging file's `os.open` asks the host for."""

    def creation_modes(self, write: Callable[[], object]) -> list[int]:
        """The mode each creating `os.open` was handed while *write* ran."""
        seen: list[int] = []
        real_open = os.open

        def recording_open(path: Path, flags: int, mode: int = 0o777) -> int:
            if flags & os.O_CREAT:
                seen.append(mode)
            return real_open(path, flags, mode)

        with mock.patch.object(atomic_write.os, "open", recording_open):
            write()
        return seen

    def test_a_file_with_a_mode_to_carry_is_staged_private(self) -> None:
        kept = self.folder / "kept.txt"
        kept.write_bytes(b"old\n")
        writes: dict[str, Callable[[], object]] = {
            "the served default": lambda: atomic_write.replace_bytes(
                self.folder / "served.txt", b"new\n"
            ),
            "a mode given": lambda: atomic_write.replace_text(
                self.folder / "wide.txt", "new\n", mode=0o664
            ),
            "a mode kept": lambda: atomic_write.replace_bytes(
                kept, b"new\n", mode=atomic_write.PRESERVE
            ),
            "a file created once": lambda: atomic_write.create_bytes(
                self.folder / "once.txt", b"new\n"
            ),
        }
        for case, write in writes.items():
            with self.subTest(case=case):
                self.assertEqual(self.creation_modes(write), [PRIVATE])

    def test_a_new_file_with_no_mode_to_keep_is_staged_as_a_plain_write(self) -> None:
        # Nothing is set on it afterward, so the mode it is created with is the mode
        # it is published with: no moment exists in which it is wider than that, and
        # asking for anything narrower would ignore the caller's umask.
        fresh = self.folder / "fresh.txt"
        modes = self.creation_modes(
            lambda: atomic_write.replace_bytes(
                fresh, b"new\n", mode=atomic_write.PRESERVE
            )
        )
        self.assertEqual(modes, [PLAIN])


@unittest.skipIf(os.name == "nt", "POSIX mode bits are not meaningful on NTFS")
class ModeOnDiskTest(Scratch):
    """The staging file's real mode before it is set, and the finished file's after."""

    def with_umask(self, mask: int) -> None:
        """Run the rest of this test under *mask*."""
        self.addCleanup(os.umask, os.umask(mask))

    def test_no_umask_can_leave_the_staging_file_open_to_others(self) -> None:
        # A umask of zero is the widest a caller can run under: the staging file
        # would be created 0666 if the creation mode were the plain one.
        self.with_umask(0)
        found: list[int] = []
        # Looked up by name: the checker also runs for Windows, whose os has none
        # below Python 3.13, and this class is skipped there.
        real_fchmod: Callable[[int, int], None] = getattr(os, "fchmod")

        def recording_fchmod(descriptor: int, mode: int) -> None:
            found.append(stat.S_IMODE(os.fstat(descriptor).st_mode))
            real_fchmod(descriptor, mode)

        with mock.patch.object(atomic_write.os, "fchmod", recording_fchmod):
            atomic_write.replace_bytes(self.folder / "served.txt", b"new\n")
            atomic_write.create_bytes(self.folder / "once.txt", b"new\n")
        self.assertEqual(found, [PRIVATE, PRIVATE])

    def test_each_finished_file_lands_at_the_mode_it_always_did(self) -> None:
        for mask in (0, 0o027, 0o077):
            with self.subTest(umask=oct(mask)):
                self.with_umask(mask)
                folder = self.folder / f"under-{mask:03o}"
                folder.mkdir(mode=0o700)
                kept = folder / "kept.sh"
                kept.write_bytes(b"old\n")
                kept.chmod(0o750)
                atomic_write.replace_bytes(folder / "served.txt", b"new\n")
                atomic_write.replace_bytes(folder / "wide.txt", b"new\n", mode=0o664)
                atomic_write.replace_bytes(folder / "narrow.txt", b"new\n", mode=0o400)
                atomic_write.replace_bytes(kept, b"new\n", mode=atomic_write.PRESERVE)
                atomic_write.replace_bytes(
                    folder / "fresh.txt", b"new\n", mode=atomic_write.PRESERVE
                )
                atomic_write.create_bytes(folder / "once.txt", b"new\n")
                atomic_write.create_bytes(folder / "secret.txt", b"new\n", mode=0o600)
                landed = {
                    entry.name: stat.S_IMODE(entry.stat().st_mode)
                    for entry in folder.iterdir()
                }
                self.assertEqual(
                    landed,
                    {
                        "served.txt": 0o644,
                        "wide.txt": 0o664,
                        "narrow.txt": 0o400,
                        "kept.sh": 0o750,
                        "fresh.txt": PLAIN & ~mask,
                        "once.txt": 0o644,
                        "secret.txt": 0o600,
                    },
                )


if __name__ == "__main__":  # pragma: no cover - direct execution entry point
    unittest.main()
