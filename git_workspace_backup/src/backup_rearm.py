"""The re-arm step of RESTORE.md: what to run once a restored checkout is in place.

A bundle carries no ``.git/config``, no hooks and no virtualenvs, so a restored workspace
needs some commands run before it works as it did. Which commands is the workspace's own
business: the config's ``rearm_commands`` names them, and when it is
absent the step names only this tool's own install line. The default derives that line from
where the entry script sits: relative to the workspace when the script lies inside it, as the
step changes into the restored workspace first; absolute when it does not, with ``--repo``
naming the workspace, since the script would otherwise back up the checkout it sits in, and
prose saying those paths are where they were when the snapshot was taken; and a placeholder
when no workspace is known, as when a document is rendered outside a run.

Every command is written into a ``text`` fence, never a run block, so a command is a person's
step and nothing here executes it. A fence still ends at a line that begins with a fence
marker, so :func:`rearm_command_problem` refuses an empty command, a fence marker, and any
character in Unicode categories Cc (control characters, line breaks among them), Zl and Zp
(the line and paragraph separators) and Cf (format characters: bidi overrides, zero-width
characters, the BOM), which could start a new line or make the text the restorer reads
differ from the text that runs. Config validation and the renderer both apply it.

The config's optional ``setup_doc`` names the workspace's setup document, relative to the
workspace root, and the step's prose then tells the restorer to rebuild from it; without it
the prose stays generic. It is named in backticks, so :func:`setup_doc_problem` applies the
command rule, refuses a backtick, and refuses a path that is absolute or climbs with ``..``.
"""

from __future__ import annotations

import shlex
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

#: The entry script's file name; the folder it sits in may be renamed, so it is not derived.
ENTRY_SCRIPT_NAME = "git_workspace_backup.py"
#: The entry script, beside this private module's folder.
ENTRY_SCRIPT = Path(__file__).resolve().parent.parent / ENTRY_SCRIPT_NAME
REARM_HEADING = "## What a bundle does not carry"
#: The step changes into the restored workspace first, so relative commands resolve there.
_CD_WORKSPACE = 'cd "$RESTORE_ROOT/workspace"'
_FENCE_MARKER = "```"
#: Unicode categories that can break, hide or reorder a line: control characters, the line
#: and paragraph separators, and format characters.
_BREAKING_CATEGORIES = frozenset({"Cc", "Zl", "Zp", "Cf"})
_BACKTICK = "`"
_TAB = "\t"
_PARENT = ".."
#: Every variant opens by re-arming the hooks, the one step that fails silently when missed.
_OPENING = (
    "A bundle holds no `.git/config`, no hooks and no virtualenvs. Re-arm the git hooks"
)
#: Why the hooks come first, in words true of any workspace's hooks.
_HOOKS_FIRST = "first: a hook that is missing fails silently."
#: With neither commands nor a setup document configured: the wording names nothing of any
#: particular workspace.
_DEFAULT_PROSE = (
    _OPENING,
    f"{_HOOKS_FIRST} Then set up again whatever",
    "else the workspace built from those, and re-install this backup's scheduled agent:",
)
_REBUILD = f"{_HOOKS_FIRST} Then rebuild the"
_GENERIC_SOURCE = "as the workspace's own setup notes describe"
#: How each variant ends, after the clause naming what to rebuild from.
_CONFIGURED_ENDING = (
    "and run in order the re-arm commands the workspace's configuration names. Then",
    "re-install any scheduled agent they leave out, including this backup's:",
)
_DEFAULT_ENDING = ("and re-install this backup's scheduled agent:",)
#: Replaces the closing colon when the default line names absolute paths.
_SNAPSHOT_PATHS = (
    "These paths are where they were when the snapshot was taken, so a restore to another",
    "place adjusts them:",
)


@dataclass(frozen=True)
class ReArm:
    """The re-arm step's commands, whether the config named them, and its setup document."""

    commands: tuple[str, ...]
    configured: bool
    #: The workspace's setup document, relative to its root; None for the generic wording.
    setup_doc: "str | None" = None
    #: True when the default line names the script and workspace absolutely, as they were
    #: when the snapshot was taken.
    absolute: bool = False


