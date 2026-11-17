"""``--mode verify``: prove a promoted snapshot folder is what the run that made it recorded.

Two proofs, and exit 0 needs both. The manifest must hash to the sha256 the local ledger
recorded for the folder's name, and every file the manifest lists must hash to its entry, at
its size, with nothing unlisted beside them but ``.DS_Store`` (Finder writes one, so it is
noted, never failed). Unlisted means any file, symlink or other entry the manifest does not
name, and any folder that holds no listed file, reported once without being entered; the
``.<name>.icloud`` placeholder of a listed file is not unlisted only while that file is
absent, since only then is it what iCloud left in the file's place. RESTORE.md is one of the listed files, so a restore that starts here
reads a procedure this machine wrote.

A folder with no ledger entry (checked on a second machine, on a machine restored without its
state, or after its snapshot was pruned) can still be checked against its own manifest, but
nothing vouches for that manifest: whoever could rewrite a file could rewrite the manifest to
match. So that case never exits 0 and never prints "verified".

Every file is read beneath the destination as a regular file, never through a symlink, and
the manifest and every listed file are read on a watchdog: the destination is usually iCloud
Drive, where an offloaded file blocks on its first read. A dataless file (evicted by iCloud) is
reported offloaded without being read, and a file whose size differs from its entry is
reported changed without being read, so a file swapped for a far larger one is never hashed to
its end. The folder listing that finds unlisted entries is not
bounded: a directory listing does not fetch file data. The folder must be directly in the
destination and carry a snapshot name, so the state key the ledger is looked up under is the
destination's own, and only a promoted ledger entry vouches for a manifest.

Exit 1 is a definite answer: a mismatch, a missing or unlisted file, a manifest the ledger does
not match, or no ledger entry once every listed file was judged. Exit 2 means something could
not be judged: the folder or its records, or a listed file (no ledger entry included, since
an unjudged file leaves the answer open). A definite problem outranks a file that could not be
read: the snapshot is already known to be bad.

A verified snapshot whose manifest records it incomplete is still verified, since the folder
is exactly what its run recorded, with a note saying how many items that run failed.

Every line is printed with each character a terminal acts on spelled as an escape: a name in
the manifest or on disk, and an OSError's text, are not this tool's, and a newline or an
escape sequence in one could erase a failure line and print a forged ``verified:`` line.

Entry point assumptions: imported by the tool's CLI, whose bootstrap has put the shared
``stdio_utf8`` library on ``sys.path``, as it does for every private ``src/`` module.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TextIO

from backup_checkout import Refused
from backup_config import canonical_path, expand_home
import stdio_utf8
from backup_fs import (
    FileOffloaded,
    ReadStalled,
    SizeChanged,
    hash_regular_file_bounded,
    read_bytes_bounded,
    read_regular_file,
    sha256_bytes,
)
from backup_ledger import LedgerError, load_ledger, state_dir_for
from backup_manifest import MANIFEST_NAME, parse_manifest
from backup_model import STATE_PROMOTED, FileEntry, Manifest
from backup_promote import MANIFEST_READ_TIMEOUT_S, split_snapshot_name

EXIT_VERIFIED = 0
EXIT_NOT_VERIFIED = 1
EXIT_UNJUDGED = 2
#: Finder's per-folder view settings: written into any folder browsed, so never a tamper.
DS_STORE = ".DS_Store"
#: How long one listed file may go without yielding a chunk before it is abandoned.
STALL_TIMEOUT_S = 60.0
#: The most a manifest read back may hold: far beyond any archive listing this tool writes.
MANIFEST_READ_LIMIT = 1 << 26
#: What older iCloud Drive leaves in place of an offloaded file: ``.<name>.icloud`` beside it.
_PLACEHOLDER = ".{}.icloud"
#: What opening a symlink no-follow, or a special file as a regular one, raises.
_NOT_REGULAR_ERRNOS = frozenset({errno.ELOOP, errno.EINVAL, errno.ENOTDIR})
_NO_ENTRY_CAUSES = (
    "no ledger entry names it here: the check runs on a second machine, on a restored "
    "machine without this one's state, or after the snapshot was pruned"
)

#: Hashes the file *rel* beneath a root, expected to hold the given size; (sha256, size),
#: else OSError, ReadStalled, FileOffloaded or SizeChanged.
HashFileFn = Callable[[Path, str, int], "tuple[str, int]"]
#: The manifest bytes of snapshot *name* beneath a destination; None when unreadable in time.
ReadManifestFn = Callable[[Path, str], "bytes | None"]


class _Unjudged(Exception):
    """The folder cannot be judged at all: it is not a snapshot folder, or its records."""


@dataclass(frozen=True)
class VerifyRequest:
    """The snapshot folder as given, the resolved destination, and the tool's state base."""

    snapshot: Path
    destination: Path
    #: The tool's own directory; the destination's ledger is under its state key there.
    state_base: Path


