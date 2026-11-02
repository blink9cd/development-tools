# help_formatter.py

One shared `--help` formatter for argparse CLIs: optionals show their live default, and
required options do not show a bogus one.

## Why it exists

A CLI's `--help` should show each optional argument's default, derived from the live
`default=` rather than hardcoded into `help=` text that drifts.
`argparse.ArgumentDefaultsHelpFormatter` does exactly that, but on CPython < 3.13 it also
stamps a misleading `(default: None)` onto *required* options, which have no default a
caller could ever use. The documented workaround is a subclass overriding
`_get_help_string`. This module is that subclass, kept in one place rather than copied into
each CLI, so a change to the workaround is one edit instead of one per copy.

## The version trap: read this before writing a test

The upstream bug is fixed, and an interpreter newer than the fix hides it. Measured on
CPython 3.14, the *stock* formatter already returns `action.help` unchanged for a required
action, whether its default is `None` or a real value:

| Action | Stock `_get_help_string` on 3.14 |
| --- | --- |
| `required=True, default=None` | `'H'` |
| `required=True, default=7` | `'H'` |
| `required=False, default=7` | `'H (default: %(default)s)'` |

So on 3.13 and later this shim is a no-op, and a test asserting on rendered `--help` output
for the required case passes with the override deleted. That was confirmed empirically: the
first version of this suite was written that way and went green against a stub that
inherited unchanged.

It is still needed wherever the code must also run below 3.13, where the bug is unfixed. The shim
therefore earns its keep only on those older interpreters, which is why
`test_help_formatter.py` pins the required-option contract against `_LegacyDefaultsBase` (a
stand-in that appends the note unconditionally, the way old argparse did) instead of against
CPython's current output.

Corollary: a compatibility shim for a *fixed* upstream bug cannot be behaviorally tested
on the fixed runtime. Its tests must target the shim's own contract, or they are vacuous at
100% coverage.

## Usage

```python
import help_formatter

parser = argparse.ArgumentParser(
    formatter_class=help_formatter.ReqAwareDefaultsFormatter,
)
```

Importers reach it by putting this module's folder on `sys.path`, the same way they reach any
other sibling helper.

## Public surface

| Name | Meaning |
| --- | --- |
| `ReqAwareDefaultsFormatter` | `argparse.ArgumentDefaultsHelpFormatter` subclass; suppresses the default note on required actions and delegates otherwise (preserving `SUPPRESS` handling and `%(default)s` interpolation). |
| `RawNoneHidingFormatter` | `ReqAwareDefaultsFormatter` composed with `argparse.RawDescriptionHelpFormatter`; shows real defaults, hides them for required options and for a `None` default, and keeps the description's and epilog's line breaks. |
| `UnsetHidingFormatter` | `ReqAwareDefaultsFormatter` subclass; shows real defaults, hides them for required options and for an unset default (`None`, or an empty list or tuple), and rewraps the epilog as prose. |

`RawNoneHidingFormatter` is for a CLI whose epilog is laid out by hand (a prerequisites
list, an exit-code table) and whose `None` defaults mean "not given". `None` is no value a
person could type, so `(default: None)` would say nothing true; such an option's help
should say instead what leaving it out means. The two bases override different methods,
so they compose, and a non-`None` default still passes through the required-option
override. Its tests pin the `None` and required-option logic against the same legacy
stand-in as the base class, and check the kept line breaks through `format_help()` on the
running interpreter.

`UnsetHidingFormatter` is the non-raw counterpart, for a CLI whose epilog is prose that
argparse may rewrap. It also hides an empty list or tuple default, which is what an
`append` or `extend` option holds before any value is given: `(default: [])` says no more
than `(default: None)` does. An empty string is still shown, because `--flag ''` can be
typed. Its tests pin the same logic against the legacy stand-in, and check through
`format_help()` that the unset notes are gone and the epilog is rewrapped.

## Notes

- The module is pure (no I/O, no filesystem access, no config), so it has no dependencies
  and cannot fail at import time.
- It ships no `samples/`: there is no input-to-output transform to run by hand, so it is a
  shared library rather than a tool.
