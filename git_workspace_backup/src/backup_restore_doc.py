"""Render RESTORE.md for one snapshot, and extract the blocks in it that run.

Every ``sh`` fenced block runs verbatim once SNAPSHOT and RESTORE_ROOT are set to absolute
paths, and the round-trip test executes exactly those blocks, so the document cannot drift
from the commands that actually restore. Steps that overwrite a live home directory, or
depend on the restored machine, stay in ``text`` blocks: those are a person's decision, not
a script's. Placing what the restore puts under a Claude project directory (each restored
memory corpus, and whatever the archive holds beneath ``claude/projects/<slug>/``) is one of
them, since that directory is named for where the workspace was restored.

A block may be run with ``sh -c`` or pasted into an interactive shell, and an interactive
shell does not exit when a command fails or a ``${NAME:?message}`` expansion fails: it moves
on to the next line. A block that stored a path in a variable and then failed to set it would
go on to run ``git -C ""``, which is git in the current directory. So each block is one subshell
under ``set -eu``, which its first failure ends in every shell (measured in ``sh``, ``bash``
and ``zsh``, run both ways), and every command reads RESTORE_ROOT and SNAPSHOT itself as
``${NAME:?message}``, never through a stored path: even a line that did run on would fail
its own expansion. Every block then refuses a RESTORE_ROOT or a SNAPSHOT that is not
absolute, before any other command: a relative one names somewhere beneath whatever
directory the shell is in, and a block pasted on its own checks nothing else. The first block
also refuses a RESTORE_ROOT that is not an existing, readable, empty directory, listing it
through ``command ls`` so a user's ``ls`` alias or function cannot turn the answer around;
later blocks see what earlier ones restored, so they do not check. Each refusal echoes its
message as separate words, so the text an interactive shell echoes back from a paste never
holds the message it prints.

Extraction takes a fence only when it opens on a line that is exactly the marker followed by
``sh`` and closes on a line that is exactly the marker. So a ``text``, ``shell`` or ``sh
anything`` fence never runs, and a line beginning with the marker before the closer (a
fence left open, or a closer with anything after it) yields nothing from that opener: a
broken block never swallows the fences that follow it.

A mirror clone is used because a plain ``git clone`` of a bundle makes only the default
branch local, files the rest under ``origin/*`` and drops the stash. The mirror keeps
``refs/stash`` but no stash reflog, so the block then stores every recorded stash entry back
(backup_restore_stash). A worktree's head sits
outside ``refs/*``, so the mirror copies its objects without naming them; a named fetch
anchors each one before garbage collection can remove it. That fetch uses verification's own
refspec, so a restore lands each head on the very ref verification proved, with the name
mid-refname: git refuses a ref ending in a dot, and ``x.`` is a worktree name git writes.

The checkout names no commit, so it keeps the HEAD the mirror clone chose: a branch whose tip
is the bundle's HEAD commit, or that commit detached when no branch tips it. Checking out the
recorded sha would detach every restore. The block then proves HEAD is the recorded head, the
same property verification proved of the same mirror clone before the snapshot was promoted.

Every manifest value the document writes is checked first, by backup_restore_checks:
whether it could end a fence or hide what runs, and where each block may write.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence

from backup_build import EXTRAS_ZIP, PREFIX_CLAUDE
from backup_machine import is_valid_label
from backup_model import BundleEntry, FileEntry, Manifest
from backup_rearm import ReArm, default_rearm, rearm_section
from backup_restore_checks import (
    EXTRAS_DIR,
    MEMORY_PREFIX,
    PATH_SEPARATOR,
    RestoreDocError,
    checked_refspecs,
    refuse_unrenderable,
    refuse_unsafe_names,
    refuse_unsafe_rearm,
    restore_dir,
)
from backup_restore_checks import restore_targets_overlap as restore_targets_overlap
from backup_restore_notes import claude_entries, second_machine_section
from backup_restore_stash import stash_restore_commands

RESTORE_DOC_NAME = "RESTORE.md"
#: Where Claude keeps per-project directories, each named by a slug, relative to its home.
_PROJECTS_DIR = "projects"
#: An archive member beneath this prefix sits inside a project directory named by a slug.
_ARCHIVED_PROJECTS_PREFIX = f"{PREFIX_CLAUDE}/{_PROJECTS_DIR}/"
#: How every command reads its two variables: unset or empty, the expansion fails right there.
_RESTORE_ROOT = '"${RESTORE_ROOT:?set RESTORE_ROOT to an absolute empty directory}"'
_SNAPSHOT = '"${SNAPSHOT:?set SNAPSHOT to this snapshot folder}"'
_SH_OPENER = "```sh"
_FENCE_CLOSER = "```"
#: Every run block's body opens a subshell that its first failure ends, and closes it.
_SUBSHELL_OPENING = ("(", "set -eu")
_SUBSHELL_CLOSING = ")"
#: Every run block's first commands: RESTORE_ROOT, then SNAPSHOT, must be absolute. Each
#: message is echoed as separate words, so the text of a pasted block never holds it.
_ABSOLUTE_GUARDS = tuple(
    f'case {expansion} in /*) ;; *) echo "{name} must be absolute:" '
    '"set it as the first lines of this document show" >&2; false ;; esac'
    for name, expansion in (("RESTORE_ROOT", _RESTORE_ROOT), ("SNAPSHOT", _SNAPSHOT))
)
#: The first run block's next command: RESTORE_ROOT must be an existing, readable, empty
#: directory. ``command`` bypasses a user's ``ls`` alias or function, either of which can
#: turn the listing's answer around, and an unreadable root would list as empty.
_EMPTY_ROOT_CHECK = (
    f"[ -d {_RESTORE_ROOT} ] && [ -r {_RESTORE_ROOT} ] && [ -x {_RESTORE_ROOT} ] && "
    f'[ -z "$(command ls -A -- {_RESTORE_ROOT})" ] || '
    '{ echo "RESTORE_ROOT must be empty:" '
    '"set it to an existing, readable, empty directory" >&2; false; }'
)
#: A line that is exactly ```sh, body lines none of which begins with ```, then a line that
#: is exactly ```. A body line beginning with ``` fails the match from that opener.
_SH_BLOCK = re.compile(r"^```sh\n((?:(?!```).*\n)*?)```$", re.MULTILINE)


# fuzz-target
def render_restore_doc(manifest: Manifest, *, rearm: "ReArm | None" = None) -> str:
    """The complete RESTORE.md for *manifest*, its re-arm step from *rearm*.

    With no *rearm*, the step is the default for an unknown workspace (backup_rearm).

    Raises RestoreDocError, before anything is rendered, for a value holding a fence marker
    or a character that can break, hide or reorder a line (a tab passes in a stash message
    alone); a machine that is not a machine label (backup_machine); a bundle name
    that does not end in ``.bundle``, or that or whose restore
    directory is not a safe relative name; a worktree name that is not one ref component; a
    head that is neither empty nor a full lowercase object id; a stash entry whose sha is not
    one or whose message could end a fence; a duplicate bundle name; two
    write targets that are equal or nested, names compared as APFS compares them; a bundle
    name the build never writes; an archived project slug or a Claude file name that could
    end a fence or hide what runs; a re-arm command
    that ``rearm_command_problem`` refuses; or a setup document that
    ``setup_doc_problem`` refuses.
    """
    refuse_unrenderable("created", manifest.created)
    if manifest.machine is not None and not is_valid_label(manifest.machine):
        raise RestoreDocError(f"machine is not a machine label: {manifest.machine!r}")
    rearm = default_rearm(None) if rearm is None else rearm
    refuse_unsafe_rearm(rearm)
    archived_slugs = _archived_project_slugs(manifest.archive_members)
    for slug in archived_slugs:
        refuse_unrenderable("archived project slug", slug)
    claude_files = claude_entries(manifest.archive_members)
    for name in claude_files:
        refuse_unrenderable("Claude file name", name)
    specs = [checked_refspecs(entry) for entry in manifest.bundles]
    refuse_unsafe_names([entry.name for entry in manifest.bundles])
    pairs = zip(manifest.bundles, specs, strict=True)
    sections = [
        _intro(manifest),
        *(
            _bundle_section(entry, entry_specs, first=index == 0)
            for index, (entry, entry_specs) in enumerate(pairs)
        ),
        _files_section(first=not manifest.bundles),
        second_machine_section(claude_files),
        *_project_folder_sections(manifest.bundles, archived_slugs),
        rearm_section(rearm),
    ]
    return "\n\n".join(sections) + "\n"


# fuzz-target
def extract_sh_blocks(markdown: str) -> tuple[str, ...]:
    """The bodies of every well-formed fence whose info string is exactly ``sh``, in order.

    Well-formed: opened by a line that is exactly ```sh and closed by the next line that
    begins with ```, which must be exactly ```. Any other such line yields nothing from that
    opener. Other fences are not tracked, so a line that is exactly ```sh inside a ``text``
    fence still opens a block: only this module's own output, which refuses line breaks and
    fence markers in every value it writes, is safe to extract.
    """
    return tuple(_SH_BLOCK.findall(markdown))


def _run_block(commands: Sequence[str], *, first: bool) -> "list[str]":
    """An ``sh`` fence holding *commands* in one subshell that its first failure ends.

    Every block refuses relative paths first; only the first also refuses a root in use.
    """
    checks = [_EMPTY_ROOT_CHECK] if first else []
    return [
        _SH_OPENER,
        *_SUBSHELL_OPENING,
        *_ABSOLUTE_GUARDS,
        *checks,
        *commands,
        _SUBSHELL_CLOSING,
        _FENCE_CLOSER,
    ]


def _intro(manifest: Manifest) -> str:
    # "CDPATH=" keeps an exported CDPATH from making cd print a second path into the value.
    return "\n".join(
        [
            "# Restoring this snapshot",
            "",
            f"Created {manifest.created}{_taken_by(manifest)}; "
            f"complete: {'yes' if manifest.complete else 'no'}.",
            "",
            "Every `sh` block below runs as written once two variables are set, each to an",
            "ABSOLUTE path: `SNAPSHOT` is this folder, `RESTORE_ROOT` an empty directory to",
            "restore into. Every block refuses a relative path, which would name somewhere",
            "beneath whatever directory your shell is in, and a path beginning with a dash is",
            "refused or read as an option. The blocks need `git` and `unzip`, nothing else.",
            "",
            "Each block is one subshell under `set -eu`, run with `sh -c` or pasted into `sh`,",
            "`bash` or `zsh`. Its first failing command ends that block, and so does its first",
            "use of a variable that is unset or empty, as one is when a `cd` below fails; the",
            "shell you pasted into stays open. Every command names `RESTORE_ROOT` itself, so",
            "none falls back to the current directory, and the first block stops unless",
            "`RESTORE_ROOT` is an existing, readable, empty directory.",
            "",
            "If the first block fails, stop: fix `RESTORE_ROOT` and start again from the first",
            "block. Every later block assumes it ran, and pasting on runs each one regardless.",
            "",
            "```text",
            'export SNAPSHOT="$(CDPATH= cd -- "<path to this folder>" && pwd)"',
            'export RESTORE_ROOT="$(CDPATH= cd -- "<empty directory>" && pwd)"',
            "```",
        ]
    )


def _taken_by(manifest: Manifest) -> str:
    """`` on machine `<label>``` for a snapshot that names its machine; "" for an older one."""
    return "" if manifest.machine is None else f" on machine `{manifest.machine}`"


def _bundle_section(entry: BundleEntry, specs: tuple[str, ...], *, first: bool) -> str:
    # "--" ends git's options before the bundle path, so clone never reads that path as an
    # option. It is no substitute for the absolute SNAPSHOT the intro requires: fetch still
    # refuses a repository path that begins with a dash, "--" or not.
    bundle = f"-- {_SNAPSHOT}/{shlex.quote(entry.name)}"
    target = f"{_RESTORE_ROOT}/{shlex.quote(restore_dir(entry.name))}"
    commands = [
        f"git clone --quiet --mirror {bundle} {target}/.git",
        f"git -C {target} config --bool core.bare false",
        f"git -C {target} config --remove-section remote.origin",
    ]
    if entry.head:
        # No commit named, so the checkout keeps the HEAD the mirror clone chose: a branch
        # tipped at the bundle's HEAD commit, or that commit detached. Naming the sha would
        # detach every restore. The recorded head is proved instead of assumed.
        head = shlex.quote(entry.head)
        commands += [
            f"git -C {target} checkout --quiet --force",
            f'[ "$(git -C {target} rev-parse HEAD)" = {head} ] || '
            f'{{ echo "the restored HEAD is not" "the recorded head" {head} >&2; false; }}',
        ]
    for spec in specs:
        commands.append(f"git -C {target} fetch --quiet {bundle} {shlex.quote(spec)}")
    commands += stash_restore_commands(target, entry.stashes)
    lines = [
        f"## {entry.name}",
        "",
        "A mirror clone restores every ref and becomes a working checkout; each stash entry",
        "is then stored back, oldest first.",
        "",
        *_run_block(commands, first=first),
    ]
    return "\n".join(lines)


def _files_section(*, first: bool) -> str:
    extras = f"{_RESTORE_ROOT}/{EXTRAS_DIR}"
    commands = [
        f"mkdir -p {extras}",
        f"unzip -q -o {_SNAPSHOT}/{shlex.quote(EXTRAS_ZIP)} -d {extras}",
    ]
    return "\n".join(
        [
            "## Files",
            "",
            *_run_block(commands, first=first),
            "",
            "The archive holds three prefixes: `workspace/` belongs in the restored workspace,",
            "`claude/` in `~/.claude`, and `home/` in the home directory. Placing them overwrites",
            "live files, so it is left to you:",
            "",
            "```text",
            f'cd "$RESTORE_ROOT/{EXTRAS_DIR}"',
            'cp -R workspace/. "$RESTORE_ROOT/workspace/"',
            'cp -R claude/. "$HOME/.claude/"',
            'cp -R home/. "$HOME/"',
            "```",
        ]
    )


def _archived_project_slugs(members: Sequence[FileEntry]) -> tuple[str, ...]:
    """Each project slug the archive holds something inside, in first-seen order, once.

    Derived from the members rather than the configured globs, so the step names exactly what
    this snapshot holds. A file sitting directly in ``claude/projects/`` names no slug.
    """
    slugs: dict[str, None] = {}
    for member in members:
        rest = member.name.removeprefix(_ARCHIVED_PROJECTS_PREFIX)
        slug, separator, _ = rest.partition(PATH_SEPARATOR)
        if rest != member.name and separator:
            slugs[slug] = None
    return tuple(slugs)


def _project_folder_sections(
    bundles: Sequence[BundleEntry], archived_slugs: Sequence[str]
) -> tuple[str, ...]:
    """The manual step naming what the restore places under a project slug; none when nothing is.

    A text step, never a run block: placing any of it writes into the live Claude home, under
    a directory name that depends on where the workspace was restored. Each path is quoted,
    so it reads as one shell word whatever the slug holds.
    """
    corpora = [
        restore_dir(entry.name)
        for entry in bundles
        if entry.name.startswith(MEMORY_PREFIX)
    ]
    archived = [
        f"{EXTRAS_DIR}/{_ARCHIVED_PROJECTS_PREFIX}{slug}" for slug in archived_slugs
    ]
    if not (corpora or archived):
        return ()
    lines = [
        "## Claude project folders",
        "",
        "Everything below belongs under `~/.claude/projects/<slug>/`. Each memory bundle",
        "restored its corpus beneath `RESTORE_ROOT`, where Claude does not look for it; a",
        "corpus belongs at `~/.claude/projects/<slug>/memory`. Each archived project folder",
        "holds what the Files step's `claude/` copy puts under the slug recorded here.",
        "`<slug>` is Claude's encoding of the restored workspace's absolute path, so on a",
        "machine with a different home path the directory all of it belongs in is named",
        "differently from the one recorded here, and Claude will not find it under the old",
        "name. Placing it writes into `~/.claude`, so it is left to you. Restored:",
        "",
        "```text",
        *(f'"$RESTORE_ROOT"/{shlex.quote(path)}' for path in (*corpora, *archived)),
        "```",
    ]
    return ("\n".join(lines),)
