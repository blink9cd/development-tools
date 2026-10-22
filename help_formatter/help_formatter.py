#!/usr/bin/env python3
"""Own the one ``--help`` formatter this project's CLIs use.

Why this exists
---------------
Each CLI's ``--help`` shows every optional argument's default, derived from the live
``default=`` rather than hardcoded into ``help=`` text that drifts.
``argparse.ArgumentDefaultsHelpFormatter`` does that -- but on CPython < 3.13 it also
stamps a misleading ``(default: None)`` onto *required* options, which have no default a
caller could ever rely on. The documented workaround is a subclass whose
``_get_help_string`` returns ``action.help`` unchanged for a required action; this module
keeps it in one place rather than a copy per CLI.

The version trap this module is really about
--------------------------------------------
**The bug is fixed upstream, and an interpreter newer than the fix hides it.** Measured
on 3.14: the stock formatter already returns ``action.help`` unchanged for a required
action, with either ``default=None`` or a real default. So on 3.13 and later this shim is
a no-op, and any test that asserts on rendered ``--help`` output passes just as well with
the override deleted.

It is still needed: this project's floor is Python 3.12, below the fix. The shim earns its
keep only on the older interpreters, which is precisely why it must be pinned by a test
that does not depend on the local interpreter's behavior. See ``test_help_formatter.py``:
the required-option contract is asserted against a stand-in for old argparse, not against
CPython's current output.

Entry point assumptions
-----------------------
Importers bind :class:`ReqAwareDefaultsFormatter` as their parser's ``formatter_class``
and nothing else. The module is pure -- no I/O, no filesystem access, no config -- so it
carries no ``stdio_utf8`` dependency and cannot fail at import time.
"""

from __future__ import annotations

import argparse


class ReqAwareDefaultsFormatter(argparse.ArgumentDefaultsHelpFormatter):
    """Show optionals' defaults in ``--help``; drop the bogus one on required options.

    Behaves identically to its base class on CPython >= 3.13, where the required-option
    bug is already fixed; the override is what keeps ``--help`` honest on the 3.12
    floor.
    """

    def _get_help_string(self, action: argparse.Action) -> str | None:
        """Return *action*'s help text, suppressing the default note when required.

        A required option cannot fall back to its default, so advertising one is
        actively misleading. Everything else delegates, keeping the base class's
        ``SUPPRESS`` handling and its ``%(default)s`` interpolation intact.
        """
        if action.required:
            return action.help
        return super()._get_help_string(action)
