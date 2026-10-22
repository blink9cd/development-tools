#!/usr/bin/env python3
"""The companion doc's two exhaustive tables agree with the code they restate.

A restated list goes stale silently: every name left in it still reads as true. So the table
of which modes take which options must say exactly what ``CONSUMED_OPTIONS`` says, and the
module map must name exactly the modules the private directory holds. An option, a mode or a
module added, removed or renamed fails here, instead of leaving the doc describing another tool.
"""

from __future__ import annotations

import itertools
import unittest
from pathlib import Path

import _fixtures as fx  # noqa: F401 - installs the src/ bootstrap

import backup_run
import git_workspace_backup as wb

#: The companion doc, named for the CLI beside it.
DOC = Path(wb.__file__).resolve().with_suffix(".md")
#: The directory the private modules are actually imported from.
PRIVATE_DIR = Path(backup_run.__file__).resolve().parent
#: The first header cell of each table under test; no other table in the doc opens with it.
MODE_TABLE = "Mode"
MODULE_TABLE = "Module"
#: A mode-table cell starting with this says the row consumes the column's option.
CONSUMED = "yes"
_TABLE_ROW = "|"


def _cells(line: str) -> "list[str]":
    """A Markdown table row's cells, without surrounding whitespace or backticks."""
    return [
        cell.strip().strip("`") for cell in line.strip().strip(_TABLE_ROW).split("|")
    ]


def _table(first_cell: str) -> "tuple[list[str], list[list[str]]]":
    """The header and body rows of the doc's table whose header opens with *first_cell*."""
    lines = DOC.read_text(encoding="utf-8").splitlines()
    start = next(
        index
        for index, line in enumerate(lines)
        if line.startswith(_TABLE_ROW) and _cells(line)[0] == first_cell
    )
    rows = itertools.takewhile(lambda line: line.startswith(_TABLE_ROW), lines[start:])
    header, _separator, *body = (_cells(line) for line in rows)
    return header, body


def _dest(option: str) -> str:
    """The dest argparse derives from a long *option*: dashes stripped, then made underscores."""
    return option.lstrip("-").replace("-", "_")


class CompanionDocTests(unittest.TestCase):
    def test_the_mode_table_states_the_options_each_mode_consumes(self) -> None:
        header, body = _table(MODE_TABLE)
        dests = [_dest(option) for option in header[1:]]
        documented = {
            row: frozenset(
                dest
                for dest, cell in zip(dests, cells, strict=True)
                if cell.startswith(CONSUMED)
            )
            for row, *cells in body
        }
        self.assertEqual(documented, dict(wb.CONSUMED_OPTIONS))

    def test_the_module_map_names_every_private_module(self) -> None:
        _, body = _table(MODULE_TABLE)
        documented = {row[0] for row in body}
        modules = {
            f"{PRIVATE_DIR.name}/{path.name}" for path in PRIVATE_DIR.glob("*.py")
        }
        self.assertEqual(documented, modules)


if __name__ == "__main__":
    unittest.main()
