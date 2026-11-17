"""Which given options a mode's row does not consume, read from the parser itself.

The CLI refuses such an option rather than ignoring it (see the entry script's docstring);
this module only finds them, so the table of rows stays beside the handlers it describes.
"""

from __future__ import annotations

import argparse

#: Parser dests no row names: --help exits while parsing, and --mode selects the row.
UNCHECKED_DESTS = frozenset({"help", "mode"})


def unconsumed_options(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    consumed: "frozenset[str]",
) -> list[str]:
    """Each option given a value other than its default whose dest *consumed* lacks.

    Compared with the default rather than tracked as "given", so an explicit value equal to
    the default, which changes nothing, passes.
    """
    return [
        action.option_strings[0]
        for action in _checkable_actions(parser)
        if action.dest not in consumed
        and getattr(args, action.dest) != parser.get_default(action.dest)
    ]


def _checkable_actions(parser: argparse.ArgumentParser) -> list[argparse.Action]:
    """Every option *parser* defines but the unchecked ones, read from the parser itself.

    Read rather than listed, so an option added later is checked too, and refused by every
    row until one names it. argparse offers no public list of a parser's actions;
    ``_actions`` is where every ``add_argument`` has put them since argparse joined the
    standard library.
    """
    return [action for action in parser._actions if action.dest not in UNCHECKED_DESTS]
