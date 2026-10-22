"""Turn the CLI's path options into the checkout a mode backs up, refusing what cannot be used.

Every refusal is a :class:`Refused` whose message is the one line the CLI prints. A path option
is refused before git runs when it is relative: it would name a different directory from each
working directory the CLI starts in, the agent's included. ``--repo`` is resolved by the same
route as the script's own checkout, so a linked worktree means its primary checkout.
"""

from __future__ import annotations

from pathlib import Path

from backup_config import (
    BackupConfig,
    ConfigError,
    canonical_path,
    expand_home,
    load_config,
)
from backup_proc import RunCmd, git_runner
from backup_roots import Roots, RootsError, is_linked_worktree, resolve_roots

#: The option naming the repository to back up, as the CLI spells it.
REPO_OPTION = "--repo"


class Refused(Exception):
    """This invocation cannot run as asked; the message is the one line printed."""


def repo_start(raw: "str | None", *, default: Path, home: Path) -> Path:
    """Where the checkout is looked for: *raw* when given, else *default*.

    Refused for a relative *raw*, and for one that is not an existing directory, which git
    could not even start in.
    """
    start = named_repo(raw, home=home)
    if start is None:
        return default
    if not start.is_dir():
        raise Refused(f"{REPO_OPTION} must name an existing directory, got {raw!r}")
    return start


def named_repo(raw: "str | None", *, home: Path) -> "Path | None":
    """*raw* with ``~`` expanded; None when not given, and refused when relative."""
    if raw is None:
        return None
    named = expand_home(raw, home)
    if not named.is_absolute():
        raise Refused(f"{REPO_OPTION} must be absolute or start with ~/, got {raw!r}")
    return named


def destination_override(raw: "str | None", home: Path) -> "Path | None":
    """--destination resolved once, by the rule a configured one is; None when not given.

    Resolved like the configuration's, so one directory keys one ledger however it was
    spelled.
    """
    if raw is None:
        return None
    expanded = expand_home(raw, home)
    if not expanded.is_absolute():
        raise Refused(f"--destination must be absolute or start with ~/, got {raw!r}")
    return canonical_path(expanded)


def checkout_and_config(
    start: Path, *, home: Path, run: RunCmd, named: bool
) -> tuple[Roots, BackupConfig]:
    """The primary checkout containing *start*, and its configuration as written.

    Refuses a start outside any git checkout and a missing or invalid configuration. *named*
    says *start* came from ``--repo``, so a refusal names the option that chose it.
    """
    try:
        roots = resolve_roots(start, home=home, run_git=git_runner(run))
    except RootsError as exc:
        prefix = f"{REPO_OPTION} {start}: " if named else ""
        raise Refused(f"{prefix}{exc}") from exc
    try:
        config = load_config(roots.workspace, home=home)
    except ConfigError as exc:
        raise Refused(str(exc)) from exc
    return roots, config


def in_linked_worktree(start: Path, *, run: RunCmd, outside_ok: bool) -> bool:
    """Whether *start* sits in a linked worktree rather than in a primary checkout.

    With *outside_ok*, a *start* git cannot place in any checkout is not a linked worktree;
    otherwise it is refused.
    """
    try:
        return is_linked_worktree(start, run_git=git_runner(run))
    except RootsError as exc:
        if outside_ok:
            return False
        raise Refused(str(exc)) from exc
