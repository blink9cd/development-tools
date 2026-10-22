"""Build one snapshot into a staging directory: memory pre-step, bundles, extras.zip, drift.

``memory_snapshot --mode snapshot`` runs first when the config's ``memory_snapshot`` key is
on. Its Stop hook commits a corpus only when a session ends, so without this step a backup
taken mid-session would miss every memory edit since. The key gates only that pre-step:
memory corpora are bundled either way. Every later item is independent: one failure is
recorded and the rest proceed.

A bundle is listed only when its result carries no failure. A failed bundle can still leave a
whole file in staging -- ``list-heads`` runs after the file is written -- so a file on disk
is never evidence that its bundle is good.

Nothing unreadable reads as absent. A projects directory that exists but cannot be listed,
and an archive that cannot be written, are failed items rather than raised errors. A memory
project whose ``memory/.git`` cannot be examined is bundled anyway, so the reason it could
not be backed up is recorded. That probe is an explicit ``stat`` rather than
``Path.is_dir``, which on 3.14 returns False for a permission error -- measured: an
unreadable project vanished from the build without a word.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from backup_bundle import BundleError, BundleResult, create_bundle, dirty_worktrees
from backup_config import BackupConfig
from backup_drift import DriftError, list_ignored, unclassified
from backup_model import BundleEntry, DirtyWorktree, FailedItem, FileEntry
from backup_proc import MEMORY_SNAPSHOT_TIMEOUT_S, RunCmd
from backup_roots import Roots
from backup_zip import ZipResult, ZipSource, write_zip

#: The suffix of every bundle a build writes, and so the one a restore recognizes.
BUNDLE_SUFFIX = ".bundle"
WORKSPACE_BUNDLE = f"workspace{BUNDLE_SUFFIX}"
MEMORY_DIR = "memory"
EXTRAS_ZIP = "extras.zip"
#: Path of memory_snapshot's CLI under the directory memory_tools_dir names.
MEMORY_SNAPSHOT_SCRIPT = ("memory_snapshot", "memory_snapshot.py")
PREFIX_WORKSPACE = "workspace"
PREFIX_CLAUDE = "claude"
PREFIX_HOME = "home"
MEMORY_SNAPSHOT_ITEM = "memory_snapshot"
DIRTY_WORKTREES_ITEM = "dirty_worktrees"
DRIFT_ITEM = "drift"
_PROJECTS = "projects"
_GIT_DIR = ".git"
#: The errors that mean a path is not there, as opposed to "cannot tell".
_ABSENT = (FileNotFoundError, NotADirectoryError)


def memory_tools_dir(script: Path) -> Path:
    """The directory holding memory_snapshot's folder, for the tool at *script*.

    The folder the tool's own folder sits in, the repository root: one fixed place, since a
    search of the parent directories would also run a same-named script planted above it.
    """
    return script.parent.parent


@dataclass(frozen=True)
class BuildRequest:
    """Everything one build reads from."""

    roots: Roots
    config: BackupConfig
    staging: Path
    tools_dir: Path
    python: str
    #: This machine's identity file, never archived whatever entry reaches it; None for none.
    identity_file: "Path | None" = None


@dataclass(frozen=True)
class BuildResult:
    """What a build produced and what went wrong along the way."""

    bundles: tuple[BundleEntry, ...]
    archive_members: tuple[FileEntry, ...]
    dirty_worktrees: tuple[DirtyWorktree, ...]
    failed: tuple[FailedItem, ...]
    skipped_symlinks: tuple[str, ...]
    unclassified_ignored: tuple[str, ...]


def snapshot_memory(request: BuildRequest, *, run: RunCmd) -> "FailedItem | None":
    """Commit every memory corpus now; a failed item when memory_snapshot does not exit 0."""
    script = request.tools_dir.joinpath(*MEMORY_SNAPSHOT_SCRIPT)
    code, out, err = run(
        [
            request.python,
            str(script),
            "--mode",
            "snapshot",
            "--home",
            str(request.roots.home),
        ],
        None,
        MEMORY_SNAPSHOT_TIMEOUT_S,
    )
    if code == 0:
        return None
    lines = (err or out).strip().splitlines()
    last = lines[-1] if lines else "no output"
    return FailedItem(MEMORY_SNAPSHOT_ITEM, f"exit {code}: {last}")


def memory_repos(claude_home: Path) -> tuple[tuple[str, Path], ...]:
    """Each ``projects/<slug>/memory`` that is, or cannot be ruled out as, a git repository.

    No projects directory means no repositories. OSError when it exists but cannot be
    listed. A project whose ``memory/.git`` cannot be examined is included, so bundling it
    records why rather than the corpus going unbacked-up in silence.
    """
    projects = claude_home / _PROJECTS
    try:
        listed = sorted(projects.iterdir())
    except _ABSENT:
        return ()
    return tuple(
        (project.name, project / MEMORY_DIR)
        for project in listed
        if _may_be_repository(project / MEMORY_DIR)
    )


def zip_sources(request: BuildRequest) -> tuple[ZipSource, ...]:
    """The three whitelisted roots, each under its archive prefix.

    Each excludes the identity file when it lies beneath that root, so no configured entry
    can carry this machine's identity to the next one set up from the snapshot.
    """
    roots, config = request.roots, request.config
    sources = (
        (PREFIX_WORKSPACE, roots.workspace, config.extras),
        (PREFIX_CLAUDE, roots.claude_home, config.claude_files),
        (PREFIX_HOME, roots.home, config.home_files),
    )
    return tuple(
        ZipSource(prefix, root, patterns, _beneath(root, request.identity_file))
        for prefix, root, patterns in sources
    )


def _beneath(root: Path, path: "Path | None") -> frozenset[str]:
    """*path* as a POSIX name relative to *root*, when it lies beneath it; else none.

    Both sides are resolved, so a root given through a symlink still finds the file, but the
    file's own name is not: a symlink in its place is never followed by the walk anyway.
    """
    if path is None:
        return frozenset()
    real = os.path.join(os.path.realpath(path.parent), path.name)
    rel = os.path.relpath(real, os.path.realpath(root))
    if rel == os.pardir or rel.startswith(f"{os.pardir}{os.sep}"):
        return frozenset()
    return frozenset({Path(rel).as_posix()})


def build_snapshot(request: BuildRequest, *, run: RunCmd) -> BuildResult:
    """Build every item into ``request.staging``; each failure is an item, never a raise."""
    failed: list[FailedItem] = []
    # Gated by config, not by whether the script exists: a configured step that cannot run
    # must stay a recorded failure, never a silent skip.
    if request.config.memory_snapshot:
        memory_failure = snapshot_memory(request, run=run)
        if memory_failure is not None:
            failed.append(memory_failure)
    bundles = _bundles(request, run, failed)
    dirty = _dirty(request, run, failed)
    drift = _drift(request, run, failed)
    zipped = _archive(request, failed)
    return BuildResult(
        bundles=bundles,
        archive_members=zipped.members,
        dirty_worktrees=dirty,
        failed=tuple(failed),
        skipped_symlinks=zipped.skipped_symlinks,
        unclassified_ignored=drift,
    )


def _may_be_repository(repo: Path) -> bool:
    """False only when *repo* provably holds no ``.git`` directory."""
    try:
        return stat.S_ISDIR((repo / _GIT_DIR).stat().st_mode)
    except _ABSENT:
        return False
    except OSError:
        return True  # cannot be examined: attempted, so its failure is recorded


def _bundles(
    request: BuildRequest, run: RunCmd, failed: list[FailedItem]
) -> tuple[BundleEntry, ...]:
    """Bundle the workspace and each memory repository; list only the bundles that held."""
    results: list[BundleResult] = [
        create_bundle(
            request.roots.workspace,
            request.staging,
            WORKSPACE_BUNDLE,
            label=PREFIX_WORKSPACE,
            require_refs=True,
            run=run,
        )
    ]
    for slug, repo in _memory_repos_or_failure(request, failed):
        results.append(
            create_bundle(
                repo,
                request.staging,
                f"{MEMORY_DIR}/{slug}{BUNDLE_SUFFIX}",
                label=f"{MEMORY_DIR}:{slug}",
                require_refs=False,
                run=run,
            )
        )
    entries: list[BundleEntry] = []
    for result in results:
        # A failure wins over an entry: a bundle recorded as failed is never verified.
        if result.failure is not None:
            failed.append(result.failure)
        elif result.entry is not None:
            entries.append(result.entry)
    return tuple(entries)


def _memory_repos_or_failure(
    request: BuildRequest, failed: list[FailedItem]
) -> tuple[tuple[str, Path], ...]:
    """The memory repositories, or none and a failed item when they cannot be listed."""
    try:
        return memory_repos(request.roots.claude_home)
    except OSError as exc:
        failed.append(FailedItem(MEMORY_DIR, f"cannot list memory projects: {exc}"))
        return ()


def _dirty(
    request: BuildRequest, run: RunCmd, failed: list[FailedItem]
) -> tuple[DirtyWorktree, ...]:
    """The workspace's dirty worktrees, or none and a failed item when git cannot list them."""
    try:
        return dirty_worktrees(request.roots.workspace, run=run)
    except BundleError as exc:
        failed.append(FailedItem(DIRTY_WORKTREES_ITEM, str(exc)))
        return ()


def _drift(
    request: BuildRequest, run: RunCmd, failed: list[FailedItem]
) -> tuple[str, ...]:
    """The unclassified ignored paths, or none and a failed item when git cannot list them."""
    try:
        ignored = list_ignored(request.roots.workspace, run=run)
    except DriftError as exc:
        failed.append(FailedItem(DRIFT_ITEM, str(exc)))
        return ()
    return unclassified(
        ignored, extras=request.config.extras, regenerable=request.config.regenerable
    )


def _archive(request: BuildRequest, failed: list[FailedItem]) -> ZipResult:
    """Write extras.zip; an archive that cannot be written is a failed item and no members."""
    try:
        zipped = write_zip(request.staging / EXTRAS_ZIP, zip_sources(request))
    except OSError as exc:
        failed.append(FailedItem(EXTRAS_ZIP, f"archive could not be written: {exc}"))
        return ZipResult((), (), ())
    failed.extend(zipped.failed)
    return zipped