def holds_hidden_character(text: str, *, allow_tab: bool = False) -> bool:
    """Whether *text* holds a character that can break, hide or reorder a line.

    *allow_tab* exempts the tab, which can neither start a line nor hide text.
    """
    return any(
        unicodedata.category(char) in _BREAKING_CATEGORIES
        and not (allow_tab and char == _TAB)
        for char in text
    )


# fuzz-target
def rearm_command_problem(command: str) -> "str | None":
    """Why *command* cannot be written into the re-arm fence, or None when it can."""
    if not command:
        return "is empty"
    if _FENCE_MARKER in command:
        return "holds a Markdown fence marker"
    if holds_hidden_character(command):
        return "holds a line break or another control character"
    return None


# fuzz-target
def setup_doc_problem(setup_doc: str) -> "str | None":
    """Why *setup_doc* cannot be named in the step's prose, or None when it can."""
    problem = rearm_command_problem(setup_doc)
    if problem is not None:
        return problem
    if _BACKTICK in setup_doc:
        return "holds a backtick"
    # A root or a drive on either platform: the path is named from the restored workspace.
    if setup_doc.startswith(("/", "\\")) or setup_doc[1:2] == ":":
        return "is absolute"
    if _PARENT in setup_doc.replace("\\", "/").split("/"):
        return "climbs out of the workspace with .."
    return None


def install_line(script: str, repo: "str | None" = None) -> str:
    """The command installing this tool's agent, for an already-quoted *script* and *repo*."""
    line = f"python {script} --mode install"
    return line if repo is None else f"{line} --repo {repo}"


def default_rearm(workspace: "Path | None", script: Path = ENTRY_SCRIPT) -> ReArm:
    """Only this tool's install line, naming *script* as seen from the restored *workspace*.

    A script outside *workspace* gets ``--repo`` naming it: without that, the reinstalled
    agent would back up the checkout the script sits in instead.
    """
    if workspace is None:
        return ReArm((install_line(f"<path to {script.name}>"),), configured=False)
    root = workspace.resolve()
    if script.is_relative_to(root):
        named = shlex.quote(script.relative_to(root).as_posix())
        return ReArm((install_line(named),), configured=False)
    line = install_line(shlex.quote(str(script)), repo=shlex.quote(str(root)))
    return ReArm((line,), configured=False, absolute=True)


def rearm_for(
    configured: "Sequence[str] | None",
    workspace: Path,
    setup_doc: "str | None" = None,
) -> ReArm:
    """The config's commands when it names any, else the default for *workspace*.

    *setup_doc*, the config's setup document, is carried either way.
    """
    if configured is None:
        return replace(default_rearm(workspace), setup_doc=setup_doc)
    return ReArm(tuple(configured), configured=True, setup_doc=setup_doc)


def _prose(rearm: ReArm) -> "tuple[str, ...]":
    """The step's prose, ending with the snapshot-paths note when the line is absolute."""
    lines = _wording(rearm)
    if not rearm.absolute:
        return lines
    return (*lines[:-1], lines[-1].removesuffix(":") + ".", *_SNAPSHOT_PATHS)


def _wording(rearm: ReArm) -> "tuple[str, ...]":
    """The step's wording: generic unless the config named commands or a setup document."""
    if not rearm.configured and rearm.setup_doc is None:
        return _DEFAULT_PROSE
    source = (
        _GENERIC_SOURCE
        if rearm.setup_doc is None
        else f"as `{rearm.setup_doc}` describes"
    )
    ending = _CONFIGURED_ENDING if rearm.configured else _DEFAULT_ENDING
    return (_OPENING, _REBUILD, f"virtualenvs and prerequisites {source},", *ending)


def rearm_section(rearm: ReArm) -> str:
    """The step's Markdown; its values must already pass this module's two rules."""
    return "\n".join(
        [
            REARM_HEADING,
            "",
            *_prose(rearm),
            "",
            f"{_FENCE_MARKER}text",
            _CD_WORKSPACE,
            *rearm.commands,
            _FENCE_MARKER,
        ]
    )
