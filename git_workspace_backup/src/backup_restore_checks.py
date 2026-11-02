"""The refusals that keep RESTORE.md's manifest values from changing what its blocks run.

Kept apart from backup_restore_doc, which renders the document: everything here decides only
whether a value may be rendered at all, and raises RestoreDocError when it may not.

Manifest values are data, and each one written into a command is shell-quoted. Quoting
cannot stop a value from ending its fence, though: a line break or a fence marker would
close the ``sh`` block early and make the text after it a block of its own, which runs. Nor
does it stop a value from showing a terminal something other than what runs: an escape
sequence, a backspace or a bidi control can redraw or reorder the text a restorer reads
before pasting it. So every manifest string the document writes must pass the re-arm
commands' own rule (backup_rearm) and hold no fence marker; a stash message alone may hold a
tab, which git can write into one and which hides nothing.

Where a block may write. Keeping each path lexically beneath its root is not enough, and
neither is an empty RESTORE_ROOT at the start: the blocks run in order and fill it, and a
restored checkout may hold symlinks, so a block whose target lay inside an earlier block's
target could be carried through one out of RESTORE_ROOT: a ``workspace.bundle`` committing
``evil -> ../..``, then a ``workspace/evil.bundle``. So a rendered document holds to three
rules, each refused when broken:

- a bundle's name ends in ``.bundle``, and both the name, joined onto SNAPSHOT, and its
  restore directory (:func:`restore_dir`), joined onto RESTORE_ROOT, are safe relative names
  (no empty, ``.`` or ``..`` component, no leading slash, no NUL), so neither join leaves
  its root lexically;
- every name is one the build writes (``workspace.bundle``, or ``memory/<slug>.bundle``
  whose slug is one safe path component), and none appears twice;
- no two write targets, each bundle's restore directory and the fixed ``extras`` directory
  the files block unzips into, are equal or nested by path component, so no target lies
  inside another block's target, where that block's checkout could have left a symlink.

Names and targets are compared as APFS compares them, ignoring case and normalization form,
since two spellings it reads as one folder would restore into one directory. The closed set
of names implies the other two rules today. Both stay, so that widening the set to a new
bundle shape can never by itself reopen an escape. Not claimed: a checkout's own symlinks are
restored as committed. Also refused: a worktree name that is not one ref component, and a
head or stash entry that is not a full object id.
"""

from __future__ import annotations

from collections.abc import Sequence

from backup_build import BUNDLE_SUFFIX, MEMORY_DIR, WORKSPACE_BUNDLE
from backup_bundle import is_object_id
from backup_json import apfs_name_key, is_safe_relative_name
from backup_model import BundleEntry
from backup_rearm import (
    ReArm,
    holds_hidden_character,
    rearm_command_problem,
    setup_doc_problem,
)
from backup_verify import worktree_refspec

MEMORY_PREFIX = f"{MEMORY_DIR}/"
PATH_SEPARATOR = "/"
#: The directory beneath RESTORE_ROOT that the files block unzips into.
EXTRAS_DIR = "extras"
#: Directories beneath RESTORE_ROOT that a run block writes whatever the manifest holds.
_FIXED_TARGETS = (EXTRAS_DIR,)
_FENCE_MARKER = "```"


class RestoreDocError(ValueError):
    """A manifest value that cannot be written into RESTORE.md without changing what runs."""


def restore_dir(bundle_name: str) -> str:
    """Where a bundle restores, relative to RESTORE_ROOT; not itself checked for safety."""
    stem = bundle_name.removesuffix(BUNDLE_SUFFIX)
    if stem.startswith(MEMORY_PREFIX):
        return f"claude/projects/{stem[len(MEMORY_PREFIX) :]}/memory"
    return stem


def restore_targets_overlap(first: str, second: str) -> bool:
    """Whether one restore target is, or lies within, the other, compared by path component.

    Each component is compared as APFS compares names. ``workspace`` overlaps itself,
    ``Workspace`` and ``workspace/evil``; ``work`` does not overlap ``workspace``.
    """
    first_parts = [apfs_name_key(part) for part in first.split(PATH_SEPARATOR)]
    second_parts = [apfs_name_key(part) for part in second.split(PATH_SEPARATOR)]
    shorter = min(len(first_parts), len(second_parts))
    return first_parts[:shorter] == second_parts[:shorter]


def refuse_unrenderable(what: str, value: str, *, allow_tab: bool = False) -> None:
    """Raise RestoreDocError when *value* could end a fence or hide or reorder its line."""
    if _FENCE_MARKER in value or holds_hidden_character(value, allow_tab=allow_tab):
        raise RestoreDocError(
            f"{what} could end a Markdown fence or hide what runs: {value!r}"
        )


