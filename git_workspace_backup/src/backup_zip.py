"""Write the whitelisted files into extras.zip under workspace/, claude/ and home/ prefixes.

Include-only: nothing enters the archive unless a
configured entry names it. Every entry must match at least one regular file -- an entry
that silently matches nothing is how a backup quietly stops covering something.

An entry means what it means to drift detection. Its root is walked, and a regular file is
covered when ``backup_drift.pattern_matches`` accepts its path, so a matched directory brings
everything beneath it. The walk never descends where ``pattern_reaches_beneath`` says no
match can lie, so an entry naming one dotfile in a home root lists that root and nothing
else. Drift judges "backed up" by the same rules, so the archive holds exactly the files
drift calls covered, less those recorded as skipped or failed. A directory the walk cannot
list is a failed item and the walk goes on: nothing beneath it vanishes unrecorded.

Nothing beneath a root is followed; the root itself is configuration, and may be a link. A
symlink the pattern reaches -- one it matches, or a directory link on the way to a match --
is recorded and skipped, and a special file (a FIFO, a socket) is skipped. A member is read
through ``backup_fs.read_regular_file``: relative to a descriptor on its root, opened with
``O_NOFOLLOW_ANY`` so that no component beneath the root may be a symlink, opened without
waiting on a FIFO, and refused unless its fstat says regular file -- and bounded here by a
watchdog. So a path swapped after the walk -- its last component or any directory above it,
up to the root -- can neither leave the root nor hang the run. The member's time and mode
come from that same fstat.

The gap that remains: ``O_NOFOLLOW_ANY`` is macOS's, and an interpreter that does not
expose it falls back to ``O_NOFOLLOW``, which guards only the last component. There, a
directory swapped for a symlink between the walk and the read is followed.

Every member name is checked with the manifest's own safe-name rule before anything is
written, because ``ZipInfo`` silently rewrites a name such as ``home/.`` to ``home`` and
``/x`` to ``x``: the archive would then hold a name the manifest does not, and the manifest
would record one its own parser refuses, so the snapshot would read as not this tool's. So a
prefix must be one safe component, and a pattern that could name a path outside its root is
a failed item.
"""

from __future__ import annotations

import os
import time
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from operator import attrgetter
from pathlib import Path

from backup_drift import pattern_matches, pattern_reaches_beneath
from backup_fs import read_bytes_bounded, read_regular_file, sha256_bytes
from backup_json import is_safe_relative_name
from backup_model import FailedItem, FileEntry

#: Bound on reading one member. Whitelisted files are small config that reads in
#: milliseconds; the bound is for the one that is not local -- an iCloud-offloaded file
#: downloads on first read -- so a stuck read costs one failed item instead of holding the
#: run lock into launchd's next firing.
MEMBER_READ_TIMEOUT_S = 60.0
#: The failure reason for a member read that failed or outlived its bound: the bounded
#: reader reports both as the same None.
UNREADABLE_MEMBER = "unreadable or timed out"
_UNREADABLE_DIRECTORY = "unreadable directory: "
#: The failure reason for an entry whose only matches are excluded. The one file a run
#: excludes is the machine identity file (backup_build), so the reason names it.
ONLY_EXCLUDED = "matched only the machine identity file, which never travels"
#: The zip format's first and last representable member times.
_ZIP_FIRST_TIME = (1980, 1, 1, 0, 0, 0)
_ZIP_LAST_TIME = (2107, 12, 31, 23, 59, 59)
#: A zip member keeps the Unix mode in the high 16 bits of its external attributes.
_MODE_MASK = 0xFFFF
_EXTERNAL_ATTR_MODE_SHIFT = 16


class PatternError(ValueError):
    """A whitelist pattern that could name a path outside its root."""


@dataclass(frozen=True)
class ZipSource:
    """One root and the whitelist entries matched under it; *prefix* is one component.

    *excluded* names root-relative files never archived, whatever entry matches them; each
    is compared ignoring case, as APFS compares names. A run excludes only the machine
    identity file, which is what an entry matching nothing else is told it matched.
    """

    prefix: str
    root: Path
    patterns: tuple[str, ...]
    excluded: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ZipResult:
    """What entered the archive, what failed, and which symlinks were skipped."""

    members: tuple[FileEntry, ...]
    failed: tuple[FailedItem, ...]
    skipped_symlinks: tuple[str, ...]


