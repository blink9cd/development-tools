"""Seal a staged snapshot: verify it by restoring into scratch of its own, then write its records.

Kept apart from backup_run, which decides what a run comes to. A problem with what is being
sealed -- a restore that does not verify, a file that cannot be hashed, a document that
refuses to render, scratch that cannot be made or removed -- is a failed item on the snapshot,
because the staged snapshot may be the only copy there is and is still promoted, marked
incomplete. A problem writing the records themselves is not: an ``OSError`` from the atomic
write of RESTORE.md or MANIFEST.json propagates, and the run fails, since a snapshot with no
manifest is not one prune could ever prove it owns.

Verification restores into a directory made fresh in the system temp dir for each run. Neither
making nor removing it fails the run: scratch that cannot be made leaves the snapshot
unverified, and scratch that cannot be removed is a leak beside a verified one. Each is a
failed item, so the record says what went wrong.

The records are RESTORE.md, then MANIFEST.json, written atomically. Every staged file is hashed
first, and one that cannot be -- or a document that refuses to render -- joins the failed
items before ``complete`` is decided: nothing is left out of the manifest unrecorded, and the
completeness RESTORE.md states is the manifest's. The document's entry and the manifest's
bytes come from what was written, which is also what the run's read-back and prune compare.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from backup_build import BuildResult
from backup_fs import atomic_write_bytes, sha256_bytes
from backup_manifest import MANIFEST_NAME, manifest_to_json
from backup_model import FailedItem, FileEntry, Manifest
from backup_proc import RunCmd
from backup_rearm import ReArm
from backup_restore_doc import RESTORE_DOC_NAME, RestoreDocError, render_restore_doc
from backup_verify import hash_snapshot_files, verify_snapshot

#: The prefix of the directory each run's verification restores into, in the system temp dir.
VERIFY_SCRATCH_PREFIX = "workspace-backup-verify-"
#: The failed item naming that directory, when it cannot be made or removed.
VERIFY_SCRATCH_ITEM = "verify-scratch"
_UTF8 = "utf-8"
_PATH_SEPARATOR = "/"


def verify_in_scratch(
    staging: Path, build: BuildResult, *, run: RunCmd
) -> tuple[FailedItem, ...]:
    """Verify *staging* by restoring into scratch of its own; every failure is an item."""
    try:
        scratch = Path(tempfile.mkdtemp(prefix=VERIFY_SCRATCH_PREFIX))
    except OSError as exc:
        reason = f"no scratch directory, so nothing was verified: {exc}"
        return (FailedItem(VERIFY_SCRATCH_ITEM, reason),)
    try:
        failures = verify_snapshot(
            staging, build.bundles, build.archive_members, scratch, run=run
        )
    except BaseException as exc:
        # A raise is a defect for backup_run to record, not a failed item. The scratch is
        # removed all the same, and a removal that fails travels with it as a note, so the
        # record names both rather than only the first.
        cleanup = _removal_problem(scratch)
        if cleanup is not None:
            exc.add_note(f"{VERIFY_SCRATCH_ITEM}: {cleanup}")
        raise
    cleanup = _removal_problem(scratch)
    if cleanup is None:
        return failures
    return (*failures, FailedItem(VERIFY_SCRATCH_ITEM, cleanup))


def write_snapshot_records(
    staging: Path,
    now: datetime,
    build: BuildResult,
    failed: Sequence[FailedItem],
    *,
    rearm: "ReArm | None" = None,
    machine: "str | None" = None,
) -> Manifest:
    """Write RESTORE.md, then MANIFEST.json, into *staging*; the manifest written.

    *rearm* is RESTORE.md's re-arm step; None renders the default for an unknown workspace.
    *machine* is the label of the machine taking it, which both records name.

    *staging* must hold only what the build wrote: a RESTORE.md already there would be
    listed twice.
    """
    hashed = hash_snapshot_files(staging, exclude=MANIFEST_NAME)
    files = list(hashed.files)
    failures = [*failed, *hashed.failed]
    draft = replace(_unlisted_manifest(now, build, failures), machine=machine)
    try:
        doc = render_restore_doc(draft, rearm=rearm).encode(_UTF8)
    except RestoreDocError as exc:
        failures.append(FailedItem(RESTORE_DOC_NAME, f"not written: {exc}"))
    else:
        atomic_write_bytes(staging / RESTORE_DOC_NAME, doc)
        files.append(FileEntry(RESTORE_DOC_NAME, sha256_bytes(doc), len(doc)))
    manifest = replace(
        draft,
        complete=not failures,
        files=tuple(sorted(files, key=lambda entry: entry.name.split(_PATH_SEPARATOR))),
        failed=tuple(failures),
    )
    atomic_write_bytes(staging / MANIFEST_NAME, manifest_bytes(manifest))
    return manifest


def manifest_bytes(manifest: Manifest) -> bytes:
    """The exact bytes MANIFEST.json holds for *manifest*."""
    return manifest_to_json(manifest).encode(_UTF8)


def _unlisted_manifest(
    now: datetime, build: BuildResult, failed: Sequence[FailedItem]
) -> Manifest:
    """The manifest for *build* with no files listed yet."""
    return Manifest(
        created=now.astimezone().isoformat(timespec="seconds"),
        complete=not failed,
        bundles=build.bundles,
        files=(),
        archive_members=build.archive_members,
        dirty_worktrees=build.dirty_worktrees,
        failed=tuple(failed),
        skipped_symlinks=build.skipped_symlinks,
        unclassified_ignored=build.unclassified_ignored,
    )


def _removal_problem(scratch: Path) -> "str | None":
    """Remove *scratch*; why that failed, or None."""
    try:
        shutil.rmtree(scratch)
    except OSError as exc:
        return f"{scratch} could not be removed: {exc}"
    return None
