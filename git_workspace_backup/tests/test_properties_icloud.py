"""Grouped suite: property tests for backup_icloud's upload-state parser.

The conventions are test_properties.py's: every ``# fuzz-target`` subject has a module-level
``@given`` ``test_fuzz_<name>`` here, run by the TestCase at the bottom, and the shared
deterministic profile is loaded before any ``@given`` is defined. Kept out of that file, which
is near the length cap.

What they are FOR: the parsed text is osascript's stdout, and a wrong parse is the one way
status could take an unknown upload state for "uploaded". So the parser must never raise, must
accept only exactly the expected number of entries, each exactly two JSON booleans, and what
it accepts must be what the text says. The answer-shaped arm exists because random text and
random JSON essentially never form a well-formed answer, and without it the acceptance path
the assertions guard is never reached.

The oracle restates the rule rather than calling the parser's own checks, so a wrong check
cannot also be the thing that judges it. Coverage is carried by test_icloud.py.
"""

from __future__ import annotations

import json
import unittest

from hypothesis import given
from hypothesis import strategies as st

import _fixtures as fx  # noqa: F401 - installs the src/ bootstrap
import _hypothesis_profile  # noqa: F401 - loads the profile before any @given below

import backup_icloud as bi

_ENTRY_KEYS = ("ubiquitous", "uploaded")
#: Scalars of every JSON kind, including the near misses a flag must refuse (0, 1, null).
_SCALARS = st.none() | st.booleans() | st.integers(-1, 2) | st.text(max_size=4)
_JSON = st.recursive(
    _SCALARS,
    lambda children: (
        st.lists(children, max_size=3)
        | st.dictionaries(st.text(max_size=10), children, max_size=3)
    ),
    max_leaves=10,
)
_FLAGS = st.booleans()
#: Items near an answer entry: well formed, any flag kind, a key missing, a key extra, or none.
_ITEMS = st.one_of(
    st.fixed_dictionaries({"ubiquitous": _FLAGS, "uploaded": _FLAGS}),
    st.fixed_dictionaries({"ubiquitous": _SCALARS, "uploaded": _SCALARS}),
    st.fixed_dictionaries({"ubiquitous": _FLAGS}),
    st.fixed_dictionaries({"ubiquitous": _FLAGS, "uploaded": _FLAGS, "extra": _FLAGS}),
    _SCALARS,
)
_ANSWERS = st.lists(_ITEMS, max_size=4)
_COUNTS = st.integers(-1, 5)


def _is_entry(item: object) -> bool:
    """Exactly the two keys, each holding a JSON boolean (never 0 or 1)."""
    return (
        isinstance(item, dict)
        and sorted(item) == sorted(_ENTRY_KEYS)
        and all(type(item[key]) is bool for key in _ENTRY_KEYS)
    )


@given(
    st.one_of(st.text(max_size=64), _JSON.map(json.dumps), _ANSWERS.map(json.dumps)),
    _COUNTS,
)
def test_fuzz_parse_upload_states(text: str, expected: int) -> None:
    """Never raises; an accepted answer is exactly *expected* states, each what the text says."""
    parsed = bi.parse_upload_states(text, expected)
    if parsed is None:
        return
    decoded = json.loads(text)
    assert len(parsed) == expected == len(decoded)
    assert parsed == tuple(
        bi.UploadState(item["ubiquitous"], item["uploaded"]) for item in decoded
    )
    assert all(
        type(state.ubiquitous) is bool and type(state.uploaded) is bool
        for state in parsed
    )


@given(_ANSWERS, _COUNTS)
def test_parse_upload_states_accepts_exactly_the_well_formed(
    items: "list[object]", expected: int
) -> None:
    """Accepted if and only if the count matches and every item is exactly two booleans."""
    well_formed = len(items) == expected and all(_is_entry(item) for item in items)
    parsed = bi.parse_upload_states(json.dumps(items), expected)
    assert (parsed is not None) == well_formed


class IcloudPropertiesRunUnderUnittestTest(unittest.TestCase):
    """Runs the module-level properties, which unittest would otherwise never collect."""

    def test_upload_state_parser_property_holds(self) -> None:
        test_fuzz_parse_upload_states()

    def test_upload_state_parser_accepts_exactly_the_well_formed(self) -> None:
        test_parse_upload_states_accepts_exactly_the_well_formed()


if __name__ == "__main__":
    unittest.main()
