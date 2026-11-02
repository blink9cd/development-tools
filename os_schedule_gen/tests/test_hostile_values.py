#!/usr/bin/env python3
"""Hostile paths through the systemd and cron builders: the output must mean the input.

A text assertion shows that an escape was written; it cannot show the scheduler reads it
back as the same value. So each case here decodes the output the way its consumer does and
compares the decoded value with the input:

* the systemd units are read as INI (no key may appear that the builder did not write),
  after joining each line that ends in an unescaped backslash to the next, as systemd's
  parser does (``systemd.syntax(7)``), so a value that swallows the key below it is seen;
  then each field is decoded by a model of systemd's rules for that field: specifiers
  first, then C-style unquoting where the field takes it, then ``$`` substitution in
  ``ExecStart=`` alone (``systemd.service(5)``, ``systemd.exec(5)``);
* the cron line's command has crontab(5)'s ``%`` rule applied by a model of cron's own
  loop, then goes through a real ``sh``: ``-n`` to parse it, and a ``printf`` of its words
  to read each one back. Nothing runs but the shell's builtins, under a ``PATH`` holding
  no command at all, so a broken escape cannot reach the host.

No unit or crontab is ever installed. A value holding a line break or NUL is refused
outright: no escape keeps a newline inside one INI value or one crontab line.
"""

from __future__ import annotations

import configparser
import re
import shutil
import subprocess
import tempfile
import unittest

import os_schedule_gen as sa
from _fixtures import FAKE_PATH, FAKE_PYTHON, FAKE_SCRIPT

_SCHEDULE = sa.WeeklySchedule(weekday="Friday", hour=6, minute=30)

#: Each a path a real directory could carry, and each a hazard to one of the two formats.
_HOSTILE = (
    "/opt/x$(id)/bin",
    "/opt/`id`/bin",
    '/opt/a"b/bin',
    "/opt/a%b/bin",
    "/opt/a%%n/bin",
    "/opt/sp ace/bin",
    "/opt/back\\slash/bin",
    "/opt/back\\%pct/bin",
    "/opt/$HOME/${HOME}/$$/bin",
    "/opt/it's/bin",
    "/opt/semi;colon&amp|pipe/bin",
)

#: Values ending in what systemd's parser reads specially at a line's end: an unescaped
#: backslash joins the next line, and surrounding whitespace is stripped.
_TRAILING = ("/opt/trail\\", "/opt/trail ", "/opt/trail\\\\", "/opt/trail\t")

#: Scripts whose directory, written unquoted into ``WorkingDirectory=``, systemd would misread.
_UNWRITABLE_SCRIPTS = (
    "/opt/a\\/s.py",
    "/opt/a /s.py",
    "/opt/a\t/s.py",
    " /opt/s.py",
    # Not absolute: systemd reads a leading "~" as the user's home and a leading "-" as
    # "ignore if missing", and a bare file name gives ".".
    "~/s.py",
    "-/opt/s.py",
    "s.py",
)

#: The characters a builder must refuse, and the name each one goes by in a failure.
_LINE_BREAKS = {"newline": "\n", "carriage return": "\r", "NUL": "\0"}

#: How long the shell gets to parse or print one line; it runs only builtins.
_SHELL_TIMEOUT_SECONDS = 10.0


def _systemd_specifiers(value: str) -> str:
    """Resolve systemd specifiers, refusing any but ``%%``: the units use none on purpose."""

    def resolve(match: "re.Match[str]") -> str:
        if match.group(1) != "%":
            raise AssertionError(f"unescaped specifier %{match.group(1)} in {value!r}")
        return "%"

    return re.sub(r"%(.?)", resolve, value, flags=re.DOTALL)


def _systemd_words(value: str) -> "list[str]":
    """Split a value into words the way systemd does: quotes group, backslash escapes."""
    words: "list[str]" = []
    chars = iter(value)
    current: "str | None" = None
    quoted = False
    for char in chars:
        if char == "\\":
            current = (current or "") + next(chars)
        elif char == '"':
            quoted = not quoted
            current = current or ""
        elif char == " " and not quoted:
            if current is not None:
                words.append(current)
            current = None
        else:
            current = (current or "") + char
    if quoted:
        raise AssertionError(f"unterminated quote in {value!r}")
    if current is not None:
        words.append(current)
    return words


def _systemd_dollars(word: str) -> str:
    """Resolve ``ExecStart=``'s ``$`` substitution, refusing any but ``$$``."""

    def resolve(match: "re.Match[str]") -> str:
        if match.group(1) != "$":
            raise AssertionError(f"unescaped variable ${match.group(1)} in {word!r}")
        return "$"

    return re.sub(r"\$(.?)", resolve, word, flags=re.DOTALL)


def _join_continuations(text: str) -> str:
    """*text* with systemd's line continuation applied: a model of its config parser's loop.

    A line ending in an unescaped backslash (an odd run of them, counted over the joined text
    so far) has that backslash replaced by a space and the next line appended to it.
    """
    lines: "list[str]" = []
    pending: "str | None" = None
    for line in text.split("\n"):
        joined = line if pending is None else pending + line
        trailing = len(joined) - len(joined.rstrip("\\"))
        if trailing % 2:
            pending = joined[:-1] + " "
            continue
        lines.append(joined)
        pending = None
    if pending is not None:
        lines.append(pending)
    return "\n".join(lines)


