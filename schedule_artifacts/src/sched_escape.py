r"""The escapes and refusals that keep a path a path in the systemd units and the cron line.

Private to schedule_artifacts: its builders call these, and nothing else imports them. One
rule set per format, because each consumer reads a different set of characters specially:
systemd's unit parser, its specifier and variable expansion, and cron's ``%`` and ``sh``.

The one value no escape can save is ``WorkingDirectory=``: systemd takes the rest of that
line as the path, unquoted, so only ``%`` can be escaped there. Its unit-file parser joins a
line ending in an unescaped ``\`` to the next (``systemd.syntax(7)``), which would fold the
key below into the path, and it strips a value's surrounding whitespace; so a directory
ending in ``\``, or starting or ending in whitespace, is refused rather than written.
"""

from __future__ import annotations

from pathlib import PurePosixPath


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


def systemd_work_dir(script: str) -> str:
    r"""The ``WorkingDirectory=`` value for *script*: its POSIX parent, ``%`` doubled.

    Raises ValueError when that directory is not absolute (systemd reads a leading ``~``
    as the user's home and a leading ``-`` as "ignore if missing"), or ends in ``\`` or
    starts or ends in whitespace, which systemd would read as a different path or as the
    next line's key (module docstring). ``PurePosixPath``, not ``Path``: the unit describes its Linux target, never
    the host generating it.
    """
    work = str(PurePosixPath(script).parent)
    if not work.startswith("/") or work.endswith("\\") or work != work.strip():
        raise ValueError(
            "WorkingDirectory= needs an absolute directory with no trailing backslash "
            f"and no surrounding whitespace: {work!r}"
        )
    return work.replace("%", "%%")


def cron_quote(value: str) -> str:
    r"""*value* as one double-quoted ``sh`` word, with crontab(5)'s backslash before each ``%``.

    Inside double quotes ``sh`` still reads ``$``, a backtick, ``"`` and ``\``, so each gets
    a backslash; a value ending in ``\`` therefore still ends its word with a quote. Not
    ``shlex.quote``: cron keeps a backslash before anything but ``%``, so a ``\%`` inside
    single quotes could not survive it.
    """
    quoted = "".join("\\" + char if char in '\\"$`' else char for char in value)
    return '"' + quoted.replace("%", "\\%") + '"'