@dataclass(frozen=True)
class Expansion:
    """What walking a root for one pattern found, as root-relative POSIX names.

    *files* are the regular files the pattern covers and *symlinks* the links it reaches,
    both sorted component by component. *unreadable_dirs* pairs each directory the walk
    could not list with why; ``""`` names the root itself.
    """

    files: tuple[str, ...]
    symlinks: tuple[str, ...]
    unreadable_dirs: tuple[tuple[str, str], ...]


def expand(root: Path, pattern: str) -> Expansion:
    """Walk *root* for *pattern* by drift's matching rules, following nothing.

    PatternError for a pattern that could name a path outside *root*. The tree itself never
    raises: a directory that cannot be listed is reported in ``unreadable_dirs``.
    """
    if not is_safe_relative_name(pattern):
        raise PatternError(
            f"unsafe pattern, it could name a path outside its root: {pattern!r}"
        )
    walk = _PatternWalk(root, pattern)
    pending = [""]
    while pending:
        # A stack fed in reverse visits the subdirectories in sorted order.
        pending.extend(reversed(walk.visit(pending.pop())))
    return walk.expansion()


def write_zip(out: Path, sources: Sequence[ZipSource]) -> ZipResult:
    """Archive every whitelisted file of *sources* into *out*.

    ValueError, before anything is created, when a source's prefix is not one safe path
    component.
    """
    for source in sources:
        _require_single_component(source.prefix)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        writer = _ArchiveWriter(archive)
        for source in sources:
            for pattern in source.patterns:
                writer.add_pattern(source, pattern)
    return ZipResult(
        tuple(writer.members), tuple(writer.failed), tuple(sorted(writer.skipped))
    )


def read_member(
    root: Path, rel: str, timeout: float
) -> "tuple[bytes, os.stat_result] | None":
    """:func:`read_regular_file` on a watchdog; None when it fails or outlives *timeout*."""
    statuses: list[os.stat_result] = []

    def read_recording_status(_: Path) -> bytes:
        data, status = read_regular_file(root, rel)
        statuses.append(status)
        return data

    data = read_bytes_bounded(root / rel, timeout, reader=read_recording_status)
    return None if data is None else (data, statuses[0])


class _PatternWalk:
    """One root walked for one pattern, collecting what :func:`expand` reports."""

    def __init__(self, root: Path, pattern: str) -> None:
        self._root = root
        self._pattern = pattern
        self._files: list[str] = []
        self._symlinks: list[str] = []
        self._unreadable: list[tuple[str, str]] = []

    def visit(self, rel_dir: str) -> list[str]:
        """Classify one directory's entries; return its subdirectories worth descending."""
        subdirs: list[str] = []
        try:
            with os.scandir(self._root / rel_dir) as listing:
                entries = sorted(listing, key=attrgetter("name"))
            for entry in entries:
                rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
                self._classify(entry, rel, subdirs)
        except OSError as exc:
            self._unreadable.append((rel_dir, exc.strerror or str(exc)))
        return subdirs

    def expansion(self) -> Expansion:
        """What the walk found."""
        return Expansion(
            _by_component(self._files),
            _by_component(self._symlinks),
            tuple(self._unreadable),
        )

    def _classify(self, entry: os.DirEntry[str], rel: str, subdirs: list[str]) -> None:
        """File one entry as a covered file, a reached symlink, or a directory to descend."""
        if entry.is_symlink():
            if self._link_may_hide_a_match(entry, rel):
                self._symlinks.append(rel)
        elif entry.is_dir(follow_symlinks=False):
            if pattern_reaches_beneath(self._pattern, rel):
                subdirs.append(rel)
        elif entry.is_file(follow_symlinks=False) and pattern_matches(
            self._pattern, rel
        ):
            self._files.append(rel)

    def _link_may_hide_a_match(self, entry: os.DirEntry[str], rel: str) -> bool:
        """Whether the link matches, or links to a directory a match could lie in."""
        if pattern_matches(self._pattern, rel):
            return True
        if not pattern_reaches_beneath(self._pattern, rel):
            return False
        # Only a directory can hold a path beneath the link. is_dir() stats the target to
        # learn its type; nothing behind the link is listed or read.
        try:
            return entry.is_dir()
        except OSError:
            return True  # its type cannot be read, so it may hide a match