def _read_unit(text: str) -> configparser.ConfigParser:
    """*text* read as systemd does: continuations joined, then INI with its one delimiter.

    Case is kept and nothing interpolated. configparser strips each value's surrounding
    whitespace, as systemd's parser does too.
    """
    unit = configparser.ConfigParser(delimiters=("=",), interpolation=None, strict=True)
    unit.optionxform = str  # type: ignore[assignment,method-assign] # systemd keys keep case
    unit.read_string(_join_continuations(text))
    return unit


def _decoded_service(python: str, script: str, path_value: str) -> "dict[str, object]":
    """The service unit for the three values, decoded field by field."""
    service, _timer = sa.build_systemd_units(_SCHEDULE, python, script, path_value)
    fields = dict(_read_unit(service)["Service"])
    exec_words = [
        _systemd_dollars(word)
        for word in _systemd_words(_systemd_specifiers(fields.pop("ExecStart")))
    ]
    environment = _systemd_words(_systemd_specifiers(fields.pop("Environment")))
    directory = _systemd_specifiers(fields.pop("WorkingDirectory"))
    return {
        "exec": exec_words,
        "environment": environment,
        "directory": directory,
        "rest": sorted(fields),
    }


def _cron_command(line: str) -> str:
    """The command cron hands to ``sh`` for *line*, after crontab(5)'s ``%`` rule.

    A model of cron's own copy loop: a backslash marks the next character escaped, an escaped
    ``%`` replaces that backslash, any other escaped character keeps it, and an unescaped
    ``%`` ends the command (what follows it is stdin, which these lines must never have).
    """
    command = line.split(" ", 5)[5]
    out: "list[str]" = []
    escaped = False
    for char in command:
        if escaped:
            if char == "%":
                out[-1] = "%"
            else:
                out.append(char)
            escaped = False
        elif char == "\\":
            out.append(char)
            escaped = True
        elif char == "%":
            raise AssertionError(f"unescaped % ends the command early: {line!r}")
        else:
            out.append(char)
    return "".join(out)


def _sh_words(command: str) -> "list[str]":
    """Every word of *command* as ``sh`` reads it, printed by a builtin and never run.

    ``printf`` takes the words as its arguments, so the assignment prefix reads back as one
    ``PATH=...`` word and the command name as a plain argument. ``PATH`` names an empty
    directory, so a command substitution an escape failed to stop finds nothing to run.
    """
    shell = shutil.which("sh")
    if shell is None:  # pragma: no cover - every POSIX host and Git for Windows has sh
        raise unittest.SkipTest("no sh on this host")
    with tempfile.TemporaryDirectory() as empty:
        env = {"PATH": empty}
        # Both runs are leaves: sh parses, or runs builtins, so no grandchild holds a pipe.
        parsed = subprocess.run(  # reliability: ignore[RL005] sh -n is a leaf
            [shell, "-n", "-c", command],
            capture_output=True,
            env=env,
            timeout=_SHELL_TIMEOUT_SECONDS,
            check=False,
        )
        if parsed.returncode != 0:
            raise AssertionError(f"sh -n refused {command!r}: {parsed.stderr!r}")
        printed = subprocess.run(  # reliability: ignore[RL005] leaf: builtins only
            [shell, "-c", f"printf '%s\\0' {command}"],
            capture_output=True,
            env=env,
            timeout=_SHELL_TIMEOUT_SECONDS,
            check=False,
        )
    if printed.returncode != 0 or printed.stderr:
        raise AssertionError(f"sh could not print {command!r}: {printed.stderr!r}")
    return printed.stdout.decode("utf-8").split("\0")[:-1]


