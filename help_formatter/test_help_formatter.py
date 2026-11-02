#!/usr/bin/env python3
"""Branch-complete tests for the shared ``--help`` formatter.

Why these tests do not assert on ``format_help()`` for the required-option case
-----------------------------------------------------------------------------
The shim exists for CPython < 3.13, which stamps a misleading ``(default: None)`` onto
*required* options. That bug is fixed upstream, so on any interpreter newer than the fix
the stock ``ArgumentDefaultsHelpFormatter`` already behaves exactly like the shim.
Measured: for a required action with either ``default=None`` or ``default=7``, the stock
hook returns ``action.help`` unchanged, identical to the shim's output.

A rendering test of the required case is therefore vacuous on such an interpreter: it
passes with the override deleted. (It was written that way first and confirmed to pass
against a no-op stub, which is why it is not written that way now.)

So the required-option contract is pinned against :class:`_LegacyDefaultsBase`, a
stand-in that appends the default note *unconditionally*, the way old argparse did. That
tests the shim's own logic rather than CPython's, and it stays meaningful on every
interpreter, including the interpreters below 3.13 the shim is actually for, which the
test run may never use.
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
    """Stands in for argparse < 3.13: appends the default note to every action.

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
        # A required option can carry a default (it is unreachable, but argparse
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

    These document the observable end result. They are deliberately not the pin for
    the required case; see the module docstring.
    """

    def test_optional_shows_its_live_default(self) -> None:
        parser = argparse.ArgumentParser(
            prog="demo", formatter_class=help_formatter.ReqAwareDefaultsFormatter
        )
        parser.add_argument("--depth", default=99, help="How deep to walk.")
        # Derived from default=, never hardcoded into help=, so changing one moves --help.
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


class _RawShimOverLegacy(help_formatter.RawNoneHidingFormatter, _LegacyDefaultsBase):
    """The raw, None-hiding shim over the legacy base, so its required case is observable."""


class RawNoneHidingLegacyTests(unittest.TestCase):
    """The None-hiding formatter's own logic, pinned against the legacy stand-in."""

    def setUp(self) -> None:
        self.formatter = _RawShimOverLegacy(prog="demo")

    def test_a_none_default_is_never_shown(self) -> None:
        # None is no value a person could type, so the note would say nothing true.
        rendered = self.formatter._get_help_string(_action(required=False))
        self.assertEqual(rendered, "Help text.")

    def test_a_required_option_with_a_real_default_loses_the_note(self) -> None:
        # Not None, so only the inherited required-option override can strip it.
        rendered = self.formatter._get_help_string(_action(required=True, default=7))
        self.assertEqual(rendered, "Help text.")

    def test_a_real_default_on_an_optional_keeps_the_note(self) -> None:
        rendered = self.formatter._get_help_string(_action(required=False, default=7))
        self.assertEqual(rendered, "Help text." + _DEFAULT_NOTE)


class RawNoneHidingRenderedTests(unittest.TestCase):
    """``format_help()`` under the None-hiding formatter, on the running interpreter."""

    def _parser(
        self, description: str | None = None, epilog: str | None = None
    ) -> argparse.ArgumentParser:
        return argparse.ArgumentParser(
            prog="demo",
            description=description,
            epilog=epilog,
            formatter_class=help_formatter.RawNoneHidingFormatter,
        )

    def test_a_real_default_is_shown(self) -> None:
        parser = self._parser()
        parser.add_argument("--depth", default=99, help="How deep to walk.")
        self.assertIn("(default: 99)", parser.format_help())

    def test_a_none_default_is_not_shown(self) -> None:
        parser = self._parser()
        parser.add_argument("--model", help="Leave out to use the first found.")
        self.assertNotIn("default:", parser.format_help())

    def test_the_epilog_and_description_keep_their_line_breaks(self) -> None:
        epilog = "exit codes:\n  0  done\n  1  a file failed"
        parser = self._parser(description="first line\n  second line", epilog=epilog)
        rendered = parser.format_help()
        self.assertIn(epilog, rendered)
        self.assertIn("first line\n  second line", rendered)