class _ArchiveWriter:
    """Collects one archive's members, failures and skipped symlinks as it is written."""

    def __init__(self, archive: zipfile.ZipFile) -> None:
        self._archive = archive
        self._written: set[str] = set()
        self._failed_directories: set[str] = set()
        self.members: list[FileEntry] = []
        self.failed: list[FailedItem] = []
        self.skipped: set[str] = set()

    def add_pattern(self, source: ZipSource, pattern: str) -> None:
        """Archive what *pattern* covers under *source*, recording each failure."""
        item = f"{source.prefix}:{pattern}"
        try:
            found = expand(source.root, pattern)
        except PatternError as exc:
            self.failed.append(FailedItem(item, str(exc)))
            return
        for rel_dir, reason in found.unreadable_dirs:
            self._fail_directory(source.prefix, rel_dir, reason)
        self.skipped.update(
            name for _, name in self._safely_named(source, found.symlinks)
        )
        excluded = {name.casefold() for name in source.excluded}
        files = [rel for rel in found.files if rel.casefold() not in excluded]
        if not files:
            reason = ONLY_EXCLUDED if found.files else "matched no files"
            self.failed.append(FailedItem(item, reason))
            return
        for rel, name in self._safely_named(source, files):
            self._add_file(source.root, rel, name)

    def _fail_directory(self, prefix: str, rel_dir: str, reason: str) -> None:
        """Record an unlistable directory once, however many patterns reached it."""
        name = f"{prefix}/{rel_dir}" if rel_dir else prefix
        if name not in self._failed_directories:
            self._failed_directories.add(name)
            self.failed.append(FailedItem(name, f"{_UNREADABLE_DIRECTORY}{reason}"))

    def _safely_named(
        self, source: ZipSource, rels: Sequence[str]
    ) -> "list[tuple[str, str]]":
        """Each relative name with its member name; an unsafe one is a failure, left out."""
        named: list[tuple[str, str]] = []
        for rel in rels:
            name = f"{source.prefix}/{rel}"
            if is_safe_relative_name(name):
                named.append((rel, name))
            else:
                self.failed.append(FailedItem(name, "unsafe archive member name"))
        return named

    def _add_file(self, root: Path, rel: str, name: str) -> None:
        """Write *rel* under *root* as member *name*, once, recording it with its hash."""
        if name in self._written:
            return  # two entries named the same file
        read = read_member(root, rel, MEMBER_READ_TIMEOUT_S)
        if read is None:
            self.failed.append(FailedItem(name, UNREADABLE_MEMBER))
            return
        data, status = read
        self._archive.writestr(_member_info(name, status), data)
        self._written.add(name)
        self.members.append(FileEntry(name, sha256_bytes(data), len(data)))


def _member_info(name: str, status: os.stat_result) -> zipfile.ZipInfo:
    """A deflated member named *name*, its time and mode taken from *status*."""
    local = time.localtime(status.st_mtime)
    stamp = (
        local.tm_year,
        local.tm_mon,
        local.tm_mday,
        local.tm_hour,
        local.tm_min,
        local.tm_sec,
    )
    info = zipfile.ZipInfo(name, min(max(stamp, _ZIP_FIRST_TIME), _ZIP_LAST_TIME))
    info.external_attr = (status.st_mode & _MODE_MASK) << _EXTERNAL_ATTR_MODE_SHIFT
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def _by_component(names: list[str]) -> tuple[str, ...]:
    """*names* sorted component by component, so a directory's contents stay together."""
    return tuple(sorted(names, key=lambda name: name.split("/")))


def _require_single_component(prefix: str) -> None:
    """ValueError unless *prefix* is one safe path component."""
    if "/" in prefix or not is_safe_relative_name(prefix):
        raise ValueError(
            f"a zip prefix must be one safe path component, not {prefix!r}"
        )
