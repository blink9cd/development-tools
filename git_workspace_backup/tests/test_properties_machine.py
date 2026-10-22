"""Grouped suite: property tests for backup_machine's identity label.

The conventions are test_properties.py's: every ``# fuzz-target`` subject has a module-level
``@given`` ``test_fuzz_<name>`` here, run by the TestCase at the bottom, and the shared
deterministic profile is loaded before any ``@given`` is defined.

What they are FOR: the label becomes the tail of every snapshot folder name, so whatever a
host name holds, the label derived from it must be one plain, path-safe component, and the
validator must accept exactly the labels derivation can produce. The rule is restated here as
a regular expression, so the subject's own pattern cannot also be the thing that judges it.
The dense arm exists because random text rarely forms a valid label, and a property that
never reaches acceptance guards nothing.
"""

from __future__ import annotations

import re
import unittest

from hypothesis import example, given
from hypothesis import strategies as st

import _fixtures as fx  # noqa: F401 - installs the src/ bootstrap
import _hypothesis_profile  # noqa: F401 - loads the profile before any @given below

import backup_machine as bm

#: ASCII lowercase letters and digits in runs joined by single hyphens, at most 32 long.
_RULE = re.compile(r"(?=.{1,32}\Z)[a-z0-9]+(-[a-z0-9]+)*\Z", re.DOTALL)
_DENSE = st.text(alphabet="az09-.A _", max_size=40)
_TEXTS = st.one_of(_DENSE, st.text(max_size=48))


@given(_TEXTS)
@example("")
@example("fixture-mac")
@example("fixture-mac\n")
@example("a" * 33)
def test_fuzz_is_valid_label(text: str) -> None:
    """Never raises; accepts exactly the restated rule, and exactly derivation's fixed points."""
    valid = bm.is_valid_label(text)
    assert valid == (_RULE.match(text) is not None)
    assert valid == (bm.derive_label(text) == text)


@given(_TEXTS)
@example("Fixture-Mac.local")
@example("")
@example("é" * 40)
def test_fuzz_derive_label(text: str) -> None:
    """Whatever the host name, the label is valid and derivation leaves it as it is."""
    label = bm.derive_label(text)
    assert _RULE.match(label) is not None, label
    assert bm.derive_label(label) == label


class MachinePropertiesRunUnderUnittestTest(unittest.TestCase):
    """Runs the module-level properties, which unittest would otherwise never collect."""

    def test_is_valid_label_property_holds(self) -> None:
        test_fuzz_is_valid_label()

    def test_derive_label_property_holds(self) -> None:
        test_fuzz_derive_label()