def checked_refspecs(entry: BundleEntry) -> tuple[str, ...]:
    """Each worktree head's refspec, once every value of *entry* is safe to render."""
    refuse_unrenderable("bundle name", entry.name)
    if not entry.name.endswith(BUNDLE_SUFFIX):
        raise RestoreDocError(
            f"bundle name does not end in {BUNDLE_SUFFIX}: {entry.name!r}"
        )
    # The name joins onto SNAPSHOT and its restore directory onto RESTORE_ROOT. An empty
    # stem gives an empty restore directory, which is refused here too.
    if not (
        is_safe_relative_name(entry.name)
        and is_safe_relative_name(restore_dir(entry.name))
    ):
        raise RestoreDocError(
            f"bundle {entry.name!r} would not restore beneath SNAPSHOT and RESTORE_ROOT"
        )
    # A bundle whose source HEAD was unborn records no head, and gets no checkout line.
    if entry.head and not is_object_id(entry.head):
        raise RestoreDocError(
            f"head of {entry.name!r} is not a full object id: {entry.head!r}"
        )
    for stash in entry.stashes:
        refuse_unrenderable("stash message", stash.message, allow_tab=True)
        if not is_object_id(stash.sha):
            raise RestoreDocError(f"stash entry is not a full object id: {stash.sha!r}")
    specs: list[str] = []
    for head in entry.worktree_heads:
        refuse_unrenderable("worktree name", head.name)
        spec = worktree_refspec(head.name)
        if spec is None:
            raise RestoreDocError(
                f"worktree name is not a single ref component: {head.name!r}"
            )
        specs.append(spec)
    return tuple(specs)


def refuse_unsafe_rearm(rearm: ReArm) -> None:
    """Raise RestoreDocError for the first re-arm value the step cannot hold."""
    for command in rearm.commands:
        problem = rearm_command_problem(command)
        if problem is not None:
            raise RestoreDocError(f"re-arm command {problem}: {command!r}")
    if rearm.setup_doc is not None:
        problem = setup_doc_problem(rearm.setup_doc)
        if problem is not None:
            raise RestoreDocError(f"setup document {problem}: {rearm.setup_doc!r}")


def refuse_unsafe_names(names: Sequence[str]) -> None:
    """Raise RestoreDocError for a duplicate, an overlap, or a name the build never writes.

    The rules that hold whatever the names are come first, then the closed set of names the
    build writes. Each layer alone refuses a nested restore; in this order every one of the
    three refusals can be reached, and told apart by its message.
    """
    _refuse_duplicate_names(names)
    _refuse_overlapping_targets(names)
    _refuse_unbuilt_names(names)


def _refuse_duplicate_names(names: Sequence[str]) -> None:
    """Raise RestoreDocError when two bundle names are one name as APFS compares them."""
    keys = [apfs_name_key(name) for name in names]
    duplicates = sorted({name for name, key in zip(names, keys) if keys.count(key) > 1})
    if duplicates:
        raise RestoreDocError(f"duplicate bundle names: {duplicates!r}")


def _refuse_overlapping_targets(names: Sequence[str]) -> None:
    """Raise RestoreDocError when two write targets are equal or one lies within the other."""
    targets = [*_FIXED_TARGETS, *(restore_dir(name) for name in names)]
    for index, first in enumerate(targets):
        for second in targets[index + 1 :]:
            if restore_targets_overlap(first, second):
                raise RestoreDocError(
                    f"restore targets overlap: {first!r} and {second!r}"
                )


def _refuse_unbuilt_names(names: Sequence[str]) -> None:
    """Raise RestoreDocError for the first bundle name the build never writes."""
    for name in names:
        if not _is_built_bundle_name(name):
            raise RestoreDocError(f"not a bundle name the build writes: {name!r}")


def _is_built_bundle_name(name: str) -> bool:
    """Whether *name* is the workspace bundle, or ``memory/<slug>.bundle`` for one slug.

    The slug must be one safe path component. It may itself end in ``.bundle``, as a
    project directory the build snapshots may.
    """
    if name == WORKSPACE_BUNDLE:
        return True
    if not (name.startswith(MEMORY_PREFIX) and name.endswith(BUNDLE_SUFFIX)):
        return False
    slug = name[len(MEMORY_PREFIX) : -len(BUNDLE_SUFFIX)]
    return PATH_SEPARATOR not in slug and is_safe_relative_name(slug)
