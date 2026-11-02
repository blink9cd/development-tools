#!/usr/bin/env python3
r"""Caller-supplied arguments: each must reach its program as exactly the word given.

An argument is rendered bare only when it is a non-empty run of letters, digits and
``_ . / : = + , @ -``: no whitespace, no quote, no backslash, no ``%`` or ``$``, no glob,
tilde or shell metacharacter, so neither systemd, ``sh``, cron nor the Windows command-line
parser reads any character of it specially. That rule is what keeps the sweep's literal
``--mode run`` byte for byte where it was. Every other argument goes through its format's
quoting, and this suite proves both halves by decoding, as ``test_hostile_values`` does for
paths: systemd's rules by a model, cron's ``%`` by a model and then a real ``sh``, and the
Windows command line by a model of ``CommandLineToArgvW`` that is itself checked against
the standard library's own encoder for that convention (``subprocess.list2cmdline``).

The script word in the Task XML is always quoted, as the sweep's task always quoted it, and
escaped too: unescaped, a ``"`` or a trailing ``\`` in it would end the quote early.
"""

from __future__ import annotations

import re
import subprocess
import unittest
import xml.etree.ElementTree as ET
from datetime import date
from typing import Callable

import os_schedule_gen as sa
from _fixtures import FAKE_PYTHON, FAKE_SCRIPT
from test_hostile_values import (
    _HOSTILE,
    _cron_command,
    _read_unit,
    _sh_words,
    _systemd_dollars,
    _systemd_specifiers,
    _systemd_words,
)

_NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
_SCHEDULE = sa.WeeklySchedule(weekday="Friday", hour=6, minute=30)
_START = date(2026, 1, 2)

#: Arguments the bare rule must leave alone: every character it admits, in each position.
_BARE = (
    "--mode",
    "run",
    "--level=3",
    "a,b+c@d:e",
    "/opt/x/y.py",
    "-",
    "v1.2_3",
)

#: Arguments each needing quoting in at least one format, beyond the hostile paths.
_QUOTED = (
    "",
    " ",
    "a b",
    "~",
    "*.py",
    "[ab]",
    "a;b",
    "a|b",
    "a&b",
    "a>b",
    "a<b",
    "{a,b}",
    "a#b",
    "!x",
    "'",
    '"',
    "\\",
    'a\\"b',
    "ends\\",
    "ends\\\\",
    "\t",
    "%PATH%",
    "$x",
    "Ünïcode",
)

_ALL = _BARE + _QUOTED + _HOSTILE


