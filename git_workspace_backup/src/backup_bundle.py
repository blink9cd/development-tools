"""Create one repository bundle and record what a restore of it must reproduce.

``git bundle create --all`` records every ref under ``refs/`` (branches, tags, the stash)
and each linked worktree's HEAD as ``worktrees/<name>/HEAD``. The refs present before
bundling are compared with the bundle's own ``list-heads``, so a bundle that silently lost
a ref is a failed item rather than a snapshot. A worktree head is recorded with whether a
branch already reaches it, because a restore must anchor the ones no branch does.

``refs/stash`` names only the newest stash entry; the older ones live in its reflog, which a
bundle never carries. So each entry's commit is handed to ``bundle create`` as an unnamed
revision, which packs its objects without naming them, and the entries are recorded in the
bundle entry, oldest first, for a restore to store back.

Every name git prints, a ref or a worktree's name, is data: recorded and compared, never
joined onto a filesystem path or handed to a shell. The parsers keep only records of the
shape git writes, so a sha handed back to git is a full object id and never text that reads
as an option. A git query that fails, times out or cannot start raises rather than reading
as "no refs": an empty listing would let an unbundled repository pass for an empty one.
"""

from __future__ import annotations

import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from backup_json import is_safe_relative_name
from backup_model import (
    BundleEntry,
    DirtyWorktree,
    FailedItem,
    StashEntry,
    WorktreeHead,
)
from backup_proc import GIT_TIMEOUT_S, RunCmd

WORKTREE_PREFIX = "worktrees/"
WORKTREE_SUFFIX = "/HEAD"
#: Recorded when a worktree's status cannot be read (its directory is gone, say).
UNREADABLE_COUNT = -1
#: Appended to a bundle's failure when its file was written anyway. The file is kept, never
#: deleted: a bundle that failed only its listing may be the run's one restorable copy.
KEPT_BUNDLE_NOTE = "; unverified bundle kept at "

_REFS_PREFIX = "refs/"
_HEAD = "HEAD"
STASH_REF = "refs/stash"
#: The stash reflog, each entry as ``<sha>NUL<message>``: a message holds spaces. A reflog
#: walk rather than ``stash list``, which refuses a bare repository; the trailing ``--``
#: keeps the ref from being read as a path.
_STASH_LOG_ARGV = (
    "git",
    "log",
    "--walk-reflogs",
    "--format=%H%x00%gs",
    STASH_REF,
    "--",
)
#: Under ``worktree list --porcelain -z`` each worktree's first record is this plus its path.
_WORKTREE_RECORD = "worktree "
#: Hex SHA-1 and SHA-256 object names.
_OBJECT_ID_LENGTHS = frozenset({40, 64})
_HEX_DIGITS = frozenset("0123456789abcdef")
#: git refuses a ref name holding a space (0x20), any control character below it, or DEL.
_LOWEST_NAME_CODEPOINT = 0x21
_DELETE_CODEPOINT = 0x7F
#: ``--no-optional-locks`` stops status refreshing a stale index under ``index.lock``, the
#: lock a commit being made in that worktree needs. The explicit untracked mode counts
#: untracked files even where ``status.showUntrackedFiles=no`` would hide them.
_STATUS_ARGV = (
    "git",
    "--no-optional-locks",
    "status",
    "--porcelain",
    "--untracked-files=normal",
)


class BundleError(Exception):
    """A git query needed to bundle or describe a repository failed."""


@dataclass(frozen=True)
class BundleResult:
    """A bundle entry, a failure, or neither: an empty repository allowed to be skipped."""

    entry: "BundleEntry | None"
    failure: "FailedItem | None"


def list_refs(repo: Path, *, run: RunCmd) -> tuple[str, ...]:
    """Every ref under ``refs/`` in *repo*, sorted; BundleError when git fails."""
    argv = ["git", "for-each-ref", "--format=%(refname)", _REFS_PREFIX]
    return parse_refs(_git_output(argv, repo, "for-each-ref", run=run))


def list_heads(bundle: Path, repo: Path, *, run: RunCmd) -> dict[str, str]:
    """The ref names *bundle* records, mapped to their shas; BundleError when git fails."""
    argv = ["git", "bundle", "list-heads", str(bundle)]
    return parse_heads(_git_output(argv, repo, "bundle list-heads", run=run))


# fuzz-target
def parse_refs(out: str) -> tuple[str, ...]:
    """The ref names in ``for-each-ref --format=%(refname)`` output *out*, sorted.

    A line that is not a ref name under ``refs/`` is skipped. Never raises.
    """
    lines = out.split("\n")
    return tuple(
        sorted(
            line
            for line in lines
            if line.startswith(_REFS_PREFIX) and _is_ref_name(line)
        )
    )


