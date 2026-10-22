"""Grouped suite: property test for the walk-pruning rule backup_zip shares with drift.

The conventions are test_properties.py's: a module-level ``@given`` ``test_fuzz_<name>`` for
the ``# fuzz-target``, run by the TestCase at the bottom, with the shared deterministic
profile loaded before it is defined. Kept out of that file, which is near the length cap.

What it is FOR: backup_zip enumerates a whitelist entry by walking its root and pruning
every directory ``pattern_reaches_beneath`` refuses. A refusal that is wrong drops files
drift calls backed up, silently -- the one miss this tool exists to prevent. So the rule
must never refuse a directory that a matching path is, or lies beneath. Precision (that it
does prune) is carried by test_drift.py and test_zip.py.

Why two input pools. Independently drawn patterns, directories and tails rarely produce the
case that matters most -- a pattern matching only beyond the directory, which must still be
descended. Measured: 3 of 100 independent draws were that case, the property's own draws
held none, and a rule that descends only into a directory the pattern already matches left
this property green. The second pool builds each pattern from the directory and a tail, so
that case is most of what it draws.
"""

from __future__ import annotations

import unittest

from hypothesis import given
from hypothesis import strategies as st

import _fixtures as fx  # noqa: F401 - installs the src/ bootstrap
import _hypothesis_profile  # noqa: F401 - loads the profile before any @given below

import backup_drift as bd

_PATTERN_PARTS = st.sampled_from(["a", "b", "*", "?", "**", "[ab]", "a*"])
_NAMES = st.sampled_from(["a", "b", "ab", "ba"])
_NAME_LISTS = st.lists(_NAMES, min_size=1, max_size=3)

Case = tuple[str, str, str]


@st.composite
def _extending_cases(draw: st.DrawFn) -> Case:
    """A pattern built to match ``directory/tail``: each component literal, ``*`` or ``**``."""
    directory = draw(_NAME_LISTS)
    tail = draw(_NAME_LISTS)
    pattern = [draw(st.sampled_from([name, "*", "**"])) for name in directory + tail]
    return "/".join(pattern), "/".join(directory), "/".join(tail)


_INDEPENDENT_CASES = st.tuples(
    st.lists(_PATTERN_PARTS, min_size=1, max_size=4).map("/".join)
    | st.text(max_size=12),
    st.lists(_NAMES, min_size=1, max_size=4).map("/".join),
    _NAME_LISTS.map("/".join),
)


@given(_INDEPENDENT_CASES | _extending_cases())
def test_fuzz_pattern_reaches_beneath(case: Case) -> None:
    """Never raises; never refuses a directory that matches or holds a matching path."""
    pattern, directory, tail = case
    reaches = bd.pattern_reaches_beneath(pattern, directory)
    if bd.pattern_matches(pattern, directory):
        assert reaches
    if bd.pattern_matches(pattern, f"{directory}/{tail}"):
        assert reaches


class PatternPropertiesRunUnderUnittestTest(unittest.TestCase):
    """Runs the module-level property, which unittest would otherwise never collect."""

    def test_pruning_rule_property_holds(self) -> None:
        test_fuzz_pattern_reaches_beneath()