def hash_listed_file(root: Path, rel: str, size: int) -> "tuple[str, int]":
    """The real :data:`HashFileFn`: no-follow, size first, chunked, abandoned when idle.

    A file whose descriptor does not report *size* raises SizeChanged before any read; one
    that yields no chunk within STALL_TIMEOUT_S raises ReadStalled.
    """
    return hash_regular_file_bounded(root, rel, STALL_TIMEOUT_S, expected_size=size)


def read_manifest_bounded(destination: Path, name: str) -> "bytes | None":
    """The real :data:`ReadManifestFn`: a regular file beneath *destination*, bounded."""
    rel = f"{name}/{MANIFEST_NAME}"

    def _read(_: Path) -> bytes:
        return read_regular_file(destination, rel, limit=MANIFEST_READ_LIMIT)[0]

    return read_bytes_bounded(destination / rel, MANIFEST_READ_TIMEOUT_S, reader=_read)


@dataclass(frozen=True)
class VerifySeams:
    """The two reads verify makes, replaceable by tests."""

    hash_file: HashFileFn = hash_listed_file
    read_manifest: ReadManifestFn = read_manifest_bounded


@dataclass
class _Findings:
    """Definite problems, files that could not be judged, and notes, each one line."""

    problems: list[str] = field(default_factory=list)
    unjudged: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: The ``.<name>.icloud`` placeholders that stand in for an absent listed file.
    placeholders: set[str] = field(default_factory=set)


def snapshot_path(raw: "str | None", home: Path) -> Path:
    """--snapshot, with a leading ``~/`` expanded against *home*; Refused unless absolute."""
    if raw is None:
        raise Refused("--mode verify needs --snapshot, the snapshot folder to check")
    path = expand_home(raw, home)
    if not path.is_absolute():
        raise Refused(f"--snapshot must be absolute or start with ~/, got {raw!r}")
    return path


def verify_snapshot_folder(
    request: VerifyRequest,
    *,
    out: TextIO,
    err: TextIO,
    seams: VerifySeams = VerifySeams(),
) -> int:
    """Judge *request*'s snapshot folder, print the verdict, and return its exit code."""
    try:
        name = _checked_folder_name(request.snapshot, request.destination)
        data, manifest = _read_manifest(request.destination, name, seams.read_manifest)
        state = state_dir_for(request.state_base, request.destination)
        recorded = _recorded_manifest_sha256(state, name)
    except _Unjudged as exc:
        _print_line(f"ERROR: {exc}", err)
        return EXIT_UNJUDGED
    findings = _Findings(notes=_incomplete_notes(manifest))
    if recorded is not None and recorded != sha256_bytes(data):
        findings.problems.append(
            f"changed: {MANIFEST_NAME} does not hash to what the ledger recorded for {name}"
        )
    _check_listed(request.destination, name, manifest.files, seams.hash_file, findings)
    _check_unlisted(request.destination / name, manifest.files, findings)
    return _report(name, len(manifest.files), recorded is not None, findings, out, err)


def _incomplete_notes(manifest: Manifest) -> "list[str]":
    """A note when the run recorded this snapshot incomplete; none for a complete one."""
    if manifest.complete:
        return []
    failed = len(manifest.failed)
    items = "item" if failed == 1 else "items"
    return [
        f"note: the run recorded this snapshot incomplete, with {failed} failed {items}; "
        "its RESTORE.md restores only what it holds"
    ]


