"""Grouped suite: property tests for backup_bundle's parsers of git output.

The conventions are test_properties.py's: every ``# fuzz-target`` subject has a module-level
``@given`` ``test_fuzz_<name>`` here, run by the TestCase at the bottom, and the shared
deterministic profile is loaded before any ``@given`` is defined. Kept out of that file,
which is near the length cap.

What they are for: a bundle is judged complete by comparing git's own listings, and a
worktree head's sha is handed back to git as an argument. So each parser must never raise
and must keep exactly the records of the shape git writes (a full lowercase object id,
never text that could read as an option; a ref name with no space or control character; a
worktree name of one component) from output mixed with anything else. Coverage is carried
by test_bundle.py.

Each property compares the parser with the rule restated as a regular expression rather than
with the parser's own predicates, so a wrong predicate cannot also be the thing that judges
it. Record-shaped arms are in every input pool because random text essentially never forms
a well-formed record, and a property that never reaches acceptance guards nothing.
"""

from __future__ import annotations

import re
import unittest

from hypothesis import given
from hypothesis import strategies as st

import _fixtures as fx  # noqa: F401 - installs the src/ bootstrap
import _hypothesis_profile  # noqa: F401 - loads the profile before any @given below

import backup_bundle as bb

_HEX = "0123456789abcdef"
#: Full SHA-1 and SHA-256 object names.
_OBJECT_IDS = st.sampled_from((40, 64)).flatmap(
    lambda size: st.text(alphabet=_HEX, min_size=size, max_size=size)
)
#: Names with no space, control character or DEL.
_REF_NAMES = st.text(
    alphabet=st.characters(min_codepoint=0x21, exclude_characters="\x7f"),
    min_size=1,
    max_size=16,
)
_NOISE = st.text(max_size=24)
#: A ref name with one character git forbids inside it: a space, a tab, a control
#: character or DEL. Measured: with only general text in these arms, ASCII text included,
#: a mutant letting a space into a name left the ref and heads properties green, since
#: such text rarely puts one inside an otherwise well-formed record. Every name drawn here
#: carries one by construction.
_BROKEN_NAMES = st.builds(
    "{}{}{}".format, _REF_NAMES, st.sampled_from(" \t\x1f\x7f"), _REF_NAMES
)

_REF_LINE = re.compile(r"refs/[^\x00-\x20\x7f]*")
_HEAD_LINE = re.compile(r"([0-9a-f]{40}|[0-9a-f]{64}) ([^\x00-\x20\x7f]+)")
_WORKTREE_RECORD = re.compile(r"worktree (.+)", re.DOTALL)
_STASH_LINE = re.compile(r"([0-9a-f]{40}|[0-9a-f]{64})\x00(.*)", re.DOTALL)
_WORKTREE_REF = re.compile(r"worktrees/([^/]+)/HEAD")

_REF_OUTPUTS = st.lists(
    st.one_of(
        _REF_NAMES.map(lambda name: f"refs/{name}"),
        _BROKEN_NAMES.map(lambda name: f"refs/{name}"),
        _REF_NAMES,
        _NOISE,
    ),
    max_size=6,
).map("\n".join)
_HEAD_OUTPUTS = st.lists(
    st.one_of(
        st.builds("{} {}".format, _OBJECT_IDS, _REF_NAMES),
        st.builds("{} {}".format, _OBJECT_IDS, _BROKEN_NAMES),
        st.builds("{} {}".format, _OBJECT_IDS.map(lambda sha: sha[1:]), _REF_NAMES),
        _OBJECT_IDS,
        _NOISE,
    ),
    max_size=6,
).map("\n".join)
_STASH_OUTPUTS = st.lists(
    st.one_of(
        st.builds("{}\0{}".format, _OBJECT_IDS, _NOISE),
        st.builds("{}\0{}".format, _OBJECT_IDS.map(str.upper), _NOISE),
        st.builds("{} {}".format, _OBJECT_IDS, _NOISE),
        _NOISE,
    ),
    max_size=6,
).map("\n".join)
_WORKTREE_OUTPUTS = st.lists(
    st.one_of(
        _NOISE.map(lambda path: f"worktree {path}"),
        _OBJECT_IDS.map(lambda sha: f"HEAD {sha}"),
        st.sampled_from(["", "detached", "bare", "worktree", "worktree "]),
        _NOISE,
    ),
    max_size=8,
).map("\0".join)
_WORKTREE_REFS = st.one_of(
    _NOISE.map(lambda name: f"worktrees/{name}/HEAD"),
    st.sampled_from(
        ["worktrees/HEAD", "worktrees//HEAD", "worktrees/a/b/HEAD", "HEAD"]
    ),
    _NOISE,
)


@given(_REF_OUTPUTS)
def test_fuzz_parse_refs(out: str) -> None:
    """Never raises; lists exactly the lines that are ref names under refs/, sorted."""
    expected = sorted(line for line in out.split("\n") if _REF_LINE.fullmatch(line))
    assert bb.parse_refs(out) == tuple(expected)


@given(_HEAD_OUTPUTS)
def test_fuzz_parse_heads(out: str) -> None:
    """Never raises; maps exactly the well-formed lines, a later line winning a name."""
    expected: dict[str, str] = {}
    for line in out.split("\n"):
        match = _HEAD_LINE.fullmatch(line)
        if match:
            expected[match.group(2)] = match.group(1)
    assert bb.parse_heads(out) == expected


@given(_STASH_OUTPUTS)
def test_fuzz_parse_stash_log(out: str) -> None:
    """Never raises; keeps exactly the full-sha NUL-separated lines, oldest (last) first."""
    matches = [_STASH_LINE.fullmatch(line) for line in out.split("\n")]
    expected = [(match.group(1), match.group(2)) for match in matches if match]
    parsed = [(entry.sha, entry.message) for entry in bb.parse_stash_log(out)]
    assert parsed == expected[::-1]


@given(_WORKTREE_OUTPUTS)
def test_fuzz_parse_worktree_paths(out: str) -> None:
    """Never raises; lists exactly the non-empty ``worktree`` records' paths, in order."""
    expected = [
        match.group(1)
        for match in map(_WORKTREE_RECORD.fullmatch, out.split("\0"))
        if match
    ]
    assert list(bb.parse_worktree_paths(out)) == expected


@given(_WORKTREE_REFS)
def test_fuzz_worktree_name(ref: str) -> None:
    """Never raises; names exactly the ``worktrees/<one component>/HEAD`` refs."""
    match = _WORKTREE_REF.fullmatch(ref)
    assert bb.worktree_name(ref) == (match.group(1) if match else None)


class BundlePropertiesRunUnderUnittestTest(unittest.TestCase):
    """Runs the module-level properties, which unittest would otherwise never collect."""

    def test_ref_parser_property_holds(self) -> None:
        test_fuzz_parse_refs()

    def test_heads_parser_property_holds(self) -> None:
        test_fuzz_parse_heads()

    def test_stash_log_parser_property_holds(self) -> None:
        test_fuzz_parse_stash_log()

    def test_worktree_listing_parser_property_holds(self) -> None:
        test_fuzz_parse_worktree_paths()

    def test_worktree_name_property_holds(self) -> None:
        test_fuzz_worktree_name()
