"""The grandchild fixture ends on its own past a generous ceiling.

``samples/gc_child.py`` models a child whose grandchild holds the capture pipes, so it must
outlive every bound a test sets: a pair found gone is then a pair the kill under test ended.
But a pair that never ends leaks for good whenever a test's cleanup is lost (a runner killed
mid-test, say), so both processes exit once a ceiling far past any test's timing has passed.

The live case runs a copy of the fixture with only that ceiling shortened, so it proves the
real fixture's code path ends both processes, without waiting the real hour.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parents[1] / "live_pid_guard"))

import live_pid_guard  # noqa: E402 - import follows the sys.path bootstrap above

_FIXTURE = _HERE.parent / "samples" / "gc_child.py"

#: The fixture's ceiling assignment, the one line the live case rewrites.
_CEILING_LINE = re.compile(r"^_LIFETIME_SECONDS = (.+)$", re.MULTILINE)

#: The ceiling the fixture ships: one hour, past every test bound by two orders of magnitude.
_SHIPPED_CEILING_SECONDS = 3600.0

#: The shortened ceiling, and how long its child gets to exit past it on a loaded machine.
_SHORT_CEILING_SECONDS = 1.0
_EXIT_WAIT_SECONDS = 30.0


def _fixture_with_ceiling(seconds: float) -> str:
    """The fixture's source with its ceiling set to *seconds*; AssertionError without one."""
    source = _FIXTURE.read_text(encoding="utf-8")
    shortened, count = _CEILING_LINE.subn(f"_LIFETIME_SECONDS = {seconds!r}", source)
    if count != 1:
        raise AssertionError(f"{_FIXTURE.name} has {count} ceiling lines, not one")
    return shortened


class FixtureLifetimeTests(unittest.TestCase):
    def test_the_shipped_ceiling_is_an_hour(self) -> None:
        match = _CEILING_LINE.search(_FIXTURE.read_text(encoding="utf-8"))
        if match is None:
            self.fail(f"{_FIXTURE.name} sets no _LIFETIME_SECONDS")
        self.assertEqual(float(match.group(1)), _SHIPPED_CEILING_SECONDS)

    def test_child_and_grandchild_exit_on_their_own_past_the_ceiling(self) -> None:
        source = _fixture_with_ceiling(_SHORT_CEILING_SECONDS)
        with (
            tempfile.TemporaryDirectory() as tmp,
            live_pid_guard.record_fixture_pids() as pids,
        ):
            copy = Path(tmp) / _FIXTURE.name
            copy.write_text(source, encoding="utf-8", newline="\n")
            proc = subprocess.Popen(
                [sys.executable, str(copy)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                returncode = proc.wait(timeout=_EXIT_WAIT_SECONDS)
            finally:
                # Popen.kill polls first, so a child already reaped is never signaled.
                proc.kill()
                proc.wait(timeout=_EXIT_WAIT_SECONDS)
            pids.probe_exits()
        self.assertEqual(returncode, 0)
        self.assertTrue(pids.child_gone)
        self.assertTrue(pids.grandchild_gone, "the grandchild outlived the ceiling")


if __name__ == "__main__":
    unittest.main()
