"""The scratch-directory fixture every `atomic_write` suite is built on.

Not a suite: its name keeps it out of discovery. Public names throughout, because more
than one suite imports it.

Entry point assumptions: imported by the suites beside it and by the folder's
aggregator, each of which has put this directory on `sys.path`.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


class Scratch(unittest.TestCase):
    """A throwaway directory per test; nothing here touches the real tree."""

    def setUp(self) -> None:
        """Make the directory and schedule its removal."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def leftovers(self) -> list[str]:
        """Every name in the scratch directory, for the cleanup assertions."""
        return sorted(entry.name for entry in self.folder.iterdir())
