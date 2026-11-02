r"""The escapes and refusals that keep a path a path in the systemd units and the cron line.

Private to os_schedule_gen: its builders and its command-line front end call these. One
rule set per format, because each consumer reads a different set of characters specially:
systemd's unit parser, its specifier and variable expansion, cron's ``%`` and ``sh``, and the
Windows command-line parser a Task Scheduler action's ``Arguments`` goes through.

An argument is written bare when :func:`is_bare_word` holds (a non-empty run of letters,
digits and ``_ . / : = + , @ -``), because no format here reads any of those specially, and
quoted otherwise. That is what keeps a trusted literal such as ``--mode run`` byte for byte
as it always was, while a caller's ``a b`` or ``$x`` is quoted like any path.

The one value no escape can save is ``WorkingDirectory=``: systemd takes the rest of that
line as the path, unquoted, so only ``%`` can be escaped there. Its unit-file parser joins a
line ending in an unescaped ``\`` to the next (``systemd.syntax(7)``), which would fold the
key below into the path, and it strips a value's surrounding whitespace; so a directory
ending in ``\``, or starting or ending in whitespace, is refused rather than written.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Callable, Optional

#: The characters an argument may hold and still be written bare in every format: none of
#: them is whitespace, a quote, an escape, an expansion (``%``, ``$``, ``~``), a glob or an
#: ``sh`` operator. ASCII ranges spelled out, never ``\w``, which matches any letter.
_BARE_WORD = re.compile(r"[A-Za-z0-9_./:=+,@-]+")
#: A job name: the systemd unit basename, the launchd label and every file name derived
#: from it. Nothing outside this set can be escaped in a unit name, so it is refused. The
#: first character is a letter or digit: a leading ``-`` reads as an option to every tool
#: handed the file name, a leading ``.`` hides the file, and ``.`` and ``..`` are not files.
JOB_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
#: The characters no Task XML value may hold: every C0 control but tab (XML 1.0 cannot
#: carry them at all, so the file would not parse) and DEL, which it can carry but no
#: Task Scheduler field means. Newline and carriage return are among them, though the
#: line-break refusal names them first.
_XML_CONTROL = re.compile("[\x00-\x08\x0a-\x1f\x7f]")


def is_bare_word(word: str) -> bool:
    """True when *word* reads back unchanged unquoted in systemd, ``sh`` and Windows alike."""
    return _BARE_WORD.fullmatch(word) is not None


def refuse_bad_name(name: str, *, what: str) -> None:
    """Raise ValueError unless *name* matches :data:`JOB_NAME`; *what* names the field."""
    if JOB_NAME.fullmatch(name) is None:
        raise ValueError(
            f"{what} must be letters, digits, '.', '_' or '-', starting with a letter "
            f"or digit: {name!r}"
        )


def _named_values(
    python: str,
    script: Optional[str],
    args: "tuple[str, ...]",
    more: "dict[str, Optional[str]]",
) -> "dict[str, str]":
    """A command's values keyed as the builders spell them, each argument ``args[<i>]``.

    A None value is left out: there is nothing in it to refuse.
    """
    values = {"python": python, "script": script, **more}
    values.update({f"args[{index}]": word for index, word in enumerate(args)})
    return {name: value for name, value in values.items() if value is not None}


def refuse_command_breaks(
    python: str,
    script: Optional[str],
    args: "tuple[str, ...]",
    **more: Optional[str],
) -> None:
    """:func:`refuse_line_breaks` over a command and any *more* named values.

    A None value is skipped. Each value is named as the builders spell their keywords,
    each argument ``args[<i>]``, so a refusal names the keyword at fault.
    """
    refuse_line_breaks(**_named_values(python, script, args, more))


def refuse_task_controls(
    python: str,
    script: Optional[str],
    args: "tuple[str, ...]",
    *,
    task_name: str,
    **more: Optional[str],
) -> None:
    """Raise ValueError, naming the keyword, for a Task XML value holding a control character.

    Named as :func:`refuse_command_breaks` names them. A tab is kept: XML holds it, and
    it is an ordinary argument character on a Windows command line. *task_name* is then
    held to its path shape by :func:`refuse_bad_task_name`.
    """
    named = _named_values(python, script, args, {"task_name": task_name, **more})
    for name, value in named.items():
        if _XML_CONTROL.search(value) is not None:
            raise ValueError(
                f"{name} holds a control character a task cannot hold: {value!r}"
            )
    refuse_bad_task_name(task_name)


def refuse_bad_task_name(task_name: str) -> None:
    r"""Raise ValueError unless *task_name* is a task path: ``\`` and job-name segments.

    Each segment between backslashes is held to :data:`JOB_NAME`, so a ``..`` segment, an
    empty one, a space or markup is refused rather than handed to Task Scheduler, which
    reads the path as a folder tree. ``\Maintenance\WeeklySweep`` is one such path.
    """
    segments = task_name.split("\\")
    head, *parts = segments
    if head or not parts or not all(JOB_NAME.fullmatch(part) for part in parts):
        raise ValueError(
            "task_name must be a backslash, then backslash-separated segments of letters, "
            f"digits, '.', '_' or '-', each starting with a letter or digit: {task_name!r}"
        )


def command_words(
    fixed: "tuple[Optional[str], ...]",
    args: "tuple[str, ...]",
    quote: "Callable[[str], str]",
    word: "Callable[[str], str]",
) -> "list[str]":
    """The command words: each present *fixed* value *quote*-d, then each of *args* *word*-ed.

    The fixed values (interpreter, script) are always quoted, as they always were; an
    argument is bare only when its format's *word* finds nothing in it to quote.
    """
    return [quote(value) for value in fixed if value is not None] + [
        word(arg) for arg in args
    ]


def refuse_line_breaks(**values: str) -> None:
    """Raise ValueError for a value holding a line break or NUL, which could start a new key."""
    for name, value in values.items():
        if any(char in value for char in "\n\r\0"):
            raise ValueError(f"{name} holds a line break or NUL: {value!r}")


def systemd_quote(value: str, *, exec_word: bool) -> str:
    r"""*value* as one double-quoted systemd word that reads back as exactly *value*.

    ``\`` and ``"`` are backslash-escaped, so a value ending in ``\`` still ends its line
    with a quote. ``%`` is doubled: an unrecognized specifier makes systemd discard the
    whole ``Environment=`` assignment, so the unit installs, passes ``systemd-analyze
    verify``, and silently carries no PATH (real systemd accepts ``%%``). ``$`` is doubled
    only in an ``ExecStart=`` word (*exec_word*), the one field here that substitutes
    variables; ``Environment=`` gives ``$`` no meaning.
    """
    quoted = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return '"' + (quoted.replace("$", "$$") if exec_word else quoted) + '"'


def systemd_exec_word(word: str) -> str:
    """An ``ExecStart=`` argument: bare when :func:`is_bare_word` holds, else quoted."""
    return word if is_bare_word(word) else systemd_quote(word, exec_word=True)


def systemd_line_value(value: str, *, what: str) -> str:
    r"""A value systemd takes as the rest of its line, unquoted, with ``%`` doubled.

    Raises ValueError, naming *what*, for a value ending in ``\`` (it would join the next
    line) or starting or ending in whitespace (systemd strips it): no escape exists for
    either in an unquoted value.
    """
    if value.endswith("\\") or value != value.strip():
        raise ValueError(
            f"{what} must not end in a backslash or start or end in whitespace: {value!r}"
        )
    return value.replace("%", "%%")


def systemd_directory(directory: str, *, what: str) -> str:
    """A ``WorkingDirectory=`` value: absolute, then as :func:`systemd_line_value`.

    systemd reads a leading ``~`` as the user's home and a leading ``-`` as "ignore if
    missing", so only a directory starting with ``/`` means what it says.
    """
    if not directory.startswith("/"):
        raise ValueError(f"{what} must be an absolute directory: {directory!r}")
    return systemd_line_value(directory, what=what)


def systemd_work_dir(script: str) -> str:
    r"""The ``WorkingDirectory=`` value for *script*: its POSIX parent, ``%`` doubled.

    Raises ValueError when that directory is not absolute (systemd reads a leading ``~``
    as the user's home and a leading ``-`` as "ignore if missing"), or ends in ``\`` or
    starts or ends in whitespace, which systemd would read as a different path or as the
    next line's key (module docstring). ``PurePosixPath``, not ``Path``: the unit describes its Linux target, never
    the host generating it.
    """
    return systemd_directory(
        str(PurePosixPath(script).parent), what="WorkingDirectory="
    )


def cron_quote(value: str) -> str:
    r"""*value* as one double-quoted ``sh`` word, with crontab(5)'s backslash before each ``%``.

    Inside double quotes ``sh`` still reads ``$``, a backtick, ``"`` and ``\``, so each gets
    a backslash; a value ending in ``\`` therefore still ends its word with a quote. Not
    ``shlex.quote``: cron keeps a backslash before anything but ``%``, so a ``\%`` inside
    single quotes could not survive it.
    """
    quoted = "".join("\\" + char if char in '\\"$`' else char for char in value)
    return '"' + quoted.replace("%", "\\%") + '"'


def cron_word(word: str) -> str:
    """A cron command argument: bare when :func:`is_bare_word` holds, else quoted."""
    return word if is_bare_word(word) else cron_quote(word)


def windows_quote(word: str) -> str:
    r"""*word* as one double-quoted Windows command-line argument (``CommandLineToArgvW``).

    Backslashes are literal except before a ``"``: so a run of them before an embedded
    ``"`` is doubled and the quote backslash-escaped, and a run at the end is doubled so
    the closing quote stays a quote. ``"`` is never written as ``""``, which runtimes
    disagree on.
    """
    out, backslashes = ['"'], 0
    for char in word:
        if char == "\\":
            backslashes += 1
            continue
        if char == '"':
            out.append("\\" * (2 * backslashes + 1) + '"')
        else:
            out.append("\\" * backslashes + char)
        backslashes = 0
    out.append("\\" * (2 * backslashes) + '"')
    return "".join(out)


def windows_word(word: str) -> str:
    """A Windows command-line argument: bare when :func:`is_bare_word` holds, else quoted."""
    return word if is_bare_word(word) else windows_quote(word)