def _checked_folder_name(snapshot: Path, destination: Path) -> str:
    """*snapshot*'s name, once it is a real snapshot folder directly in *destination*."""
    name = snapshot.name
    if split_snapshot_name(name) is None:
        raise _Unjudged(f"{snapshot} is not a snapshot folder name this tool writes")
    if canonical_path(snapshot.parent) != destination:
        raise _Unjudged(f"{snapshot} is not directly in the destination {destination}")
    try:
        mode = snapshot.lstat().st_mode
    except FileNotFoundError:
        raise _Unjudged(f"no snapshot folder at {snapshot}") from None
    except OSError as exc:
        raise _Unjudged(f"{snapshot} cannot be examined: {exc}") from exc
    if stat.S_ISLNK(mode):
        raise _Unjudged(f"{snapshot} is a symlink, which verify never follows")
    if not stat.S_ISDIR(mode):
        raise _Unjudged(f"{snapshot} is not a directory")
    return name


def _read_manifest(
    destination: Path, name: str, read_manifest: ReadManifestFn
) -> "tuple[bytes, Manifest]":
    """The manifest's bytes and parse; _Unjudged when unreadable in time or not ours."""
    data = read_manifest(destination, name)
    if data is None:
        raise _Unjudged(
            f"{name}/{MANIFEST_NAME} could not be read in time as a regular file, never "
            "through a symlink; if it is in iCloud only, download it and run verify again"
        )
    manifest = parse_manifest(data)
    if manifest is None:
        raise _Unjudged(f"{name}/{MANIFEST_NAME} is not a manifest this tool wrote")
    return data, manifest


def _recorded_manifest_sha256(state: Path, name: str) -> "str | None":
    """The manifest sha256 the ledger under *state* recorded for *name*; None for no entry."""
    try:
        ledger = load_ledger(state)
    except (LedgerError, OSError) as exc:
        raise _Unjudged(f"the ledger under {state} cannot be read: {exc}") from exc
    found = [
        entry
        for entry in ledger.entries
        if entry.name == name and entry.state == STATE_PROMOTED
    ]
    return found[0].manifest_sha256 if found else None


def _check_listed(
    destination: Path,
    name: str,
    files: "tuple[FileEntry, ...]",
    hash_file: HashFileFn,
    findings: _Findings,
) -> None:
    """Hash each listed file beneath *destination*, adding one line per file that fails."""
    for entry in files:
        try:
            digest, size = hash_file(destination, f"{name}/{entry.name}", entry.size)
        except SizeChanged as exc:
            findings.problems.append(_size_changed(entry, exc.size))
        except FileOffloaded:
            findings.unjudged.append(
                f"offloaded: {entry.name} is in iCloud only; download it and run verify again"
            )
        except ReadStalled:
            findings.unjudged.append(
                f"stalled: {entry.name} yielded no data within the bound; it may be in "
                "iCloud only: download it and run verify again"
            )
        except FileNotFoundError:
            _missing(destination / name, entry.name, findings)
        except OSError as exc:
            _unopenable(entry.name, exc, findings)
        else:
            _compare(entry, digest, size, findings)


def _missing(folder: Path, rel: str, findings: _Findings) -> None:
    """A listed file absent: offloaded when iCloud's placeholder is beside it, else missing."""
    placeholder = _placeholder(rel)
    if os.path.lexists(folder / placeholder):
        findings.placeholders.add(placeholder)
        findings.unjudged.append(
            f"offloaded: {rel} is in iCloud only; download it and run verify again"
        )
    else:
        findings.problems.append(f"missing: {rel}")


def _unopenable(rel: str, exc: OSError, findings: _Findings) -> None:
    """A listed file that could not be opened: not a regular file, or unreadable."""
    if exc.errno in _NOT_REGULAR_ERRNOS:
        findings.problems.append(
            f"not a regular file: {rel} (a symlink or special file is never followed)"
        )
    else:
        findings.unjudged.append(f"unreadable: {rel} could not be read: {exc}")


def _compare(entry: FileEntry, digest: str, size: int, findings: _Findings) -> None:
    """Add a line when a listed file's size or sha256 differs from its manifest entry."""
    if size != entry.size:
        findings.problems.append(_size_changed(entry, size))
    elif digest != entry.sha256:
        findings.problems.append(
            f"changed: {entry.name} does not hash to the manifest's sha256"
        )


