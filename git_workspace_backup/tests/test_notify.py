#!/usr/bin/env python3
"""The failure notification: text travels as arguments, bounded, and a refusal is reported.

osascript never runs for real here: every call is answered by a FakeRunner route, which
refuses an unrouted osascript outright.
"""

from __future__ import annotations

import unittest
from collections.abc import Sequence

import _fixtures as fx

import backup_notify as bn
from backup_proc import OSASCRIPT_TIMEOUT_S


class NotifyTests(unittest.TestCase):
    def test_title_and_message_travel_as_arguments_after_the_options_end(self) -> None:
        calls: list[list[str]] = []

        def accept(argv: Sequence[str]) -> "tuple[int, str, str]":
            calls.append(list(argv))
            return 0, "", ""

        runner = fx.FakeRunner({"osascript": accept})
        message = '-a "quoted" reason'
        self.assertTrue(bn.notify("t", message, run=runner))
        [argv] = calls
        # A dash-leading message after "--" is data; without the "--" osascript refuses it
        # as an option and posts nothing.
        self.assertEqual(argv[-3:], ["--", "t", message])
        self.assertNotIn("quoted", " ".join(argv[:-3]))
        self.assertEqual(argv[0], bn.OSASCRIPT)

    def test_the_bound_is_osascripts_timeout(self) -> None:
        bounds: list[float] = []

        def run(
            argv: Sequence[str], cwd: object, timeout: float
        ) -> "tuple[int, str, str]":
            bounds.append(timeout)
            return 0, "", ""

        bn.notify("t", "m", run=run)
        self.assertEqual(bounds, [OSASCRIPT_TIMEOUT_S])

    def test_a_refused_notification_reports_false(self) -> None:
        failing = fx.FakeRunner({"osascript": lambda argv: (1, "", "denied")})
        self.assertFalse(bn.notify("t", "m", run=failing))

    def test_a_long_message_is_cut_to_the_limit_with_an_ellipsis(self) -> None:
        calls: list[list[str]] = []

        def accept(argv: Sequence[str]) -> "tuple[int, str, str]":
            calls.append(list(argv))
            return 0, "", ""

        long_message = "x" * (bn.MESSAGE_LIMIT * 3)
        bn.notify("t", long_message, run=fx.FakeRunner({"osascript": accept}))
        posted = calls[0][-1]
        self.assertEqual(len(posted), bn.MESSAGE_LIMIT)
        self.assertTrue(posted.endswith(bn.ELLIPSIS))
        self.assertEqual(posted[: -len(bn.ELLIPSIS)], long_message[: len(posted) - 1])

    def test_bounded_text_keeps_what_fits_and_drops_nul(self) -> None:
        fits = "y" * bn.MESSAGE_LIMIT
        self.assertEqual(bn.bounded_text(fits), fits)
        self.assertEqual(bn.bounded_text(fits + "y")[-1], bn.ELLIPSIS)
        # No argv item can hold a NUL: the launch would raise rather than exit non-zero.
        self.assertEqual(bn.bounded_text("a\x00b"), "ab")


if __name__ == "__main__":
    unittest.main()
