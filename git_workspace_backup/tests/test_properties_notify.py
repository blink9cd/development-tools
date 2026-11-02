"""Grouped suite: property tests for backup_notify's bounded notification text.

The conventions are test_properties.py's: every ``# fuzz-target`` subject has a module-level
``@given`` ``test_fuzz_<name>`` here, run by the TestCase at the bottom, and the shared
deterministic profile is loaded before any ``@given`` is defined. Kept out of that file,
which is near the length cap.

What they are for: the text is a failure reason, which can hold anything a failing command
printed, and it becomes an argv item. It must never carry a NUL (launching a process with
one raises instead of exiting), never exceed the limit, and never lose the start of a reason,
which is where the outcome and the first failed item sit. The long arm exists because text
near the limit is where an off-by-one lives, and short random text never reaches it.
"""

from __future__ import annotations

import unittest

from hypothesis import example, given
from hypothesis import strategies as st

import _fixtures as fx  # noqa: F401 - installs the src/ bootstrap
import _hypothesis_profile  # noqa: F401 - loads the profile before any @given below

import backup_notify as bn

_NEAR_LIMIT = st.text(
    alphabet=st.sampled_from("ab\x00"),
    min_size=bn.MESSAGE_LIMIT - 2,
    max_size=bn.MESSAGE_LIMIT + 4,
)


@given(st.one_of(st.text(), _NEAR_LIMIT))
@example("a" * bn.MESSAGE_LIMIT)
@example("a" * (bn.MESSAGE_LIMIT + 1))
@example("\x00" * (bn.MESSAGE_LIMIT + 1))
def test_fuzz_bounded_text(text: str) -> None:
    """Never a NUL, never over the limit, and the start of the text always kept."""
    bounded = bn.bounded_text(text)
    clean = text.replace("\x00", "")
    assert "\x00" not in bounded
    assert len(bounded) <= bn.MESSAGE_LIMIT
    if len(clean) <= bn.MESSAGE_LIMIT:
        assert bounded == clean
    else:
        assert len(bounded) == bn.MESSAGE_LIMIT
        assert bounded.endswith(bn.ELLIPSIS)
        assert clean.startswith(bounded[: -len(bn.ELLIPSIS)])


class NotifyPropertiesRunUnderUnittestTest(unittest.TestCase):
    """Runs the module-level property, which unittest would otherwise never collect."""

    def test_bounded_text_property_holds(self) -> None:
        test_fuzz_bounded_text()


if __name__ == "__main__":
    unittest.main()
