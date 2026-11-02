"""Verify a staged snapshot by restoring it, never by re-reading what was written.

``git bundle verify`` is deliberately not used: it checks the header and prerequisites only,
and in testing it accepted a bundle whose pack data was corrupted where ``git clone``
refused it, and it will not run outside a repository at all. A mirror clone plus
``fsck --full`` exercises the path a real restore takes.

Each restore gets a directory of its own, made under the caller's scratch root with a unique
name and removed whatever the verdict. So a half-cloned mirror one failure leaves is never
what the next verification reads, and nothing else in the scratch root is touched. Every
command after the clone names the mirror with ``--git-dir`` and runs in that directory: a
mirror that is missing or is not a repository fails, rather than git discovering some
repository above it. Every command runs through the seam under ``GIT_TIMEOUT_S``, and a
timeout (124) or a git that could not start (127) fails like any other exit. So does a
restore that lists no refs at all: an empty listing proves nothing was restored.

What the entry recorded is proved, not assumed. The mirror's HEAD must resolve to the
recorded head, and each worktree head, once fetched, to its recorded sha: a restore anchors
an unreachable head by that sha, so a wrong one would be certified without ever being
checked. Each recorded stash entry must resolve to a commit the mirror holds, since a
restore stores it back by that sha and nothing in the bundle names it. A head's sha is only
compared, never handed to git; a stash entry's is, but only once it is a full object id.

Names are data. A bundle's name and a snapshot file's name must pass the manifest's
safe-name rule before either is joined onto the snapshot. A worktree head's name becomes
part of a refspec argument (never a path, never shell text), and only when git's own
rules for a ref component accept it: ``*`` would make the refspec a glob, which passes by
fetching whatever worktree heads the bundle happens to hold. Archive members are looked up
inside the zip, never on disk.

Archive and hash reads carry no timeout, so they are for a snapshot in local staging only:
a file that blocks on its first read, as an iCloud-offloaded one does, would hang the run.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import zipfile
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from operator import attrgetter
from pathlib import Path

from backup_build import EXTRAS_ZIP
from backup_bundle import WORKTREE_PREFIX, WORKTREE_SUFFIX, is_object_id, parse_refs
from backup_fs import sha256_bytes, sha256_file
from backup_json import is_safe_relative_name
from backup_model import BundleEntry, FailedItem, FileEntry, StashEntry, WorktreeHead
from backup_proc import GIT_TIMEOUT_S, RunCmd

VERIFY_LABEL = "verify:"
HASH_LABEL = "hash:"
#: Where a fetched worktree head is anchored in the mirror, as ``<prefix><name>/HEAD``.
RESTORED_WORKTREE_PREFIX = "refs/backup/worktrees/"
#: Each restore's own directory under the scratch root, and the mirror inside it.
RESTORE_DIR_PREFIX = "verify-"
MIRROR_NAME = "mirror.git"
_REFS_PREFIX = "refs/"
_HEAD = "HEAD"
#: Names the snapshot root itself in a hash failure.
_ROOT_NAME = "."
#: git refuses these in a ref component, beside every control character, space and DEL.
_REF_FORBIDDEN = frozenset("~^:?*[\\/")
_SPACE_CODEPOINT = 0x20
_DELETE_CODEPOINT = 0x7F
_LOCK_SUFFIX = ".lock"
#: What reading a damaged archive raises. RuntimeError covers NotImplementedError (a method
#: field flipped to one zipfile lacks) and an encryption flag demanding a password; EOFError
#: and zlib.error a member stream cut short or corrupted; KeyError a listed member that the
#: archive does not hold; ValueError a member name flagged UTF-8 that does not decode as it
#: (UnicodeDecodeError, raised while the archive is opened).
_ARCHIVE_ERRORS = (
    OSError,
    EOFError,
    RuntimeError,
    KeyError,
    ValueError,
    zipfile.BadZipFile,
    zlib.error,
)


@dataclass(frozen=True)
class HashedFiles:
    """Each file hashed, and each name that could not be, with why."""

    files: tuple[FileEntry, ...]
    failed: tuple[FailedItem, ...]


@dataclass(frozen=True)
class _Mirror:
    """A mirror clone of *bundle*, made in the restore directory *workdir*."""

    bundle: Path
    workdir: Path
    run: RunCmd

    def clone_failure(self) -> "str | None":
        """Clone the mirror; why that failed, or None."""
        # "--" ends the options, so neither path can ever be read as one.
        argv = ["git", "clone", "--quiet", "--mirror", "--", str(self.bundle)]
        argv.append(str(self.workdir / MIRROR_NAME))
        failure, _ = _git(self.run, argv, self.workdir, "mirror clone")
        return failure

    def git(self, args: Sequence[str], what: str) -> "tuple[str | None, str]":
        """Run git *args* against the mirror alone: why it failed (or None), and stdout."""
        argv = ["git", f"--git-dir={self.workdir / MIRROR_NAME}", *args]
        return _git(self.run, argv, self.workdir, what)

    def commit_failure(self, rev: str, recorded: str, what: str) -> "str | None":
        """Why *rev* in the mirror does not resolve to the commit *recorded*, or None."""
        resolve = ["rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}"]
        failure, out = self.git(resolve, f"rev-parse of {what}")
        if failure is not None:
            return failure
        restored = out.strip()
        if restored != recorded:
            return f"{what} restored as {restored}, recorded {recorded}"
        return None


def verify_bundle(
    snapshot: Path, entry: BundleEntry, scratch: Path, *, run: RunCmd
) -> "FailedItem | None":
    """Restore *entry* in a directory of its own under *scratch*; None only if every check holds.

    *scratch* must exist. The restore directory is removed whatever the verdict, and a
    removal that fails is reported beside it.
    """
    label = f"{VERIFY_LABEL}{entry.name}"
    if not is_safe_relative_name(entry.name):
        return FailedItem(label, f"unsafe bundle name: {entry.name!r}")
    try:
        workdir = Path(tempfile.mkdtemp(prefix=RESTORE_DIR_PREFIX, dir=scratch))
    except OSError as exc:
        return FailedItem(label, f"no scratch directory for the restore: {exc}")
    try:
        verdict = _restore_failure(_Mirror(snapshot / entry.name, workdir, run), entry)
    finally:
        cleanup = _removal_failure(workdir)
    reasons = [reason for reason in (verdict, cleanup) if reason is not None]
    return FailedItem(label, "; ".join(reasons)) if reasons else None


def verify_archive(zip_path: Path, members: Sequence[FileEntry]) -> "FailedItem | None":
    """CRC-check every member and compare each listed member with its recorded hash.

    An archive too damaged to read is a failed item, never an exception. *zip_path* must be
    in local staging: its reads carry no timeout.
    """
    label = f"{VERIFY_LABEL}{zip_path.name}"
    try:
        with zipfile.ZipFile(zip_path) as archive:
            bad = archive.testzip()
            if bad is not None:
                return FailedItem(label, f"corrupt member: {bad}")
            for member in members:
                if sha256_bytes(archive.read(member.name)) != member.sha256:
                    return FailedItem(
                        label, f"member differs from its source: {member.name}"
                    )
    except _ARCHIVE_ERRORS as exc:
        return FailedItem(label, f"unreadable archive: {exc}")
    return None


def verify_snapshot(
    snapshot: Path,
    bundles: Sequence[BundleEntry],
    archive_members: Sequence[FileEntry],
    scratch: Path,
    *,
    run: RunCmd,
) -> tuple[FailedItem, ...]:
    """Every bundle and the archive; one failed item per check that does not hold.

    *snapshot* must be local staging: the archive's reads carry no timeout.
    """
    failures: list[FailedItem] = []
    for entry in bundles:
        failure = verify_bundle(snapshot, entry, scratch, run=run)
        if failure is not None:
            failures.append(failure)
    archive_failure = verify_archive(snapshot / EXTRAS_ZIP, archive_members)
    if archive_failure is not None:
        failures.append(archive_failure)
    return tuple(failures)


def hash_snapshot_files(snapshot: Path, *, exclude: str) -> HashedFiles:
    """sha256 and size of every file under *snapshot* except *exclude*, by POSIX name.

    Nothing is followed and nothing is dropped unrecorded: a symlink or a special file is a
    failed item, and so is a directory that cannot be listed (``.`` names the root).
    *snapshot* must be local staging: its reads carry no timeout.
    """
    names, unlisted = _walk_names(snapshot)
    hashed = hash_named_files(snapshot, [name for name in names if name != exclude])
    return HashedFiles(hashed.files, unlisted + hashed.failed)


def hash_named_files(snapshot: Path, names: Sequence[str]) -> HashedFiles:
    """sha256 and size of each of *names* under *snapshot*, in the order given.

    A name failing the manifest's safe-name rule is a failed item and is never joined onto
    *snapshot*, let alone read. So is a name that is not a regular file there (its last
    component is examined without being followed), and one that cannot be read.
    *snapshot* must be local staging: its reads carry no timeout.
    """
    files: list[FileEntry] = []
    failed: list[FailedItem] = []
    for name in names:
        outcome = _hash_one(snapshot, name)
        if isinstance(outcome, FileEntry):
            files.append(outcome)
        else:
            failed.append(outcome)
    return HashedFiles(tuple(files), tuple(failed))


# fuzz-target
def worktree_refspec(name: str) -> "str | None":
    """The refspec fetching worktree head *name* into the mirror; None for a name git refuses.

    git's rules for one ref component: non-empty; no leading dot, ``..``, ``@{`` or
    ``.lock`` suffix; no control character, space, DEL, slash, backslash or any of
    ``~ ^ : ? * [``. Never raises.
    """
    if not _is_ref_component(name):
        return None
    return f"{WORKTREE_PREFIX}{name}{WORKTREE_SUFFIX}:{_restored_worktree_ref(name)}"


def _restored_worktree_ref(name: str) -> str:
    """Where worktree head *name* is anchored in the mirror.

    The name sits mid-refname, never last: a ref may not end in a dot, and ``x.`` is a
    worktree name git writes.
    """
    return f"{RESTORED_WORKTREE_PREFIX}{name}{WORKTREE_SUFFIX}"


def _is_ref_component(name: str) -> bool:
    """Whether git would accept *name* as one component of a ref name."""
    return (
        bool(name)
        and not name.startswith(".")
        and ".." not in name
        and "@{" not in name
        and not name.endswith(_LOCK_SUFFIX)
        and all(_is_ref_character(char) for char in name)
    )


def _is_ref_character(char: str) -> bool:
    """Whether git allows *char* in a ref name: above space, not DEL, not forbidden."""
    return (
        ord(char) > _SPACE_CODEPOINT
        and ord(char) != _DELETE_CODEPOINT
        and char not in _REF_FORBIDDEN
    )


def _restore_failure(mirror: _Mirror, entry: BundleEntry) -> "str | None":
    """Why a mirror of the bundle does not reproduce *entry*, or None."""
    failure = mirror.clone_failure()
    if failure is not None:
        return failure
    failure, _ = mirror.git(["fsck", "--full"], "fsck")
    if failure is not None:
        return failure
    failure = _refs_failure(mirror, entry)
    if failure is not None:
        return failure
    # A bundle whose source HEAD was unborn records no head, and so claims none to prove.
    if entry.head:
        failure = mirror.commit_failure(_HEAD, entry.head, _HEAD)
        if failure is not None:
            return failure
    for head in entry.worktree_heads:
        failure = _worktree_head_failure(mirror, head)
        if failure is not None:
            return failure
    for stash in entry.stashes:
        failure = _stash_failure(mirror, stash)
        if failure is not None:
            return failure
    return None


def _refs_failure(mirror: _Mirror, entry: BundleEntry) -> "str | None":
    """Why the mirror's refs fall short of *entry*'s, or None."""
    listing = ["for-each-ref", "--format=%(refname)", _REFS_PREFIX]
    failure, out = mirror.git(listing, "for-each-ref")
    if failure is not None:
        return failure
    restored = parse_refs(out)
    if not restored:
        return "restored mirror lists no refs"
    missing = sorted(set(entry.refs) - set(restored))
    if missing:
        return "restore lacks refs: " + ", ".join(missing)
    return None


def _worktree_head_failure(mirror: _Mirror, head: WorktreeHead) -> "str | None":
    """Why worktree *head* cannot be fetched from the bundle as the commit recorded, or None."""
    spec = worktree_refspec(head.name)
    if spec is None:
        return f"worktree head {head.name!r} is not a single ref component"
    fetch = ["fetch", "--quiet", "--", str(mirror.bundle), spec]
    failure, _ = mirror.git(fetch, f"fetch of worktree head {head.name}")
    if failure is not None:
        return failure
    restored = _restored_worktree_ref(head.name)
    return mirror.commit_failure(restored, head.sha, f"worktree head {head.name}")


def _stash_failure(mirror: _Mirror, stash: StashEntry) -> "str | None":
    """Why stash entry *stash* is not a commit the mirror holds, or None."""
    if not is_object_id(stash.sha):
        return f"stash entry {stash.sha!r} is not a full object id"
    return mirror.commit_failure(stash.sha, stash.sha, f"stash entry {stash.sha}")


def _git(
    run: RunCmd, argv: Sequence[str], cwd: Path, what: str
) -> "tuple[str | None, str]":
    """Why *argv* failed (None when it exited 0), and its stdout."""
    code, out, err = run(argv, cwd, GIT_TIMEOUT_S)
    if code != 0:
        return f"{what} failed (exit {code}): {err.strip()}", out
    return None, out


def _removal_failure(workdir: Path) -> "str | None":
    """Remove the restore directory *workdir*; why that failed, or None."""
    try:
        shutil.rmtree(workdir)
    except OSError as exc:
        return f"scratch cleanup failed: {exc}"
    return None


def _hash_one(snapshot: Path, name: str) -> "FileEntry | FailedItem":
    """*name*'s entry under *snapshot*, or the failed item saying why it has none."""
    item = f"{HASH_LABEL}{name}"
    if not is_safe_relative_name(name):
        return FailedItem(item, f"unsafe file name: {name!r}")
    path = snapshot / name
    try:
        status = path.lstat()
        if not stat.S_ISREG(status.st_mode):
            return FailedItem(item, "not a regular file")
        return FileEntry(name, sha256_file(path), status.st_size)
    except OSError as exc:
        return FailedItem(item, f"unreadable: {exc.strerror or exc}")


def _walk_names(snapshot: Path) -> tuple[list[str], tuple[FailedItem, ...]]:
    """Every non-directory under *snapshot*, sorted by component, and each unlistable directory.

    Directories are descended only when they are not symlinks; a symlink of any kind is
    listed as a name, for :func:`_hash_one` to refuse.
    """
    names: list[str] = []
    unlisted: list[FailedItem] = []
    pending = [""]
    while pending:
        rel_dir = pending.pop()
        subdirs: list[str] = []
        try:
            with os.scandir(snapshot / rel_dir) as listing:
                entries = sorted(listing, key=attrgetter("name"))
            for entry in entries:
                rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
                (subdirs if entry.is_dir(follow_symlinks=False) else names).append(rel)
        except OSError as exc:
            reason = f"unlistable directory: {exc.strerror or exc}"
            unlisted.append(FailedItem(f"{HASH_LABEL}{rel_dir or _ROOT_NAME}", reason))
        # A stack fed in reverse visits the subdirectories in sorted order.
        pending.extend(reversed(subdirs))
    return sorted(names, key=lambda name: name.split("/")), tuple(unlisted)
