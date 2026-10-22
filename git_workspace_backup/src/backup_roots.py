"""Resolve the three roots a run reads from: the primary checkout, the Claude home, home.

The workspace root is the PRIMARY checkout, found by :func:`repo_root_of`
(``--path-format=absolute --git-common-dir``), never the checkout the script happens to sit
in: a linked worktree holds none of the gitignored extras, and a worktree cleanup can later
delete it. That is also why installing from a linked worktree is refused -- see
is_linked_worktree.

:func:`repo_root_of` and :class:`GitError` are a copy of a shared git library's, not an
import: one short function did not justify vendoring that library into a tool that ships on
its own.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from backup_proc import RunGit

CLAUDE_HOME_NAME = ".claude"
#: What a refused layout's message says of its work tree.
_NOT_PARENT = "is not the parent of its git directory"


class RootsError(Exception):
    """The start path is not inside a git checkout git can resolve."""


class GitError(Exception):
    """git was unusable, or a query this module needs failed."""


@dataclass(frozen=True)
class Roots:
    """Where a run reads from."""

    workspace: Path
    claude_home: Path
    home: Path


def resolve_roots(start: Path, *, home: Path, run_git: RunGit) -> Roots:
    """The primary checkout containing *start*, plus the Claude home and home."""
    return Roots(
        workspace=_primary_checkout(start, run_git),
        claude_home=home / CLAUDE_HOME_NAME,
        home=home,
    )


def is_linked_worktree(path: Path, *, run_git: RunGit) -> bool:
    """True when *path* sits in a linked worktree rather than in the primary checkout."""
    code, out, err = run_git(["rev-parse", "--show-toplevel"], path)
    if code != 0:
        raise RootsError(
            f"git rev-parse --show-toplevel failed in {path}: {err.strip()}"
        )
    return Path(out.strip()).resolve() != _primary_checkout(path, run_git).resolve()


def repo_root_of(run: RunGit, path: Path) -> Path:
    """The main working tree's root for the repository containing *path*.

    Uses ``--path-format=absolute --git-common-dir``, which resolves to the SHARED admin dir
    even when *path* is inside a linked worktree, so every worktree of one repository
    answers with the same root.
    """
    out = _git_ok(
        run,
        ("rev-parse", "--path-format=absolute", "--git-common-dir"),
        path,
        "git rev-parse --git-common-dir",
    )
    common = Path(out.strip())
    if not common.name:  # pragma: no cover - defensive; git always names the dir
        raise GitError(f"unexpected --git-common-dir output: {out!r}")
    return common.parent


def _git_ok(run: RunGit, args: Sequence[str], cwd: Path, what: str) -> str:
    """Run git and return stdout, raising :class:`GitError` on a non-zero exit."""
    code, out, err = run(args, cwd)
    if code != 0:
        raise GitError(f"{what} failed (exit {code}): {err.strip() or out.strip()}")
    return out


def _primary_checkout(path: Path, run_git: RunGit) -> Path:
    """:func:`repo_root_of` for *path*, refused unless it is the work tree of that git dir."""
    try:
        root = repo_root_of(run_git, path)
        _require_work_tree_at(run_git, root, path)
    except GitError as exc:
        raise RootsError(str(exc)) from exc
    return root


def _require_work_tree_at(run: RunGit, root: Path, path: Path) -> None:
    """Raise GitError unless git, asked in *root*, calls it a work tree sharing *path*'s git dir.

    A submodule keeps its git dir under the superproject's ``.git/modules/`` and
    ``--separate-git-dir`` puts it anywhere, so the parent of the common dir is then no work
    tree, or a different repository's. Comparing both answers covers that other repository
    sitting exactly there, such as a home directory kept in git.
    """
    query = ("rev-parse", "--path-format=absolute", "--git-common-dir")
    common = _git_ok(run, query, path, "git rev-parse --git-common-dir").strip()
    code, out, err = run(("rev-parse", "--show-toplevel", *query[1:]), root)
    answers = out.split("\n")[:2] if code == 0 else []
    if [Path(answer).resolve() for answer in answers] != [
        root.resolve(),
        Path(common).resolve(),
    ]:
        # git's own refusal, when it gave one, is kept: it also tells a git that failed for
        # another reason apart from this layout.
        said = " ".join(err.split())
        answered = f" (git rev-parse --show-toplevel there: {said})" if code else ""
        raise GitError(
            f"the repository at {path} has a work tree that {_NOT_PARENT} {common}: a "
            "submodule or a repository made with --separate-git-dir, which this tool "
            f"cannot back up{answered}"
        )
