"""Grouped suite: property test for backup_verify's worktree-head refspec builder.

The conventions are test_properties.py's: the ``# fuzz-target`` subject has a module-level
``@given`` ``test_fuzz_<name>`` here, run by the TestCase at the bottom, and the shared
deterministic profile is loaded before any ``@given`` is defined. Kept out of that file,
which is near the length cap.

What it is FOR: a worktree head's name reaches verification from a manifest, and becomes a
refspec handed to ``git fetch``. A name holding ``*`` makes that refspec a glob, which
passes by fetching whatever worktree heads the bundle holds; other forbidden characters make
git refuse it or read it differently. So a refspec is built only for a name git's own
ref-component rules accept. Coverage is carried by test_verify_guards.py.

The rule is restated here as a regular expression rather than calling the subject's own
predicate, so a wrong predicate cannot also be the thing that judges it. The alphabet arm is
dense in ``.``, ``@``, ``{`` and the letters of ``lock``, because random text essentially
never forms ``..``, ``@{`` or a ``.lock`` suffix.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from hypothesis import example, given
from hypothesis import strategies as st

import _fixtures as fx
import _hypothesis_profile  # noqa: F401 - loads the profile before any @given below

import backup_verify as bv

#: git's rules for one ref component: no leading dot, no "..", no "@{", no ".lock" suffix,
#: and none of the control characters, space, DEL, ~ ^ : ? * [ \ or a slash.
_COMPONENT = re.compile(
    r"(?!\.)(?!.*\.\.)(?!.*@\{)(?!.*\.lock\Z)[^\x00-\x20\x7f~^:?*\[\\/]+", re.DOTALL
)
_DENSE = st.text(alphabet="ab.-_@{}lock", max_size=12)
_WITH_FORBIDDEN = st.builds(
    "{}{}{}".format, _DENSE, st.sampled_from(list(" \t\x00\x7f~^:?*[\\/")), _DENSE
)
_NAMES = st.one_of(_DENSE, _WITH_FORBIDDEN, st.text(max_size=12))


@given(_NAMES)
@example("wt")
@example("*")
@example("x.lock")
@example("x.")
@example("a@{b")
def test_fuzz_worktree_refspec(name: str) -> None:
    """Never raises; a refspec exactly for the names git's component rules accept."""
    expected = (
        f"worktrees/{name}/HEAD:refs/backup/worktrees/{name}/HEAD"
        if _COMPONENT.fullmatch(name)
        else None
    )
    assert bv.worktree_refspec(name) == expected


class VerifyPropertiesRunUnderUnittestTest(unittest.TestCase):
    """Runs the module-level property, which unittest would otherwise never collect."""

    def test_worktree_refspec_property_holds(self) -> None:
        test_fuzz_worktree_refspec()

    def test_both_sides_of_an_accepted_refspec_are_refs_git_accepts(self) -> None:
        """The regex restates the component rule, so it cannot see a rule about position.

        A ref may not END in a dot, which no component rule expresses: ``x.`` is a valid
        worktree name that was once built into a destination git refuses. So git itself
        judges both halves, for names that probe each rule's edge.
        """
        cwd = Path(tempfile.gettempdir())
        for name in ("wt", "x.", "a.b.", "-lead", "a@b", "a{b}", "x.lo", "caf\u00e9"):
            spec = bv.worktree_refspec(name)
            assert spec is not None, name
            for ref in spec.split(":"):
                with self.subTest(ref=ref):
                    try:
                        fx.git(cwd, "check-ref-format", ref)
                    except subprocess.CalledProcessError:
                        self.fail(f"git check-ref-format refuses {ref!r}")
