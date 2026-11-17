#!/usr/bin/env python3
"""A hostile or damaged pyproject is a ConfigError, never a bare exception.

What these tests are defending
------------------------------
The library's promise is two-valued: absent config is empty, and config that cannot be
trusted raises :class:`pyproject_config.ConfigError`. Every consumer builds its exit-2
line on the second half, so an input that leaves this module as any OTHER exception
reaches the user as a traceback, and an uncaught exception's exit code 1 is the code
most gates use for "a finding".

Each class below is one way an input got past the catch that was meant to hold it:

* **Nesting past the parser's depth.** ``tomllib`` is recursive, and a document nested
  deeper than the interpreter's recursion limit allows raises ``RecursionError``, which
  is not a ``TOMLDecodeError``.
* **An entry that is not a usable path.** TOML's ``\\u0000`` escape puts a NUL into a
  perfectly well-formed string, and ``Path.resolve`` refuses it with ``ValueError``.
* **A target that cannot be resolved.** The walk begins by resolving the caller's
  target, so the same ``ValueError`` used to leave before any file was looked for.
* **A real damaged file.** The decode and OS errors were already caught; these pin
  them against real bytes on disk, where the other suites use fakes.

The nesting depths are deliberately far past what was measured to fail (see
``_DEEP_ARRAYS``), because the smallest failing depth moves with how deep the caller's
own stack already is.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import pyproject_config as pc
from _fixtures import TABLE as _TABLE

_BASE = Path("/proj").resolve()
_PYPROJECT = _BASE / "pyproject.toml"

# Measured on CPython 3.14 at the default recursion limit of 1,000, called from the top
# of a script: nested arrays first raise RecursionError at depth 497, nested inline
# tables at 331, and a dotted key at 1,001 segments. Ten times the largest of those, so
# the input stays past the limit from any call depth a test runner adds.
_DEEP_ARRAYS = "deep = " + "[" * 10_000 + "]" * 10_000 + "\n"
_DEEP_INLINE_TABLES = "deep = " + "{a = " * 10_000 + "1" + "}" * 10_000 + "\n"
_DEEP_DOTTED_KEY = ".".join(["a"] * 10_000) + " = 1\n"

# A path no filesystem call accepts: the embedded NUL makes Path.resolve raise ValueError.
_NUL_PATH = Path("/proj/a\x00b")


def _only_the_pyproject(path: Path) -> bool:
    """is_file seam: the one pyproject at ``_BASE`` exists, and nothing else does."""
    return path == _PYPROJECT


def _vanished() -> FileNotFoundError:
    """The ``OSError`` a filesystem raises from ``Path.resolve``, absolute path included."""
    return FileNotFoundError(2, "No such file or directory", str(_BASE / "gone"))


def _nearest(start: Path) -> Path | None:
    """``nearest`` over the one-pyproject tree, as a single-argument route."""
    return pc.nearest(start, is_file=_only_the_pyproject)


class TooDeeplyNestedTest(unittest.TestCase):
    """A document the parser cannot follow is unparseable config, not a crash."""

    def _raised(self, body: str) -> pc.ConfigError:
        with self.assertRaises(pc.ConfigError) as caught:
            pc.read_tool_table(
                _BASE / "a" / "b",
                table=_TABLE,
                is_file=_only_the_pyproject,
                read_text=lambda _p: body,
            )
        return caught.exception

    def test_the_inputs_really_do_overflow_the_parser(self) -> None:
        """Guards the fixtures: an input the parser accepted would test nothing below."""
        import tomllib

        for body in (_DEEP_ARRAYS, _DEEP_INLINE_TABLES, _DEEP_DOTTED_KEY):
            with self.subTest(body=body[:20]), self.assertRaises(RecursionError):
                tomllib.loads(body)

    def test_deeply_nested_arrays_are_a_config_error(self) -> None:
        self.assertIn("cannot read or parse", str(self._raised(_DEEP_ARRAYS)))

    def test_deeply_nested_inline_tables_are_a_config_error(self) -> None:
        self.assertIn("cannot read or parse", str(self._raised(_DEEP_INLINE_TABLES)))

    def test_a_very_deep_dotted_key_is_a_config_error(self) -> None:
        self.assertIn("cannot read or parse", str(self._raised(_DEEP_DOTTED_KEY)))

    def test_the_path_list_reader_raises_the_same_error(self) -> None:
        with self.assertRaises(pc.ConfigError):
            pc.read_path_lists(
                _BASE,
                table=_TABLE,
                keys=("allow",),
                is_file=_only_the_pyproject,
                read_text=lambda _p: _DEEP_ARRAYS,
            )

    def test_the_message_says_why_and_names_the_file_relatively(self) -> None:
        """One short line: consumers print it verbatim as their exit-2 diagnostic."""
        message = str(self._raised(_DEEP_ARRAYS))
        shown = os.path.join(os.pardir, os.pardir, "pyproject.toml")
        self.assertEqual(
            message,
            f"cannot read or parse {shown}: it nests deeper than the parser can follow",
        )

    def test_the_parser_error_stays_attached_as_the_cause(self) -> None:
        self.assertIsInstance(self._raised(_DEEP_ARRAYS).__cause__, RecursionError)


class UnresolvableEntryTest(unittest.TestCase):
    """A configured entry that cannot be a path is a malformed value, so it stops the gate."""

    def _raised(self) -> str:
        with self.assertRaises(pc.ConfigError) as caught:
            pc.read_path_lists(
                _BASE / "a",
                table=_TABLE,
                keys=("allow",),
                is_file=_only_the_pyproject,
                # The escape is TOML's own, so this document parses cleanly.
                read_text=lambda _p: f'[tool.{_TABLE}]\nallow = ["ok", "a\\u0000b"]\n',
            )
        return str(caught.exception)

    def test_an_entry_holding_a_nul_is_a_config_error(self) -> None:
        self.assertIn("cannot be resolved as a path", self._raised())

    def test_the_message_names_the_table_the_key_and_the_file(self) -> None:
        message = self._raised()
        self.assertIn(f"[tool.{_TABLE}] allow", message)
        self.assertIn(os.path.join(os.pardir, "pyproject.toml"), message)
        self.assertNotIn(str(_PYPROJECT), message)

    def test_the_message_names_the_entry_by_its_one_based_position(self) -> None:
        """The NUL sits in the second of two entries: counted from 1, as a reader counts."""
        message = self._raised()
        self.assertIn("holds an entry (number 2) that cannot be resolved", message)
        self.assertNotIn("a\x00b", message)

    def test_the_first_entry_is_number_one(self) -> None:
        with self.assertRaises(pc.ConfigError) as caught:
            pc._resolved_list(
                {"allow": ["a\x00b", "ok"]},
                "allow",
                base=_BASE,
                pyproject=Path("pyproject.toml"),
                table=_TABLE,
            )
        self.assertIn("an entry (number 1) that", str(caught.exception))

    def test_the_message_does_not_carry_the_nul_onto_a_terminal(self) -> None:
        self.assertNotIn("\x00", self._raised())

    def test_a_resolve_failure_of_any_kind_is_a_config_error(self) -> None:
        """Before 3.13 a symlink loop raises RuntimeError, whose text quotes the path.

        Faked, because this interpreter no longer raises it: the message must name the
        error's type and nothing from its text.
        """
        loop = RuntimeError(f"Symlink loop from '{_BASE / 'loop'}'")
        with (
            mock.patch.object(Path, "resolve", side_effect=loop),
            self.assertRaises(pc.ConfigError) as caught,
        ):
            pc._resolved_list(
                {"allow": ["loop"]},
                "allow",
                base=_BASE,
                pyproject=Path("pyproject.toml"),
                table=_TABLE,
            )
        self.assertIn("RuntimeError", str(caught.exception))
        self.assertNotIn(str(_BASE), str(caught.exception))

    def test_a_filesystem_refusal_is_a_config_error(self) -> None:
        """An ``OSError`` from the filesystem itself, such as a vanished working directory.

        Faked, because no real tree raises it on demand. An ``OSError``'s text ends with
        its ``filename``, so the message names the error's type and nothing else.
        """
        with (
            mock.patch.object(Path, "resolve", side_effect=_vanished()),
            self.assertRaises(pc.ConfigError) as caught,
        ):
            pc._resolved_list(
                {"allow": ["gone"]},
                "allow",
                base=_BASE,
                pyproject=Path("pyproject.toml"),
                table=_TABLE,
            )
        self.assertIn("FileNotFoundError", str(caught.exception))
        self.assertNotIn(str(_BASE), str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, FileNotFoundError)


class UnresolvableTargetTest(unittest.TestCase):
    """A target the walk cannot resolve finds no config it could trust."""

    def test_a_filesystem_refusal_of_the_target_is_a_config_error(self) -> None:
        """The same faked ``OSError``, reached through the walk's own first step."""
        for route in (pc._resolved_target, _nearest):
            with self.subTest(route=route.__name__):
                with (
                    mock.patch.object(Path, "resolve", side_effect=_vanished()),
                    self.assertRaises(pc.ConfigError) as caught,
                ):
                    route(_BASE / "gone")
                message = str(caught.exception)
                self.assertIn("cannot resolve the path", message)
                self.assertIn("FileNotFoundError", message)
                self.assertNotIn(str(_BASE), message)

    def test_the_raw_reader_raises_config_error(self) -> None:
        with self.assertRaises(pc.ConfigError) as caught:
            pc.read_tool_table(
                _NUL_PATH,
                table=_TABLE,
                is_file=_only_the_pyproject,
                read_text=lambda _p: "",
            )
        message = str(caught.exception)
        self.assertIn("cannot resolve the path", message)
        self.assertNotIn("\x00", message)
        self.assertNotIn(str(_BASE), message)

    def test_the_path_list_reader_raises_config_error(self) -> None:
        with self.assertRaises(pc.ConfigError):
            pc.read_path_lists(
                _NUL_PATH,
                table=_TABLE,
                keys=("allow",),
                is_file=_only_the_pyproject,
                read_text=lambda _p: "",
            )

    def test_nearest_raises_config_error(self) -> None:
        with self.assertRaises(pc.ConfigError):
            pc.nearest(_NUL_PATH, is_file=_only_the_pyproject)

    def test_chain_raises_config_error_for_either_end(self) -> None:
        for start, stop in ((_NUL_PATH, _BASE), (_BASE, _NUL_PATH)):
            with self.subTest(start=start), self.assertRaises(pc.ConfigError):
                pc.chain(start, stop=stop, is_file=_only_the_pyproject)


