#!/usr/bin/env python3
"""bounded_run's ``inherit_stderr``: the child's stderr goes to the caller's own, unread.

A caller that runs a long child for its stdout alone (a sweep whose report it parses) can
let the child's progress lines reach the operator as they are written, instead of holding
them in a pipe until the child ends. The tests point the caller's file descriptor 2 at a
temp file for the duration of one run, so what the child wrote there can be read back,
and restore it before asserting.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bounded_run

#: The run's bound: generous, since a loaded host starts an interpreter slowly.
_BOUND = 60.0

#: A child writing one line to each stream.
_BOTH = [
    sys.executable,
    "-c",
    "import sys; print('to-stdout'); print('to-stderr', file=sys.stderr)",
]

#: The caller's stderr, by descriptor: what an inherited child stream writes to.
_STDERR_FD = 2


def run_with_stderr_to_file(
    *, inherit: bool
) -> "tuple[subprocess.CompletedProcess[str], str]":
    """Run :data:`_BOTH` with fd 2 pointed at a temp file; the result and that file's text.

    *inherit* False leaves ``inherit_stderr`` out of the call, so the default is what runs.
    """
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as sink:
        sys.stderr.flush()
        saved = os.dup(_STDERR_FD)
        try:
            os.dup2(sink.fileno(), _STDERR_FD)
            result = (
                bounded_run.run_bounded(_BOTH, timeout=_BOUND, inherit_stderr=True)
                if inherit
                else bounded_run.run_bounded(_BOTH, timeout=_BOUND)
            )
        finally:
            os.dup2(saved, _STDERR_FD)
            os.close(saved)
        sink.seek(0)
        return result, sink.read()


class InheritStderrTests(unittest.TestCase):
    """The child's stderr is the caller's when asked, and captured otherwise."""

    def test_inherited_stderr_reaches_the_caller_and_is_not_captured(self) -> None:
        result, written = run_with_stderr_to_file(inherit=True)
        self.assertEqual(result.stdout, "to-stdout\n")
        self.assertEqual(result.stderr, "")
        self.assertIn("to-stderr", written)

    def test_the_default_still_captures_stderr(self) -> None:
        result, written = run_with_stderr_to_file(inherit=False)
        self.assertEqual(result.stderr, "to-stderr\n")
        self.assertNotIn("to-stderr", written)


if __name__ == "__main__":
    unittest.main()
