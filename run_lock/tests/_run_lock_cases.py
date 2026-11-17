"""The fixtures every run_lock suite shares: a scratch lock path, and a win32 build of the module.

Not a suite: its name keeps it out of discovery.

Entry point assumptions: imported by the suites beside it and by the folder's aggregator,
each of which has put this directory on ``sys.path``. The insert below reaches the subject
one folder up, never the working directory.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run_lock  # noqa: E402 - import follows the sys.path bootstrap above

#: The umask the mode tests run under: the usual one, which leaves 0o700 and 0o600 intact.
USUAL_UMASK = 0o022


class TempDirCase(unittest.TestCase):
    """A throwaway directory per test, and a lock path one missing folder below it."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.lock_path = self.root / "state" / "run.lock"


def load_as_windows(locking: Callable[[int, int, int], None]) -> ModuleType:
    """A fresh copy of run_lock built down its win32 branch, over a stand-in ``msvcrt``.

    The branch is chosen when the module body runs, so the body runs again here with
    ``sys.platform`` reading win32 and ``msvcrt`` answering through *locking*. That
    puts the Windows locking code under test on every host, and on Windows it is the
    only way to make ``msvcrt.locking`` fail with an error other than contention.
    """
    fake = ModuleType("msvcrt")
    # A bare ModuleType declares no attributes, so mypy refuses each assignment below.
    fake.LK_NBLCK = 2  # type: ignore[attr-defined]  # bare ModuleType; msvcrt's value
    fake.LK_UNLCK = 0  # type: ignore[attr-defined]  # bare ModuleType; msvcrt's value
    fake.locking = locking  # type: ignore[attr-defined]  # bare ModuleType
    found = importlib.util.spec_from_file_location(
        "run_lock_as_windows", run_lock.__file__
    )
    assert found is not None and found.loader is not None
    module = importlib.util.module_from_spec(found)
    with (
        # The module itself joins too, for the import only: a dataclass in it would read
        # sys.modules[cls.__module__] while its body runs. patch.dict removes both after.
        mock.patch.dict(sys.modules, {"msvcrt": fake, found.name: module}),
        mock.patch.object(sys, "platform", "win32"),
    ):
        found.loader.exec_module(module)
    return module