class SystemdHostileValueTests(unittest.TestCase):
    """Each hostile value decodes back to itself in the field it went into."""

    def test_every_field_decodes_to_the_value_given(self) -> None:
        for value in _HOSTILE:
            with self.subTest(value=value):
                decoded = _decoded_service(value + "/py", value + "/s.py", value)
                self.assertEqual(
                    decoded["exec"], [value + "/py", value + "/s.py", "--mode", "run"]
                )
                self.assertEqual(decoded["environment"], ["PATH=" + value])
                self.assertEqual(decoded["directory"], value)
                self.assertEqual(decoded["rest"], ["TimeoutStartSec", "Type"])

    def test_an_ordinary_path_decodes_unchanged(self) -> None:
        decoded = _decoded_service(FAKE_PYTHON, FAKE_SCRIPT, FAKE_PATH)
        self.assertEqual(decoded["exec"], [FAKE_PYTHON, FAKE_SCRIPT, "--mode", "run"])
        self.assertEqual(decoded["environment"], ["PATH=" + FAKE_PATH])

    def test_a_value_ending_in_a_backslash_or_whitespace_decodes_unchanged(
        self,
    ) -> None:
        # Quoted fields end in a quote, so no trailing character of the value ends the line.
        for value in _TRAILING:
            with self.subTest(value=value):
                script = "/opt/bin/s" + value[len("/opt/trail") :]
                decoded = _decoded_service(value, script, value)
                self.assertEqual(decoded["exec"], [value, script, "--mode", "run"])
                self.assertEqual(decoded["environment"], ["PATH=" + value])
                self.assertEqual(decoded["directory"], "/opt/bin")
                self.assertEqual(decoded["rest"], ["TimeoutStartSec", "Type"])

    def test_a_working_directory_systemd_would_misread_is_refused(self) -> None:
        # Regression: "/opt/a\\/s.py" wrote "WorkingDirectory=/opt/a\\", which joined the
        # TimeoutStartSec= line into the path. The field cannot be quoted or escaped.
        for script in _UNWRITABLE_SCRIPTS:
            with self.subTest(script=script):
                with self.assertRaises(ValueError):
                    sa.build_systemd_units(_SCHEDULE, FAKE_PYTHON, script, FAKE_PATH)

    def test_a_line_break_in_any_value_is_refused(self) -> None:
        for name, char in _LINE_BREAKS.items():
            for field in range(3):
                values = [FAKE_PYTHON, FAKE_SCRIPT, FAKE_PATH]
                values[field] = f"/opt/a{char}ExecStartPre=/bin/evil"
                with self.subTest(char=name, field=field):
                    with self.assertRaises(ValueError):
                        sa.build_systemd_units(_SCHEDULE, *values)


class CronHostileValueTests(unittest.TestCase):
    """Each hostile value reaches ``sh`` as one word holding exactly itself."""

    def test_every_word_reads_back_as_the_value_given(self) -> None:
        for value in _HOSTILE:
            with self.subTest(value=value):
                line = sa.build_cron_line(
                    _SCHEDULE, value + "/py", value + "/s.py", value
                )
                self.assertTrue(line.startswith("30 6 * * 5 "), line)
                self.assertEqual(
                    _sh_words(_cron_command(line)),
                    ["PATH=" + value, value + "/py", value + "/s.py", "--mode", "run"],
                )

    def test_a_value_ending_in_a_backslash_or_whitespace_reads_back(self) -> None:
        for value in _TRAILING + _UNWRITABLE_SCRIPTS:
            with self.subTest(value=value):
                line = sa.build_cron_line(_SCHEDULE, value, value, value)
                self.assertEqual(
                    _sh_words(_cron_command(line)),
                    ["PATH=" + value, value, value, "--mode", "run"],
                )

    def test_an_ordinary_line_reads_back_too(self) -> None:
        line = sa.build_cron_line(_SCHEDULE, FAKE_PYTHON, FAKE_SCRIPT, "")
        self.assertEqual(
            _sh_words(_cron_command(line)), [FAKE_PYTHON, FAKE_SCRIPT, "--mode", "run"]
        )

    def test_a_line_break_in_any_value_is_refused(self) -> None:
        for name, char in _LINE_BREAKS.items():
            for field in range(3):
                values = [FAKE_PYTHON, FAKE_SCRIPT, FAKE_PATH]
                values[field] = f"/opt/a{char}* * * * * /bin/evil"
                with self.subTest(char=name, field=field):
                    with self.assertRaises(ValueError):
                        sa.build_cron_line(_SCHEDULE, *values)


class DecoderModelTests(unittest.TestCase):
    """The decoders above refuse what their consumer would misread, so a pass means something."""

    def test_an_unescaped_specifier_or_variable_is_refused(self) -> None:
        with self.assertRaises(AssertionError):
            _systemd_specifiers("/opt/%h")
        with self.assertRaises(AssertionError):
            _systemd_dollars("/opt/$HOME")
        self.assertEqual(_systemd_specifiers("%%"), "%")
        self.assertEqual(_systemd_dollars("$$"), "$")

    def test_systemd_words_group_quotes_and_refuse_an_open_one(self) -> None:
        self.assertEqual(_systemd_words('"a b" c\\"d ""'), ["a b", 'c"d', ""])
        with self.assertRaises(AssertionError):
            _systemd_words('"open')

    def test_a_trailing_backslash_joins_the_next_line(self) -> None:
        unit = _read_unit("[S]\nA=x\\\nB=1\nC=y\\\\\nD= z \n")
        self.assertEqual(dict(unit["S"]), {"A": "x B=1", "C": "y\\\\", "D": "z"})
        self.assertEqual(_join_continuations("a\\"), "a ")

    def test_an_unescaped_percent_ends_a_cron_command(self) -> None:
        with self.assertRaises(AssertionError):
            _cron_command("0 0 * * 0 echo a%b")
        self.assertEqual(_cron_command("0 0 * * 0 a\\%b\\x"), "a%b\\x")

    def test_a_line_sh_cannot_parse_is_refused(self) -> None:
        with self.assertRaises(AssertionError):
            _sh_words('"unterminated')

    def test_a_line_sh_parses_but_cannot_print_is_refused(self) -> None:
        with self.assertRaises(AssertionError):
            _sh_words("$(nonexistent-command-here)")


if __name__ == "__main__":
    unittest.main()