# fuzz-target
def parse_heads(out: str) -> dict[str, str]:
    """The ``<sha> <name>`` lines of ``git bundle list-heads`` output *out*, as name -> sha.

    A line is kept only when its sha is a full lowercase object id and its name a ref name;
    a bare sha, a short or uppercase one, or a name holding a space is skipped. A later line
    for the same name wins. Never raises.
    """
    heads: dict[str, str] = {}
    for line in out.split("\n"):
        sha, _, name = line.partition(" ")
        if is_object_id(sha) and _is_ref_name(name):
            heads[name] = sha
    return heads


def list_stashes(repo: Path, *, run: RunCmd) -> tuple[StashEntry, ...]:
    """Every stash entry of *repo*, oldest first; BundleError when git fails.

    Call it only for a repository whose refs include STASH_REF: without that ref git exits
    non-zero, which raises here.
    """
    return parse_stash_log(_git_output(_STASH_LOG_ARGV, repo, "stash log", run=run))


# fuzz-target
def parse_stash_log(out: str) -> tuple[StashEntry, ...]:
    """The ``<sha>NUL<message>`` lines of the stash reflog *out*, oldest first.

    git walks the reflog newest first, so the order is reversed for a restore to store the
    entries as listed. A line without a NUL, or whose sha is not a full lowercase object id,
    is skipped. Never raises.
    """
    entries: list[StashEntry] = []
    for line in out.split("\n"):
        sha, separator, message = line.partition("\0")
        if separator and is_object_id(sha):
            entries.append(StashEntry(sha, message))
    return tuple(reversed(entries))


# fuzz-target
def worktree_name(ref: str) -> "str | None":
    """The ``<name>`` of a ``worktrees/<name>/HEAD`` ref, or None for any other ref.

    The name must be one non-empty component. It is display data, never a path.
    """
    if not (ref.startswith(WORKTREE_PREFIX) and ref.endswith(WORKTREE_SUFFIX)):
        return None
    # Sliced by explicit end, not a negative index: in "worktrees/HEAD" the prefix and the
    # suffix share their "/", and the slice comes out empty rather than wrapping around.
    name = ref[len(WORKTREE_PREFIX) : len(ref) - len(WORKTREE_SUFFIX)]
    if not name or "/" in name:
        return None
    return name


def worktree_heads(
    repo: Path, heads: Mapping[str, str], *, run: RunCmd
) -> tuple[WorktreeHead, ...]:
    """The ``worktrees/<name>/HEAD`` entries of *heads*, each marked reachable or not.

    Reachable means a branch or tag already contains the commit. BundleError when that query
    fails: a failure read as "no branch contains it" would be a guess, not a record.
    """
    found: list[WorktreeHead] = []
    for ref, sha in sorted(heads.items()):
        name = worktree_name(ref)
        if name is None:
            continue
        # "--contains=<sha>" binds the value, so it can never be read as an option of its own.
        argv = [
            "git",
            "for-each-ref",
            f"--contains={sha}",
            "--format=%(refname)",
            "refs/heads",
            "refs/tags",
        ]
        out = _git_output(argv, repo, f"for-each-ref --contains {sha}", run=run)
        found.append(WorktreeHead(name=name, sha=sha, reachable=bool(out.strip())))
    return tuple(found)


def create_bundle(
    repo: Path,
    snapshot: Path,
    rel_name: str,
    *,
    label: str,
    require_refs: bool,
    run: RunCmd,
) -> BundleResult:
    """Bundle *repo* to ``snapshot/rel_name`` and describe what its restore must hold.

    A repository with no refs is skipped, as neither entry nor failure, unless
    *require_refs*. An unsafe *rel_name* is a failure before any git command runs; a failed
    git query and a bundle missing a listed ref are failures too. A failure that leaves a
    bundle file behind names it with KEPT_BUNDLE_NOTE.
    """
    if not is_safe_relative_name(rel_name):
        return BundleResult(
            None, FailedItem(label, f"unsafe bundle name: {rel_name!r}")
        )
    try:
        refs = list_refs(repo, run=run)
        if not refs:
            reason = "repository has no refs to bundle"
            return BundleResult(
                None, FailedItem(label, reason) if require_refs else None
            )
        entry = _bundle_and_verify(repo, snapshot / rel_name, rel_name, refs, run=run)
    # OSError: the bundle's directory could not be made. It is this bundle's failure, not
    # the run's: the other repositories still get their bundles.
    except (BundleError, OSError) as exc:
        reason = _naming_a_kept_file(str(exc), snapshot, rel_name)
        return BundleResult(None, FailedItem(label, reason))
    return BundleResult(entry, None)


