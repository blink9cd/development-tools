#!/usr/bin/env python3
"""The private layers ``read_tool_table`` and ``read_path_lists`` are built from.

These are tested directly rather than only through the public entry points because
each one owns a distinct decision that fails SILENTLY when wrong: the real seams pin
the UTF-8 decode, the validator pins that a wrong-shaped value stops the gate, and
the table/list extractors pin that every wrong shape on the way down degrades to
empty rather than raising. Reaching them through the walk alone would leave several
of those branches provable only by inference.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pyproject_config as pc
from _fixtures import TABLE as _TABLE
from _fixtures import OTHER_TABLE as _OTHER_TABLE


class DefaultSeamsTest(unittest.TestCase):
    """The real filesystem implementations of the injected seams.

    These exist so a consumer never re-types ``read_text(encoding="utf-8")`` and
    gets the encoding wrong on a non-UTF-8 locale. They are tested against real
    temp files rather than pragma'd out: they are public API here, and the whole
    point of owning them centrally is that their behavior is pinned once.
    """

    def test_is_file_true_for_a_file_and_false_for_a_dir(self) -> None:
        with TemporaryDirectory() as tmp:
            here = Path(tmp)
            target = here / "pyproject.toml"
            target.write_text("", encoding="utf-8", newline="")
            self.assertTrue(pc.default_is_file(target))
            self.assertFalse(pc.default_is_file(here))
            self.assertFalse(pc.default_is_file(here / "absent.toml"))

    def test_is_dir_true_for_a_dir_and_false_for_a_file(self) -> None:
        with TemporaryDirectory() as tmp:
            here = Path(tmp)
            target = here / "f.txt"
            target.write_text("", encoding="utf-8", newline="")
            self.assertTrue(pc.default_is_dir(here))
            self.assertFalse(pc.default_is_dir(target))

    def test_read_text_decodes_as_utf8(self) -> None:
        """Explicitly UTF-8, not the locale code page -- a pyproject may hold any text."""
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "pyproject.toml"
            target.write_bytes('name = "café"\n'.encode())
            self.assertEqual(pc.default_read_text(target), 'name = "café"\n')


class RequireStringListTest(unittest.TestCase):
    """The list-of-strings validator refuses every other shape."""

    def test_valid_list_passes_through(self) -> None:
        self.assertEqual(
            pc._require_string_list(
                ["a"], key="allow", pyproject=Path("p"), table=_TABLE
            ),
            ["a"],
        )

    def test_not_a_list_raises(self) -> None:
        with self.assertRaises(pc.ConfigError):
            pc._require_string_list("x", key="allow", pyproject=Path("p"), table=_TABLE)

    def test_non_string_element_raises(self) -> None:
        with self.assertRaises(pc.ConfigError):
            pc._require_string_list(
                ["a", 3], key="allow", pyproject=Path("p"), table=_TABLE
            )

    def test_message_names_the_table_the_key_and_the_file(self) -> None:
        """The diagnostic must point at the exact table/key/file to edit."""
        with self.assertRaises(pc.ConfigError) as caught:
            pc._require_string_list(
                3, key="allow", pyproject=Path("/proj/pyproject.toml"), table=_TABLE
            )
        message = str(caught.exception)
        self.assertIn(f"[tool.{_TABLE}]", message)
        self.assertIn("allow", message)
        self.assertIn("pyproject.toml", message)


class ToolTableTest(unittest.TestCase):
    """Extracting [tool.<name>] degrades to {} on every wrong shape."""

    def test_data_not_a_dict(self) -> None:
        self.assertEqual(pc._tool_table(3, _TABLE), {})

    def test_tool_not_a_dict(self) -> None:
        self.assertEqual(pc._tool_table({"tool": 3}, _TABLE), {})

    def test_table_not_a_dict(self) -> None:
        self.assertEqual(pc._tool_table({"tool": {_TABLE: 3}}, _TABLE), {})

    def test_table_absent(self) -> None:
        self.assertEqual(
            pc._tool_table({"tool": {_OTHER_TABLE: {"k": []}}}, _TABLE), {}
        )

    def test_present_table_is_returned(self) -> None:
        self.assertEqual(
            pc._tool_table({"tool": {_TABLE: {"allow": ["a"]}}}, _TABLE),
            {"allow": ["a"]},
        )


class ResolvedListTest(unittest.TestCase):
    """One key resolved against the pyproject's own directory."""

    def test_absent_key_is_empty(self) -> None:
        self.assertEqual(
            pc._resolved_list(
                {}, "allow", base=Path("/base"), pyproject=Path("p"), table=_TABLE
            ),
            [],
        )

    def test_present_key_resolves_against_base(self) -> None:
        base = Path("/base")
        self.assertEqual(
            pc._resolved_list(
                {"allow": ["a/b"]},
                "allow",
                base=base,
                pyproject=Path("p"),
                table=_TABLE,
            ),
            [(base / "a/b").resolve()],
        )

    def test_malformed_value_raises(self) -> None:
        with self.assertRaises(pc.ConfigError):
            pc._resolved_list(
                {"allow": [1]},
                "allow",
                base=Path("/base"),
                pyproject=Path("p"),
                table=_TABLE,
            )


class ReasonTest(unittest.TestCase):
    """``_reason``: what a ConfigError quotes of the underlying error."""

    def test_an_os_error_without_strerror_names_its_type(self) -> None:
        self.assertEqual(pc._reason(PermissionError()), "PermissionError")

    def test_any_other_error_is_quoted_whole(self) -> None:
        self.assertEqual(pc._reason(ValueError("bad value")), "bad value")
