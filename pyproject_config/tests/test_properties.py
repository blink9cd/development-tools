#!/usr/bin/env python3
"""Property tests: no pyproject text makes a reader raise anything but ConfigError.

Why properties, on top of the examples beside this file
------------------------------------------------------
``test_hostile_input`` pins the inputs that were FOUND to escape. That is a list, and
the defect class is open-ended: the readers sit on a parser and on ``Path.resolve``,
and each raises outside its documented error type for some input nobody listed. The
property states the contract itself, so the next such input fails here instead of in a
consumer: for any text at all, a reader returns its documented shape or raises
:class:`pyproject_config.ConfigError`.

What they are NOT for: coverage. Every branch is reached by the example suites on fixed
inputs; a branch covered only by a generated example has no guaranteed coverage.

Nothing here touches the filesystem: both seams are fakes, as in the other suites.

Entry point assumptions: run through the parent aggregator, or by a fuzzing runner that
imports this file by location with only its own directory on ``sys.path``, hence the
bootstrap naming the tool folder.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

import pyproject_config as pc  # noqa: E402 - import follows the sys.path bootstrap above
from _fixtures import TABLE as _TABLE  # noqa: E402 - same bootstrap

# The determinism convention. In the profile rather than an ``@settings`` decorator
# because a fuzzing run applies its own settings object to each target, and Hypothesis
# refuses a second one on a function that carries one.
# ``deadline=None`` because a per-example wall-clock deadline is a flakiness source and
# not a correctness signal; what bounds a wedged run is the gate's own timeout.
settings.register_profile(
    "deterministic",
    derandomize=True,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("deterministic")

_BASE = Path("/proj").resolve()
_PYPROJECT = _BASE / "pyproject.toml"

# The regression seed for the parser's RecursionError, far past the 1,001 segments
# measured to raise it: generation will not reach ten thousand levels alone. Only the
# dotted key is seeded here, and the nested arrays and inline tables stay in
# test_hostile_input. Those two overflow the real stack, and Hypothesis times the
# collector through a ``gc`` callback; a collection at the bottom of that stack fails
# inside the callback and prints "Exception ignored" over the run. Hypothesis registers
# that callback when the first property runs and leaves it registered for the rest of
# the process, so the example suite's deep-array tests are free of the noise only
# because they run first: discovery loads test_hostile_input before this file.
# The parser counts a key's segments instead and raises without recursing.
_DEEP_DOTTED_KEY = ".".join(["a"] * 10_000) + " = 1\n"

#: Fragments TOML is built from, mixed into arbitrary text so generated documents get
#: far enough into the parser to matter rather than failing on their first character.
_TOML_FRAGMENTS = st.sampled_from(
    ["[", "]", "{", "}", "=", ".", ",", '"', "'", "\n", "tool", _TABLE, "allow", "#"]
)
_DOCUMENTS = st.lists(st.one_of(st.text(max_size=8), _TOML_FRAGMENTS), max_size=40).map(
    "".join
)


def _only_the_pyproject(path: Path) -> bool:
    """is_file seam: the one pyproject at ``_BASE`` exists, and nothing else does."""
    return path == _PYPROJECT


class ReadersRaiseOnlyConfigErrorTest(unittest.TestCase):
    """Any text is either read or refused with ConfigError."""

    @given(_DOCUMENTS)
    @example(_DEEP_DOTTED_KEY)
    @example("tool = 3\n")
    @example(f"[tool]\n{_TABLE} = 3\n")
    def test_fuzz_read_tool_table(self, text: str) -> None:
        try:
            found = pc.read_tool_table(
                _BASE,
                table=_TABLE,
                is_file=_only_the_pyproject,
                read_text=lambda _p: text,
            )
        except pc.ConfigError:
            return
        assert found is not None
        pyproject, table = found
        self.assertEqual(pyproject, _PYPROJECT)
        self.assertIsInstance(table, dict)

    @given(st.lists(st.text(max_size=12), max_size=4))
    @example(["a\x00b"])
    @example([""])
    def test_fuzz_read_path_lists(self, entries: list[str]) -> None:
        """Arbitrary strings as entries: JSON's string escapes are TOML's, bar surrogates.

        An astral character JSON spells as a surrogate pair is one TOML refuses, which
        is a ConfigError like any other and so still inside the property.
        """
        body = f"[tool.{_TABLE}]\nallow = {json.dumps(entries)}\n"
        try:
            lists = pc.read_path_lists(
                _BASE,
                table=_TABLE,
                keys=("allow",),
                is_file=_only_the_pyproject,
                read_text=lambda _p: body,
            )
        except pc.ConfigError:
            return
        self.assertEqual(len(lists["allow"]), len(entries))
        for path in lists["allow"]:
            self.assertTrue(path.is_absolute())