class RealDamagedFileTest(unittest.TestCase):
    """The real ``default_read_text`` against real bytes: each fault is a ConfigError.

    ``is_file`` stays pinned to the one temp file, so the walk can never climb out of
    the temp directory into the host's own tree.
    """

    def _raised(self, pyproject: Path) -> str:
        with self.assertRaises(pc.ConfigError) as caught:
            pc.read_tool_table(
                pyproject.parent,
                table=_TABLE,
                is_file=lambda p: p == pyproject,
                read_text=pc.default_read_text,
            )
        return str(caught.exception)

    def test_bytes_that_are_not_utf8(self) -> None:
        with TemporaryDirectory() as tmp:
            pyproject = Path(tmp).resolve() / "pyproject.toml"
            pyproject.write_bytes(b"\xff\xfe\x00not text")
            message = self._raised(pyproject)
        self.assertTrue(message.startswith("cannot read or parse pyproject.toml:"))
        self.assertNotIn(tmp, message)

    def test_a_directory_where_the_file_should_be(self) -> None:
        with TemporaryDirectory() as tmp:
            pyproject = Path(tmp).resolve() / "pyproject.toml"
            pyproject.mkdir()
            message = self._raised(pyproject)
        self.assertTrue(message.startswith("cannot read or parse pyproject.toml:"))
        self.assertNotIn(tmp, message)

    def test_a_raw_nul_in_the_text(self) -> None:
        with TemporaryDirectory() as tmp:
            pyproject = Path(tmp).resolve() / "pyproject.toml"
            pyproject.write_bytes(b'[tool.widget_gate]\nallow = ["a\x00b"]\n')
            message = self._raised(pyproject)
        self.assertTrue(message.startswith("cannot read or parse pyproject.toml:"))
