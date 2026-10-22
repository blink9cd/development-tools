"""The prose-only sections of RESTORE.md: what a person decides, never a block that runs.

Kept apart from backup_restore_doc, whose run blocks and refusals are what make a restore
safe. The one manifest value here is each Claude file name the archive holds, which the
renderer checks before this module writes it into a ``text`` fence.
"""

from __future__ import annotations

from collections.abc import Sequence

from backup_build import PREFIX_CLAUDE
from backup_machine import IDENTITY_SHOWN
from backup_model import FileEntry

_CLAUDE_PREFIX = f"{PREFIX_CLAUDE}/"
#: Claude's per-project folders, which the project-folder step names instead.
_PROJECTS_PREFIX = f"{_CLAUDE_PREFIX}projects/"
_SEPARATOR = "/"


def claude_entries(members: Sequence[FileEntry]) -> tuple[str, ...]:
    """Each top-level name under ``claude/`` the archive holds, sorted, a folder with its slash.

    Derived from the archive's members, so the note names exactly what this snapshot holds.
    Anything beneath ``claude/projects/`` is left to the project-folder step, which names it.
    """
    entries: set[str] = set()
    for member in members:
        if not member.name.startswith(_CLAUDE_PREFIX) or member.name.startswith(
            _PROJECTS_PREFIX
        ):
            continue
        top, separator, _ = member.name[len(_CLAUDE_PREFIX) :].partition(_SEPARATOR)
        entries.add(f"{_CLAUDE_PREFIX}{top}{separator}")
    return tuple(sorted(entries))


def second_machine_section(claude_files: Sequence[str]) -> str:
    """The note qualifying the Files step's ``claude/`` copy of *claude_files* on a second machine.

    It also says the second machine takes its own identity, so its snapshots never share a
    name with this machine's in one destination, and re-installing the agent there is safe;
    and that a replacement keeps this machine's label instead, so the old snapshots stay its.
    """
    return "\n".join(
        [
            "## Setting up a second machine",
            "",
            *_claude_files_note(claude_files),
            "Any memory corpus or project folder this snapshot holds is that machine's own",
            "too, since memory holds machine facts, so a second machine does not place it",
            "either.",
            "",
            "A second machine takes its own machine identity. Its first backup run derives",
            f"one from its host name and keeps it in `{IDENTITY_SHOWN}`, which no snapshot",
            "carries, so its snapshots are named apart from this machine's even in one shared",
            "destination. If both machines have the same host name, edit that file on one.",
            "A machine replacing this one instead writes this machine's label, named above",
            "beside `Created`, into that file before its first backup run, so its old",
            "snapshots stay its own; a genuinely new machine lets the first run derive one.",
        ]
    )


def _claude_files_note(claude_files: Sequence[str]) -> "list[str]":
    """What the archive's ``claude/`` copy holds, and what a second machine does with it."""
    opening = "This snapshot restores the machine it was taken on."
    if not claude_files:
        return [opening, "Its archive holds no file under `claude/`.", ""]
    return [
        opening,
        "Its archive holds these Claude files, which the Files step's `claude/` copy places:",
        "",
        "```text",
        *claude_files,
        "```",
        "",
        "Files like these can carry the machine's own facts, such as its scheduled agents",
        "and local paths. A second machine that is not replacing this one places only those",
        "it means to share, after reading each.",
    ]
