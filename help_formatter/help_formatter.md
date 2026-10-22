# help_formatter.py

The one `--help` formatter this project's argparse CLIs use: optionals show their live
default, required options do not show a bogus one.

## Why it exists

Each CLI's `--help` shows every optional argument's **default**, auto-derived from the live
`default=` rather than hardcoded into `help=` text that drifts.
`argparse.ArgumentDefaultsHelpFormatter` does exactly that — but on CPython < 3.13 it also
stamps a misleading `(default: None)` onto *required* options, which have no default a
caller could ever use. The documented workaround is a subclass overriding
`_get_help_string`, and this module is that subclass, kept in one place rather than copied
into each CLI.

## The version trap — read this before writing a test

**The upstream bug is fixed, and an interpreter newer than the fix hides it.** Measured on
CPython 3.14, the *stock* formatter already returns `action.help` unchanged for a required
action, whether its default is `None` or a real value:

| Action | Stock `_get_help_string` on 3.14 |
| --- | --- |
| `required=True, default=None` | `'H'` |
| `required=True, default=7` | `'H'` |
| `required=False, default=7` | `'H (default: %(default)s)'` |

So on 3.13 and later this shim is a **no-op**, and a test asserting on rendered `--help`
output for the required case **passes with the override deleted**. That was confirmed
empirically: the first version of this suite was written that way and went green against
a stub that inherited unchanged.

It is still needed, because this project's floor is Python 3.12, below the fix. The shim
therefore earns its keep only on the older interpreters, which is why
`test_help_formatter.py` pins the required-option contract against `_LegacyDefaultsBase` —
a stand-in that appends the note unconditionally, the way old argparse did — instead of
against CPython's current output.

**Corollary worth carrying elsewhere:** a compatibility shim for a *fixed* upstream bug
cannot be behaviorally tested on the fixed runtime. Its tests must target the shim's own
contract, or they are vacuous at 100% coverage.

## Usage

```python
import help_formatter

parser = argparse.ArgumentParser(
    formatter_class=help_formatter.ReqAwareDefaultsFormatter,
)
```

Each CLI reaches it through the same `sys.path` bootstrap that finds `stdio_utf8`
(`../stdio_utf8/stdio_utf8.md`, "How this project's tools import it").

## Public surface

| Name | Meaning |
| --- | --- |
| `ReqAwareDefaultsFormatter` | `argparse.ArgumentDefaultsHelpFormatter` subclass; suppresses the default note on required actions and delegates otherwise (preserving `SUPPRESS` handling and `%(default)s` interpolation). |

## Notes

- The module is **pure** — no I/O, no filesystem access, no config — so it carries no
  `stdio_utf8` dependency and cannot fail at import time.
- It ships no `samples/`: there is no input→output transform to run by hand, so it is a
  shared library rather than a tool.
