#!/usr/bin/env python3
"""What a resolved config entry MEANS: a subtree, and one that can rot.

Separated from the reading suites because these two functions touch no pyproject at
all. They encode the half of the contract that drifts most quietly -- every tool
re-decides "does this exclude cover the files below it?" and "what happens when a
configured path goes away?" in passing, and both wrong answers report PASS.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import pyproject_config as pc


class IsUnderTest(unittest.TestCase):
    """A configured entry names a subtree: itself and everything below it."""

    def test_exact_match(self) -> None:
        root = Path("/a/b").resolve()
        self.assertTrue(pc.is_under(Path("/a/b"), [root]))

    def test_a_descendant_is_under_it(self) -> None:
        root = Path("/a").resolve()
        self.assertTrue(pc.is_under(Path("/a/b/c"), [root]))

    def test_an_unrelated_path_is_not(self) -> None:
        self.assertFalse(pc.is_under(Path("/x/y"), [Path("/a").resolve()]))

    def test_an_ancestor_is_not_under_its_own_child(self) -> None:
        """Direction matters: excluding /a/b must not exclude /a."""
        self.assertFalse(pc.is_under(Path("/a"), [Path("/a/b").resolve()]))

    def test_no_roots_excludes_nothing(self) -> None:
        self.assertFalse(pc.is_under(Path("/a"), []))


class DeadPathsTest(unittest.TestCase):
    """Rotted config entries gate unless declared prospective (then reported)."""

    def test_splits_gating_from_prospective(self) -> None:
        # alive -> not dead; dead_gating -> dead and not prospective (gates);
        # dead_absent -> dead and prospective (reported, non-gating). One fixture
        # hits every branch of both partition comprehensions and the seam.
        alive = Path("/t/alive")
        dead_gating = Path("/t/dead")
        dead_absent = Path("/t/prospective")
        gating, absent = pc.dead_paths(
            frozenset({alive, dead_gating, dead_absent}),
            frozenset({dead_absent}),
            exists=lambda p: p == alive,
        )
        self.assertEqual(gating, [dead_gating])
        self.assertEqual(absent, [dead_absent])

    def test_all_alive_is_two_empty_lists(self) -> None:
        gating, absent = pc.dead_paths(
            [Path("/t/a")], frozenset(), exists=lambda _p: True
        )
        self.assertEqual((gating, absent), ([], []))

    def test_results_are_sorted_for_a_stable_report(self) -> None:
        gating, _absent = pc.dead_paths(
            [Path("/t/c"), Path("/t/a"), Path("/t/b")],
            frozenset(),
            exists=lambda _p: False,
        )
        self.assertEqual(gating, sorted(gating))