def _size_changed(entry: FileEntry, size: int) -> str:
    """The line for a listed file holding *size* bytes where its entry lists another size."""
    return f"changed: {entry.name} holds {size} bytes, the manifest lists size {entry.size}"


def _check_unlisted(
    folder: Path, files: "tuple[FileEntry, ...]", findings: _Findings
) -> None:
    """Add a line for every entry under *folder* the manifest does not list.

    A placeholder counts as listed only when :func:`_missing` found it standing in for its
    absent file.
    """
    listed = {entry.name for entry in files} | {MANIFEST_NAME}
    try:
        for rel in _walk_entries(folder, _listed_folders(listed)):
            if rel in listed or rel in findings.placeholders:
                continue
            # A folder yields with a trailing slash, so one named .DS_Store is never Finder's.
            if not rel.endswith("/") and PurePosixPath(rel).name == DS_STORE:
                findings.notes.append(f"note: {rel} is Finder's, unlisted and ignored")
            else:
                findings.problems.append(f"unlisted: {rel}")
    except OSError as exc:
        findings.unjudged.append(f"unlistable: a folder in the snapshot: {exc}")


def _listed_folders(listed: "set[str]") -> "frozenset[str]":
    """Every folder a listed name sits in, by POSIX name: the folders a walk enters."""
    return frozenset(
        str(parent)
        for name in listed
        for parent in PurePosixPath(name).parents
        if parent != PurePosixPath(".")
    )


def _walk_entries(
    folder: Path, entered: "frozenset[str]", prefix: str = ""
) -> Iterator[str]:
    """Every entry beneath *folder* by POSIX name, a symlink never entered.

    A folder in *entered* is walked; any other folder is yielded once, as ``<name>/``, and not
    entered, since nothing in it is listed. A file or other entry is yielded by its name.
    """
    with os.scandir(folder) as entries:
        listing = sorted(entries, key=lambda entry: entry.name)
    for entry in listing:
        rel = f"{prefix}{entry.name}"
        if not entry.is_dir(follow_symlinks=False):
            yield rel
        elif rel in entered:
            yield from _walk_entries(Path(entry.path), entered, f"{rel}/")
        else:
            yield f"{rel}/"


def _placeholder(rel: str) -> str:
    """Where older iCloud Drive leaves ``.<name>.icloud`` for the offloaded file *rel*."""
    path = PurePosixPath(rel)
    return str(path.with_name(_PLACEHOLDER.format(path.name)))


def _report(
    name: str,
    count: int,
    authenticated: bool,
    findings: _Findings,
    out: TextIO,
    err: TextIO,
) -> int:
    """Print the verdict's lines and return its exit code, per the module docstring."""
    if authenticated and not findings.problems and not findings.unjudged:
        files = "file" if count == 1 else "files"
        for line in (f"verified: {name} ({count} {files})", *findings.notes):
            _print_line(line, out)
        return EXIT_VERIFIED
    lines = [*findings.problems, *findings.unjudged, *findings.notes]
    if not authenticated:
        lines.append(_not_authenticated(name, findings))
    for line in lines:
        _print_line(line, err)
    if findings.problems or not findings.unjudged:
        return EXIT_NOT_VERIFIED
    return EXIT_UNJUDGED


def _not_authenticated(name: str, findings: _Findings) -> str:
    """The no-entry line: what the files showed, and why nothing vouches for the manifest."""
    if findings.problems or findings.unjudged:
        return f"{name}: the manifest is NOT authenticated either: {_NO_ENTRY_CAUSES}"
    return (
        f"{name} is consistent with its own manifest, but the manifest is NOT "
        f"authenticated: {_NO_ENTRY_CAUSES}"
    )


def _print_line(line: str, stream: TextIO) -> None:
    """Print one verdict line with every character a terminal acts on spelled as an escape.

    A line quotes names and OSError text this tool did not write, so a newline in one would
    start a line of its own and an escape sequence could erase the line before it.
    """
    print(stdio_utf8.spell_unsafe(line), file=stream)