def _windows_words(line: str) -> "list[str]":
    """Split a Windows command line into argv the way ``CommandLineToArgvW`` does.

    ``2n`` backslashes before a ``"`` are ``n`` backslashes and a quote toggle; ``2n+1`` are
    ``n`` backslashes and a literal ``"``; backslashes before anything else are literal.
    A ``""`` inside a quoted run is refused rather than modeled: runtimes disagree on it,
    and the encoder under test must never produce it.
    """
    words: "list[str]" = []
    index, size = 0, len(line)
    while True:
        while index < size and line[index] in " \t":
            index += 1
        if index >= size:
            return words
        word, quoted = "", False
        while index < size and (quoted or line[index] not in " \t"):
            char = line[index]
            if char == "\\":
                end = index
                while end < size and line[end] == "\\":
                    end += 1
                count = end - index
                if end < size and line[end] == '"':
                    word += "\\" * (count // 2) + ('"' if count % 2 else "")
                    index = end + (count % 2)
                else:
                    word += "\\" * count
                    index = end
            elif char == '"':
                if quoted and line[index + 1 : index + 2] == '"':
                    raise AssertionError(f'ambiguous "" inside quotes in {line!r}')
                quoted = not quoted
                index += 1
            else:
                word += char
                index += 1
        if quoted:
            raise AssertionError(f"unterminated quote in {line!r}")
        words.append(word)


def _task_arguments(script: "str | None", args: "tuple[str, ...]") -> "list[str]":
    """The task's Arguments for *script* and *args*, decoded as a Windows argv."""
    xml = sa.build_task_xml(_SCHEDULE, FAKE_PYTHON, script, _START, args=args)
    text = ET.fromstring(xml).findtext(f"{_NS}Actions/{_NS}Exec/{_NS}Arguments") or ""
    return _windows_words(text)


class WindowsModelTests(unittest.TestCase):
    """The Windows decoder agrees with the standard library's encoder for the convention."""

    def test_the_model_inverts_list2cmdline(self) -> None:
        for value in _ALL:
            with self.subTest(value=value):
                words = [value, "x", value]
                self.assertEqual(_windows_words(subprocess.list2cmdline(words)), words)

    def test_the_model_refuses_what_it_does_not_model(self) -> None:
        with self.assertRaises(AssertionError):
            _windows_words('"a""b"')
        with self.assertRaises(AssertionError):
            _windows_words('"open')
        self.assertEqual(
            _windows_words('a\\\\b "c d" \\\\"e f"'), ["a\\\\b", "c d", "\\e f"]
        )


class ArgumentRoundTripTests(unittest.TestCase):
    """Every argument decodes back to itself in every format that runs a command line."""

    def test_systemd_exec_start_reads_each_argument_back(self) -> None:
        for value in _ALL:
            with self.subTest(value=value):
                service, _timer = sa.build_systemd_units(
                    _SCHEDULE, FAKE_PYTHON, FAKE_SCRIPT, "", args=(value, "tail")
                )
                exec_line = dict(_read_unit(service)["Service"])["ExecStart"]
                words = [
                    _systemd_dollars(word)
                    for word in _systemd_words(_systemd_specifiers(exec_line))
                ]
                self.assertEqual(words, [FAKE_PYTHON, FAKE_SCRIPT, value, "tail"])

    def test_cron_hands_sh_each_argument_back(self) -> None:
        for value in _ALL:
            with self.subTest(value=value):
                line = sa.build_cron_line(
                    _SCHEDULE, FAKE_PYTHON, None, "", args=(value, "tail")
                )
                self.assertEqual(
                    _sh_words(_cron_command(line)), [FAKE_PYTHON, value, "tail"]
                )

    def test_the_task_reads_each_argument_and_the_script_back(self) -> None:
        for value in _ALL:
            with self.subTest(value=value):
                self.assertEqual(
                    _task_arguments(value, (value, "tail")), [value, value, "tail"]
                )


class BareRuleTests(unittest.TestCase):
    """The bare rule's two halves: a safe word stays bare, every other word is quoted."""

    def test_a_safe_word_is_written_bare_in_every_format(self) -> None:
        for value in _BARE:
            with self.subTest(value=value):
                service, _timer = sa.build_systemd_units(
                    _SCHEDULE, FAKE_PYTHON, None, "", args=(value,)
                )
                self.assertIn(f'ExecStart="{FAKE_PYTHON}" {value}\n', service)
                line = sa.build_cron_line(
                    _SCHEDULE, FAKE_PYTHON, None, "", args=(value,)
                )
                self.assertTrue(line.endswith(f'"{FAKE_PYTHON}" {value}'), line)
                xml = sa.build_task_xml(
                    _SCHEDULE, FAKE_PYTHON, None, _START, args=(value,)
                )
                self.assertIn(f"<Arguments>{value}</Arguments>", xml)

    def test_any_other_word_is_quoted_in_every_format(self) -> None:
        for value in _QUOTED + _HOSTILE:
            with self.subTest(value=value):
                service, _timer = sa.build_systemd_units(
                    _SCHEDULE, FAKE_PYTHON, None, "", args=(value,)
                )
                self.assertTrue(
                    service.split("ExecStart=")[1].split("\n")[0].endswith('"')
                )
                line = sa.build_cron_line(
                    _SCHEDULE, FAKE_PYTHON, None, "", args=(value,)
                )
                self.assertTrue(line.endswith('"'), line)
                xml = sa.build_task_xml(
                    _SCHEDULE, FAKE_PYTHON, None, _START, args=(value,)
                )
                self.assertIn("<Arguments>&quot;", xml)

    def test_the_sweep_script_is_quoted_even_when_it_is_safe(self) -> None:
        self.assertEqual(
            _task_arguments("C:/safe/tool.py", ("--mode", "run")),
            ["C:/safe/tool.py", "--mode", "run"],
        )
        xml = sa.build_task_xml(_SCHEDULE, FAKE_PYTHON, "C:/safe/tool.py", _START)
        self.assertIn(
            "<Arguments>&quot;C:/safe/tool.py&quot; --mode run</Arguments>", xml
        )


#: Characters XML 1.0 cannot hold at all (C0 controls but tab, newline and carriage
#: return) and DEL, which it can hold but no Task Scheduler value means. Newline and
#: carriage return are refused too, by the line-break rule, so every C0 control but tab
#: is refused in the end.
_XML_CONTROLS = tuple(chr(code) for code in range(0x20) if chr(code) != "\t") + (
    "\x7f",
)


def _task_renders(bad: str) -> "dict[str, Callable[[], str]]":
    """One ``build_task_xml`` call per value it writes, *bad* in that value alone.

    Keyed by the name the refusal must carry, which is the keyword's own spelling.
    """

    def task(**keywords: str) -> "Callable[[], str]":
        return lambda: sa.build_task_xml(
            _SCHEDULE,
            keywords.get("python") or FAKE_PYTHON,
            keywords.get("script"),
            _START,
            args=("ok", keywords["arg"]) if "arg" in keywords else ("--mode", "run"),
            task_name=keywords.get("task_name") or sa.TASK_NAME,
            description=keywords.get("description") or "x",
            working_directory=keywords.get("working_directory"),
            execution_limit=keywords.get("execution_limit") or "PT1M",
        )

    return {
        "python": task(python=bad),
        "script": task(script=bad),
        "args[1]": task(arg=bad),
        "task_name": task(task_name=bad),
        "description": task(description=bad),
        "working_directory": task(working_directory=bad),
        "execution_limit": task(execution_limit=bad),
    }


class TaskXmlControlTests(unittest.TestCase):
    """A control character in any Task XML value is refused, never written unparsable."""

    def test_a_control_character_in_any_value_is_refused_by_name(self) -> None:
        for char in _XML_CONTROLS:
            bad = f"a{char}b"
            for keyword, render in _task_renders(bad).items():
                with self.subTest(char=hex(ord(char)), keyword=keyword):
                    with self.assertRaisesRegex(ValueError, re.escape(keyword)):
                        render()

    def test_a_tab_is_kept_and_the_xml_still_parses(self) -> None:
        xml = sa.build_task_xml(
            _SCHEDULE, FAKE_PYTHON, None, _START, args=("a\tb",), description="x\ty"
        )
        root = ET.fromstring(xml)
        self.assertEqual(
            _windows_words(
                root.findtext(f"{_NS}Actions/{_NS}Exec/{_NS}Arguments") or ""
            ),
            ["a\tb"],
        )
        self.assertEqual(
            root.findtext(f"{_NS}RegistrationInfo/{_NS}Description"), "x\ty"
        )


if __name__ == "__main__":
    unittest.main()
