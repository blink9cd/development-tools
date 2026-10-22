"""What an installed agent's plist says it runs: its repository, and its pinned destination.

Read back from the plist itself, never from the configuration or a checkout, so the line
uninstall prints names the repository that agent really ran against, even when the
configuration is broken or ``--repo`` names another. The plist is read as a regular file,
bounded, and never through a symlink, like every other file the tool reads back. Status reads
the destination install pinned the same way, and names it when it is not the one judged.

The parse runs with no entity guard, which is acceptable here for three reasons together: the
read is bounded at ``PLIST_READ_LIMIT`` (64 KiB), only the user and install write the file,
and Expat, which plistlib parses XML with, applies its own billion-laughs amplification limit.
"""

from __future__ import annotations

import plistlib
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from backup_config import canonical_path
from backup_fs import read_regular_file
from backup_json import apfs_name_key

#: The most a plist read back may hold: far more than any plist install writes.
PLIST_READ_LIMIT = 1 << 16
#: The option naming the repository among an agent's arguments.
_REPO_OPTION = "--repo"
#: The option naming the destination install pinned among an agent's arguments.
_DESTINATION_OPTION = "--destination"
#: Unicode categories never printed raw: controls, format characters (bidi overrides among
#: them) and the line and paragraph separators.
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
#: ProgramArguments' first two entries: the interpreter, then the script it runs.
_SCRIPT_INDEX = 1


class AgentArgsError(Exception):
    """The plist exists but does not say what the agent runs; the message says why."""


@dataclass(frozen=True)
class InstalledAgent:
    """What an installed agent runs: its script, and the repository ``--repo`` names."""

    script: str
    #: None when the agent named no repository, so it backed up its script's checkout.
    repo: "str | None"
    #: None when the agent was installed before install pinned its destination.
    destination: "str | None" = None


def read_installed_agent(plist: Path) -> "InstalledAgent | None":
    """What the agent at *plist* runs; None when there is no plist.

    AgentArgsError when the plist cannot be read, or does not say what the agent runs.
    """
    try:
        data, _ = read_regular_file(plist.parent, plist.name, limit=PLIST_READ_LIMIT)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AgentArgsError(f"{plist} cannot be read: {exc}") from exc
    return installed_agent_from(data)


def installed_agent_from(data: bytes) -> InstalledAgent:
    """The script and repository plist *data* runs; AgentArgsError when it does not say."""
    try:
        loaded = plistlib.loads(data)
    except Exception as exc:
        # plistlib documents no closed set of errors for bytes that are not a plist: XML,
        # binary-format and value errors all reach here. Each is reported, never swallowed.
        raise AgentArgsError(f"not a plist: {exc}") from exc
    args = loaded.get("ProgramArguments") if isinstance(loaded, dict) else None
    if not (
        isinstance(args, list)
        and len(args) > _SCRIPT_INDEX
        and all(isinstance(arg, str) for arg in args)
    ):
        raise AgentArgsError("its ProgramArguments are not an interpreter and a script")
    options = args[_SCRIPT_INDEX + 1 :]
    return InstalledAgent(
        script=args[_SCRIPT_INDEX],
        repo=_option_value(options, _REPO_OPTION),
        destination=_option_value(options, _DESTINATION_OPTION),
    )


def _option_value(options: "list[str]", name: str) -> "str | None":
    """The value the first *name* among *options* gives, or None when none gives one."""
    # The value is the argument after the option; an option ending the list names nothing.
    named = [value for option, value in zip(options, options[1:]) if option == name]
    return named[0] if named else None


def pinned_destination_problems(
    plist: Path, judged: Path, err: TextIO
) -> "tuple[str, ...]":
    """The status problem the agent at *plist* raises by pinning a destination not *judged*.

    Empty when there is no agent, or it was installed before the pin. A plist that cannot say
    is a warning on *err*, as uninstall treats it, never a refusal: the status still judges.
    """
    try:
        agent = read_installed_agent(plist)
    except AgentArgsError as exc:
        print(
            "WARNING: cannot tell which destination the installed agent writes to: "
            f"{exc}",
            file=err,
        )
        return ()
    if agent is None or agent.destination is None:
        return ()
    if _same_folder(Path(agent.destination), judged):
        return ()
    return (
        f"the installed agent writes to {_shown(agent.destination)}; "
        f"this status judges {judged}",
    )


def _shown(value: str) -> str:
    """*value* as printed: escaped by ``repr`` when it holds a hidden character, else as is.

    A plist value is read back from a file the user may edit, so a control, format or
    separator character in it must not reach a terminal raw, where it could make the line
    shown differ from the value held. Install refuses such characters, so a value it wrote
    prints unchanged.
    """
    hidden = any(unicodedata.category(char) in _HIDDEN_CATEGORIES for char in value)
    return repr(value) if hidden else value


def _same_folder(first: Path, second: Path) -> bool:
    """Whether two spellings name one folder: resolved, then compared as APFS compares names."""
    return apfs_name_key(str(canonical_path(first))) == apfs_name_key(
        str(canonical_path(second))
    )


def removal_report(agent: InstalledAgent, named: "Path | None") -> "tuple[str, ...]":
    """The lines saying what the removed *agent* backed up, and whether *named* was it.

    Compared resolved, as the agent's repository was when install baked it, so one folder
    spelled two ways is not called another.
    """
    backed_up = (
        f"the checkout holding {_shown(agent.script)}"
        if agent.repo is None
        else _shown(agent.repo)
    )
    lines = [f"The removed agent was backing up {backed_up}."]
    if named is not None and (
        agent.repo is None or canonical_path(Path(agent.repo)) != canonical_path(named)
    ):
        lines.append(
            f"--repo names {named}, not the repository the removed agent was backing up; "
            "there is one agent per account, so it is removed all the same."
        )
    return tuple(lines)
