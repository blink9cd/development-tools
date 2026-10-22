"""Make a run's staging folder fresh: sweep what an earlier run left, then make its own.

Kept apart from backup_run, which decides what a run comes to. A staging folder that cannot be
made fresh leaves nothing to promote, so every refusal here is a :class:`StagingError`, which
the run records as failed. Called only while the run lock is held, as sweep_staging requires.
"""

from __future__ import annotations

import stat
from pathlib import Path

from backup_fs import PRIVATE_DIR_MODE, make_private_dirs
from backup_promote import sweep_staging


class StagingError(Exception):
    """Staging cannot be made fresh, so there is nothing to promote; the message says why."""


def fresh_staging(staging_root: Path, name: str) -> Path:
    """A new, empty ``staging_root/name``, once every stale staging folder is gone.

    Raises StagingError when *staging_root* exists as anything but a plain directory, or cannot
    be examined -- the children of a symlink's target are not staging's to sweep or fill, and
    sweep_staging quietly sweeps nothing there -- when a stale folder cannot be removed, or
    when the folder already exists. It is made without ``exist_ok``, because a file found in
    staging is taken as this build's own (a failed bundle's file is named as kept), which is
    true only of a folder this run created.
    """
    refusal = "so the run refuses to sweep it"
    try:
        mode = staging_root.lstat().st_mode
    except FileNotFoundError:
        mode = None
    except OSError as exc:
        raise StagingError(
            f"staging root cannot be examined, {refusal}: {exc}"
        ) from exc
    if mode is not None and not stat.S_ISDIR(mode):
        raise StagingError(
            f"staging root is not a plain directory, {refusal}: {staging_root}"
        )
    try:
        sweep_staging(staging_root)
    except OSError as exc:
        raise StagingError(f"staging sweep failed in {staging_root}: {exc}") from exc
    make_private_dirs(staging_root)
    staging = staging_root / name
    try:
        staging.mkdir(mode=PRIVATE_DIR_MODE)
    except FileExistsError as exc:
        raise StagingError(f"staging folder already exists: {staging}") from exc
    return staging
