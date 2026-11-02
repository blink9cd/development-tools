#!/usr/bin/env python3
"""The committed samples are what the README's own commands produce, byte for byte.

The commands are read OUT of ``samples/README.md`` rather than restated here, so the
README a person follows by hand and the run this suite checks cannot drift apart: each
``python "$tool" ...`` command in its first ``bash`` block is split as ``sh`` would split
it, ``$scratch`` is pointed at a temporary directory, and the files written there are
compared with the expected files beside the README.
"""

from __future__ import annotations

import contextlib
import io
import re
import shlex
import tempfile
import unittest
from pathlib import Path

import os_schedule_gen as sa

_SAMPLES = Path(sa.__file__).resolve().parent / "samples"
_COMMAND_PREFIX = 'python "$tool" '
_SCRATCH = "$scratch"


def _readme_commands() -> "list[list[str]]":
    """Each ``python "$tool"`` command in the README's first bash block, as argv words."""
    text = (_SAMPLES / "README.md").read_text(encoding="utf-8")
    match = re.search(r"```bash\n(.*?)```", text, flags=re.DOTALL)
    if match is None:
        raise AssertionError("samples/README.md has no bash block")
    joined = match.group(1).replace("\\\n", " ")
    return [
        shlex.split(line[len(_COMMAND_PREFIX) :])
        for line in joined.splitlines()
        if line.startswith(_COMMAND_PREFIX)
    ]


class SampleTests(unittest.TestCase):
    """Run the README's commands into a scratch dir and compare every file."""

    def test_the_readme_holds_a_weekly_and_a_daily_command(self) -> None:
        commands = _readme_commands()
        self.assertEqual(len(commands), 2)
        self.assertIn("--day", commands[0])
        self.assertNotIn("--day", commands[1])

    def test_every_expected_file_is_reproduced_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            for argv in _readme_commands():
                words = [word.replace(_SCRATCH, scratch) for word in argv]
                folder = Path(words[words.index("--output-dir") + 1])
                folder.mkdir(parents=True)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(sa.main(words), 0)
                expected = _SAMPLES / folder.name
                self.assertEqual(
                    sorted(path.name for path in folder.iterdir()),
                    sorted(path.name for path in expected.iterdir()),
                )
                for path in sorted(folder.iterdir()):
                    with self.subTest(sample=f"{folder.name}/{path.name}"):
                        self.assertEqual(
                            path.read_bytes(), (expected / path.name).read_bytes()
                        )


if __name__ == "__main__":
    unittest.main()
