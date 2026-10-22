#!/usr/bin/env python3
"""The regular-file reader under a root: it leaks no descriptor and never leaves the root.

A member and a promoted manifest are both read relative to a descriptor on their root. That
descriptor is closed on every read, however many there are, and a name is refused unless it
is a safe relative name: ``dir_fd`` is ignored for an absolute path, and ``O_NOFOLLOW_ANY``
does not refuse a ``..`` component, so either would otherwise read a file outside the root.

Every file is in the case's own resolved temp dir, so no symlink above the root takes part.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import _fixtures as fx  # noqa: F401 - installs the src/ bootstrap

import backup_zip as bz

#: Reads in one batch: a leaked descriptor per read shows as this many more open.
_READS = 20
_BOUND_S = 5.0


def _open_descriptors() -> int:
    """How many descriptors this process holds open, as its /dev/fd lists them."""
    return len(os.listdir("/dev/fd"))


class RegularReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.root = self.tmp / "root"
        self.root.mkdir()
        (self.root / "inside.txt").write_bytes(b"inside")
        self.outside = self.tmp / "outside.txt"
        self.outside.write_bytes(b"outside")

    def test_a_batch_of_reads_leaves_no_descriptor_open(self) -> None:
        bz.read_regular_file(self.root, "inside.txt")
        before = _open_descriptors()
        for _ in range(_READS):
            self.assertEqual(
                bz.read_regular_file(self.root, "inside.txt")[0], b"inside"
            )
        self.assertEqual(_open_descriptors(), before)

    def test_a_name_that_could_leave_the_root_is_refused_before_it_is_opened(
        self,
    ) -> None:
        # The first two, opened as given, read the regular file outside the root; the third
        # passes through a file, and is refused by its name before any open is tried.
        for rel in (
            "../outside.txt",
            str(self.outside),
            "inside.txt/../../outside.txt",
        ):
            with self.subTest(rel=rel):
                with self.assertRaises(OSError):
                    bz.read_regular_file(self.root, rel)
                self.assertIsNone(bz.read_member(self.root, rel, _BOUND_S))


if __name__ == "__main__":
    unittest.main()