def dirty_worktrees(repo: Path, *, run: RunCmd) -> tuple[DirtyWorktree, ...]:
    """Every worktree of *repo* with uncommitted changes, by directory name.

    A worktree whose status cannot be read (its directory is gone, or git fails or times
    out there) is listed with UNREADABLE_COUNT, never dropped or read as clean.
    BundleError when the listing itself fails or names no worktree, since *repo* is one.
    Needs git 2.36 or later, the first to accept ``worktree list -z``; an older git exits
    non-zero on it, which raises here rather than listing nothing.
    """
    argv = ["git", "worktree", "list", "--porcelain", "-z"]
    paths = parse_worktree_paths(_git_output(argv, repo, "worktree list", run=run))
    if not paths:
        raise BundleError(f"git worktree list named no worktree in {repo}")
    dirty: list[DirtyWorktree] = []
    for text in paths:
        worktree = Path(text)
        count = _changed_path_count(worktree, run=run)
        if count:
            dirty.append(DirtyWorktree(worktree.name, count))
    return tuple(dirty)


# fuzz-target
def parse_worktree_paths(out: str) -> tuple[str, ...]:
    """The worktree paths in ``git worktree list --porcelain -z`` output *out*, in order.

    ``-z`` ends every record with a NUL, so a path holding a newline stays one record; it
    needs git 2.36 or later. A ``worktree`` record with an empty path is skipped. Never
    raises.
    """
    return tuple(
        record[len(_WORKTREE_RECORD) :]
        for record in out.split("\0")
        if record.startswith(_WORKTREE_RECORD) and len(record) > len(_WORKTREE_RECORD)
    )


def _bundle_and_verify(
    repo: Path, out: Path, rel_name: str, refs: tuple[str, ...], *, run: RunCmd
) -> BundleEntry:
    """Write the bundle to *out* and describe it; BundleError if git fails or a ref is lost."""
    out.parent.mkdir(parents=True, exist_ok=True)
    stashes = list_stashes(repo, run=run) if STASH_REF in refs else ()
    # Each sha is a full object id, so it can never be read as an option.
    argv = ["git", "bundle", "create", str(out), "--all"]
    argv += [stash.sha for stash in stashes]
    _git_output(argv, repo, "bundle create", run=run)
    heads = list_heads(out, repo, run=run)
    missing = sorted(set(refs) - set(heads))
    if missing:
        raise BundleError("bundle lacks refs: " + ", ".join(missing))
    return BundleEntry(
        name=rel_name,
        head=heads.get(_HEAD, ""),
        refs=refs,
        worktree_heads=worktree_heads(repo, heads, run=run),
        stashes=stashes,
    )


def _naming_a_kept_file(reason: str, snapshot: Path, rel_name: str) -> str:
    """*reason*, plus the kept file's name when the failed bundle was written to disk.

    An explicit ``lstat`` that reads any error as "no kept file", rather than ``Path.is_file``:
    3.11 and 3.12 raise for an error outside a short list (a name too long, say), and later
    interpreters (3.14 measured) return False. Raised from here, it would make one bundle's
    failure the whole run's.
    """
    try:
        kept = stat.S_ISREG((snapshot / rel_name).lstat().st_mode)
    except OSError:
        kept = False
    if kept:
        return f"{reason}{KEPT_BUNDLE_NOTE}{rel_name}"
    return reason


def _changed_path_count(worktree: Path, *, run: RunCmd) -> int:
    """How many paths status lists in *worktree*; UNREADABLE_COUNT when status fails."""
    code, out, _ = run(_STATUS_ARGV, worktree, GIT_TIMEOUT_S)
    if code != 0:
        return UNREADABLE_COUNT
    # Without -z porcelain v1 quotes an awkward path and prints a rename as one line, so
    # each non-empty line is exactly one changed path.
    return sum(1 for line in out.split("\n") if line)


def _git_output(argv: Sequence[str], cwd: Path, what: str, *, run: RunCmd) -> str:
    """Stdout of *argv* run in *cwd*; BundleError naming *what* on any non-zero exit.

    A timeout (124) and a git that could not start (127) are non-zero exits like any other.
    """
    code, out, err = run(argv, cwd, GIT_TIMEOUT_S)
    if code != 0:
        raise BundleError(f"git {what} failed in {cwd} (exit {code}): {err.strip()}")
    return out


def is_object_id(text: str) -> bool:
    """Whether *text* is a full lowercase hex SHA-1 or SHA-256 object name."""
    return len(text) in _OBJECT_ID_LENGTHS and _HEX_DIGITS.issuperset(text)


def _is_ref_name(text: str) -> bool:
    """Whether *text* is non-empty and holds no space, control character or DEL."""
    return bool(text) and all(
        ord(char) >= _LOWEST_NAME_CODEPOINT and ord(char) != _DELETE_CODEPOINT
        for char in text
    )
