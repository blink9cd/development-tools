#!/usr/bin/env python3
"""Branch-complete tests for the shared ``--help`` formatter.

Why these tests do not assert on ``format_help()`` for the required-option case
-----------------------------------------------------------------------------
The shim exists for CPython < 3.13, which stamps a misleading ``(default: None)`` onto
*required* options. That bug is FIXED upstream, so on an interpreter newer than the fix
(3.13 or later) the stock ``ArgumentDefaultsHelpFormatter`` already behaves exactly like
the shim. Measured: for a
required action with either ``default=None`` or ``default=7``, the stock hook returns
``action.help`` unchanged, identical to the shim's output.

A rendering test of the required case is therefore **vacuous on this interpreter** -- it
passes with the override deleted. (It was written that way first and confirmed to pass
against a no-op stub, which is why it is not written that way now.)

So the required-option contract is pinned against :class:`_LegacyDefaultsBase`, a
stand-in that appends the default note *unconditionally*, the way old argparse did. That
tests the shim's OWN logic rather than CPython's, and it stays meaningful on every
interpreter -- including the 3.12 floor the shim is actually for.
"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import help_formatter  # noqa: E402 - import follows the sys.path bootstrap above

_DEFAULT_NOTE = " (default: %(default)s)"


class _LegacyDefaultsBase(argparse.ArgumentDefaultsHelpFormatter):
    """Stands in for argparse < 3.13: appends the default note to EVERY action.

    Only the required-option branch differs between old and new argparse, so
    reproducing the old unconditional append is enough to make the shim's override
    observable on a modern interpreter.
    """

    def _get_help_string(self, action: argparse.Action) -> str | None:
        return (action.help or "") + _DEFAULT_NOTE


class _ShimOverLegacy(help_formatter.ReqAwareDefaultsFormatter, _LegacyDefaultsBase):
    """The shim layered over the legacy base, so ``super()`` resolves to the latter."""


def _action(*, required: bool, default: object = None) -> argparse.Action:
    """One parser action with the requested required/default shape."""
    parser = argparse.ArgumentParser(prog="demo")
    return parser.add_argument(
        "--flag", required=required, default=default, help="Help text."
    )


class LegacyInterceptTests(unittest.TestCase):
    """The load-bearing contract: strip the note on required, delegate otherwise."""

    def setUp(self) -> None:
        self.formatter = _ShimOverLegacy(prog="demo")

    def test_required_action_loses_the_legacy_default_note(self) -> None:
        rendered = self.formatter._get_help_string(_action(required=True))
        self.assertEqual(rendered, "Help text.")

    def test_required_action_with_a_real_default_also_loses_it(self) -> None:
        # A required option CAN carry a default (it is unreachable, but argparse
        # allows it); the note is misleading there for the same reason.
        rendered = self.formatter._get_help_string(_action(required=True, default=7))
        self.assertEqual(rendered, "Help text.")

    def test_optional_action_keeps_the_delegated_note(self) -> None:
        rendered = self.formatter._get_help_string(_action(required=False, default=7))
        self.assertEqual(rendered, "Help text." + _DEFAULT_NOTE)

    def test_help_is_passed_through_unchanged_when_absent(self) -> None:
        parser = argparse.ArgumentParser(prog="demo")
        action = parser.add_argument("--flag", required=True)
        self.assertIsNone(self.formatter._get_help_string(action))


class RenderedOutputTests(unittest.TestCase):
    """Real ``format_help()`` behavior, on whatever interpreter is running.

    These document the observable end result. They are deliberately NOT the pin for
    the required case -- see the module docstring.
    """

    def test_optional_shows_its_live_default(self) -> None:
        parser = argparse.ArgumentParser(
            prog="demo", formatter_class=help_formatter.ReqAwareDefaultsFormatter
        )
        parser.add_argument("--depth", default=99, help="How deep to walk.")
        # Derived from default=, never hardcoded into help= -- changing one moves --help.
        self.assertIn("default: 99", parser.format_help())

    def test_required_option_never_shows_a_default(self) -> None:
        parser = argparse.ArgumentParser(
            prog="demo", formatter_class=help_formatter.ReqAwareDefaultsFormatter
        )
        parser.add_argument("--path", required=True, help="Target to check.")
        self.assertNotIn("default:", parser.format_help())

    def test_suppressed_default_stays_suppressed(self) -> None:
        parser = argparse.ArgumentParser(
            prog="demo", formatter_class=help_formatter.ReqAwareDefaultsFormatter
        )
        parser.add_argument("--quiet", default=argparse.SUPPRESS, help="Be quiet.")
        self.assertNotIn("default:", parser.format_help())


class ExportTests(unittest.TestCase):
    """The public name is what every CLI binds to, so pin its shape."""

    def test_is_an_argparse_defaults_formatter(self) -> None:
        self.assertTrue(
            issubclass(
                help_formatter.ReqAwareDefaultsFormatter,
                argparse.ArgumentDefaultsHelpFormatter,
            )
        )


if __name__ == "__main__":
    unittest.main()