class _UnsetShimOverLegacy(help_formatter.UnsetHidingFormatter, _LegacyDefaultsBase):
    """The unset-hiding shim over the legacy base, so its required case is observable."""


class UnsetHidingLegacyTests(unittest.TestCase):
    """The unset-hiding formatter's own logic, pinned against the legacy stand-in."""

    def setUp(self) -> None:
        self.formatter = _UnsetShimOverLegacy(prog="demo")

    def test_a_none_default_is_never_shown(self) -> None:
        rendered = self.formatter._get_help_string(_action(required=False))
        self.assertEqual(rendered, "Help text.")

    def test_an_empty_list_default_is_never_shown(self) -> None:
        # An append/extend option's [] means "none given", which no one can type either.
        rendered = self.formatter._get_help_string(_action(required=False, default=[]))
        self.assertEqual(rendered, "Help text.")

    def test_an_empty_tuple_default_is_never_shown(self) -> None:
        rendered = self.formatter._get_help_string(_action(required=False, default=()))
        self.assertEqual(rendered, "Help text.")

    def test_a_non_empty_list_default_keeps_the_note(self) -> None:
        rendered = self.formatter._get_help_string(
            _action(required=False, default=["a"])
        )
        self.assertEqual(rendered, "Help text." + _DEFAULT_NOTE)

    def test_an_empty_string_default_keeps_the_note(self) -> None:
        # "" is a value a person can type (--flag ''), unlike None or an empty list.
        rendered = self.formatter._get_help_string(_action(required=False, default=""))
        self.assertEqual(rendered, "Help text." + _DEFAULT_NOTE)

    def test_a_required_option_with_a_real_default_loses_the_note(self) -> None:
        rendered = self.formatter._get_help_string(_action(required=True, default=7))
        self.assertEqual(rendered, "Help text.")


class UnsetHidingRenderedTests(unittest.TestCase):
    """``format_help()`` under the unset-hiding formatter, on the running interpreter."""

    def _parser(self, epilog: str | None = None) -> argparse.ArgumentParser:
        return argparse.ArgumentParser(
            prog="demo",
            epilog=epilog,
            formatter_class=help_formatter.UnsetHidingFormatter,
        )

    def test_unset_defaults_are_not_shown_and_a_real_one_is(self) -> None:
        parser = self._parser()
        parser.add_argument("--model", help="Leave out to use the first found.")
        parser.add_argument("--skip", action="append", default=[], help="Skip one.")
        parser.add_argument("--depth", default=99, help="How deep to walk.")
        rendered = parser.format_help()
        self.assertNotIn("default: None", rendered)
        self.assertNotIn("default: []", rendered)
        self.assertIn("(default: 99)", rendered)

    def test_the_epilog_is_rewrapped_not_kept_raw(self) -> None:
        # The non-raw counterpart of RawNoneHidingFormatter: a prose epilog is filled.
        parser = self._parser(epilog="one\ntwo")
        self.assertIn("one two", parser.format_help())


class ExportTests(unittest.TestCase):
    """The public name is what every importer binds to, so pin its shape."""

    def test_is_an_argparse_defaults_formatter(self) -> None:
        self.assertTrue(
            issubclass(
                help_formatter.ReqAwareDefaultsFormatter,
                argparse.ArgumentDefaultsHelpFormatter,
            )
        )

    def test_the_raw_formatter_composes_both_bases(self) -> None:
        cls = help_formatter.RawNoneHidingFormatter
        self.assertTrue(issubclass(cls, help_formatter.ReqAwareDefaultsFormatter))
        self.assertTrue(issubclass(cls, argparse.RawDescriptionHelpFormatter))

    def test_the_unset_hiding_formatter_is_not_raw(self) -> None:
        cls = help_formatter.UnsetHidingFormatter
        self.assertTrue(issubclass(cls, help_formatter.ReqAwareDefaultsFormatter))
        self.assertFalse(issubclass(cls, argparse.RawDescriptionHelpFormatter))


if __name__ == "__main__":
    unittest.main()
