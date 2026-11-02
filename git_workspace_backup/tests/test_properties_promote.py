"""Grouped suite: property tests for backup_promote's snapshot names.

The conventions are test_properties.py's: every ``# fuzz-target`` subject has a module-level
``@given`` ``test_fuzz_<name>`` here, run by the TestCase at the bottom, and the shared
deterministic profile is loaded before any ``@given`` is defined. Kept out of that file,
which is near the length cap.

What they are for: a folder counts as a snapshot, and so can be listed, judged and pruned,
only when its name parses. ``strptime`` alone accepts names the tool never writes (unpadded
fields, non-ASCII digits), so parsing must accept exactly the canonical form and never raise
on any other text; and every name the tool writes must parse back to the moment it records.
Example cases are carried by test_promote.py.

The canonical form is restated here as a regular expression plus the calendar, rather than
calling the subject's own formatter, so a wrong formatter cannot also be the thing that judges
the parser. The dense and near-miss arms exist because random text essentially never forms a
well-formed name, and a property that never reaches acceptance guards nothing.

A name may end in ``-<machine>``, the label of the machine that wrote it; a bare name is one
written before machine identity, and names no machine. The label rule is restated too, for
the same reason, and the suffixed arms exist for the same reason the dense ones do.
"""

from __future__ import annotations

import re
import unittest
from datetime import datetime

from hypothesis import example, given
from hypothesis import strategies as st

import _fixtures as fx  # noqa: F401 - installs the src/ bootstrap
import _hypothesis_profile  # noqa: F401 - loads the profile before any @given below

import backup_promote as bp

#: ``YYYY-MM-DDTHHMMSS`` in ASCII digits, and nothing else.
_CANONICAL = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2})([0-9]{2})([0-9]{2})"
)
#: A machine label: lowercase ASCII letters and digits in runs joined by single hyphens.
_LABEL = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_MAX_LABEL = 32
#: Whether this platform's strftime zero-pads a year below 1000 to four digits. Where it
#: does not, such a year is written short and cannot be the four-digit field read back.
_PADS_SHORT_YEARS = datetime(5, 1, 1).strftime("%Y") == "0005"
_FIRST_PADDED_YEAR = 1000

#: Moments a snapshot name can record: whole seconds, naive local time.
_MOMENTS = st.datetimes(
    min_value=datetime(1, 1, 1), max_value=datetime(9999, 12, 31, 23, 59, 59)
).map(lambda moment: moment.replace(microsecond=0))
_WELL_FORMED = _MOMENTS.map(
    lambda m: (
        f"{m.year:04d}-{m.month:02d}-{m.day:02d}T{m.hour:02d}{m.minute:02d}{m.second:02d}"
    )
)


_LABELS = st.from_regex(r"[a-z0-9]{1,9}(-[a-z0-9]{1,9}){0,3}", fullmatch=True)
_SUFFIXED = st.builds(lambda name, label: f"{name}-{label}", _WELL_FORMED, _LABELS)


def _near_miss(name: str, index: int, junk: str) -> str:
    """*name* with the character at *index* dropped, or replaced by *junk* when given."""
    cut = index % (len(name) + 1)
    return name[:cut] + junk + name[cut + 1 :] if junk else name[:cut] + name[cut + 1 :]


_NEAR_MISSES = st.builds(
    _near_miss,
    _WELL_FORMED | _SUFFIXED,
    st.integers(min_value=0, max_value=40),
    st.sampled_from(["", "0", "9", "-", "T", " ", "\n", "x", "X", "/", "٠"]),
)
_DENSE = st.text(alphabet="0123456789-T", min_size=14, max_size=20)
_NAMES = st.one_of(_WELL_FORMED, _SUFFIXED, _NEAR_MISSES, _DENSE, st.text(max_size=24))


def _expected_parts(text: str) -> "tuple[datetime, str | None] | None":
    """The moment and machine *text* names when it is canonical, else None."""
    stamp, separator, label = text[:17], text[17:18], text[18:]
    moment = _expected_moment(stamp)
    if moment is None or (separator and separator != "-"):
        return None
    if not separator:
        return moment, None
    if len(label) > _MAX_LABEL or _LABEL.fullmatch(label) is None:
        return None
    return moment, label


def _expected_moment(text: str) -> "datetime | None":
    """The moment bare *text* names when it is canonical and on the calendar, else None."""
    match = _CANONICAL.fullmatch(text)
    if match is None:
        return None
    year, month, day, hour, minute, second = (int(field) for field in match.groups())
    try:
        moment = datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None
    if moment.year < _FIRST_PADDED_YEAR and not _PADS_SHORT_YEARS:
        return None
    return moment


@given(_NAMES)
@example("2026-09-14T060005")
@example("2026-9-14T060005")
@example("2026-09-14T60005")
@example("2026-09-14T060005\n")
@example("2026-02-29T060000")
@example("2024-02-29T060000")
@example("2026-09-14T060005-fixture-mac")
@example("2026-09-14T060005-")
def test_fuzz_parse_snapshot_name(text: str) -> None:
    """Never raises; a moment exactly for canonical names on the calendar."""
    expected = _expected_parts(text)
    assert bp.parse_snapshot_name(text) == (None if expected is None else expected[0])


@given(_NAMES)
@example("2026-09-14T060005-fixture-mac")
@example("2026-09-14T060005-" + "a" * 33)
@example("2026-09-14T060005-fixture--mac")
def test_fuzz_split_snapshot_name(text: str) -> None:
    """Never raises; the moment and machine exactly for canonical names, bare or suffixed."""
    parts = bp.split_snapshot_name(text)
    assert (None if parts is None else tuple(parts)) == _expected_parts(text)


@given(_MOMENTS.filter(lambda m: m.year >= _FIRST_PADDED_YEAR or _PADS_SHORT_YEARS))
@example(datetime(2026, 9, 14, 6, 0, 5))
@example(datetime(9999, 12, 31, 23, 59, 59))
def test_fuzz_snapshot_name(moment: datetime) -> None:
    """Every name written is canonical, and parses back to the moment it records."""
    name = bp.snapshot_name(moment)
    assert _CANONICAL.fullmatch(name) is not None, name
    assert bp.parse_snapshot_name(name) == moment


_PADDED_MOMENTS = _MOMENTS.filter(
    lambda m: m.year >= _FIRST_PADDED_YEAR or _PADS_SHORT_YEARS
)


@given(_PADDED_MOMENTS, _PADDED_MOMENTS, _LABELS | st.none(), _LABELS | st.none())
def test_names_sort_by_moment_whatever_the_machine(
    first: datetime, second: datetime, one: "str | None", other: "str | None"
) -> None:
    """A name carrying a machine parses back to both, and names sort by moment first."""
    one = one if one is None or len(one) <= _MAX_LABEL else None
    other = other if other is None or len(other) <= _MAX_LABEL else None
    named = bp.snapshot_name(first, one)
    assert tuple(bp.split_snapshot_name(named) or ()) == (first, one)
    if first < second:
        assert named < bp.snapshot_name(second, other)


class PromotePropertiesRunUnderUnittestTest(unittest.TestCase):
    """Runs the module-level properties, which unittest would otherwise never collect."""

    def test_parse_snapshot_name_property_holds(self) -> None:
        test_fuzz_parse_snapshot_name()

    def test_snapshot_name_property_holds(self) -> None:
        test_fuzz_snapshot_name()

    def test_split_snapshot_name_property_holds(self) -> None:
        test_fuzz_split_snapshot_name()

    def test_names_sort_by_moment_property_holds(self) -> None:
        test_names_sort_by_moment_whatever_the_machine()
