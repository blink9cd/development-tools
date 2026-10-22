"""This machine's identity: the label every snapshot name and manifest it writes carries.

Two machines can back up into one synced destination, and a name made of local time alone
lets them write the same folder in the same second. So each snapshot's name ends in this
machine's label, and its manifest records it. Rejected: a per-machine subfolder, which would
orphan the ledger keyed on the destination and leave the snapshots already there unpruned; a
label derived on every run, which a host-name change would turn into another machine's; and a
random id, which status and RESTORE.md could show only as an unreadable string.

The label is derived once and then kept. The first run derives it from the host name -- the
short form before the first dot, lowercased, every run of characters outside ``[a-z0-9]``
made one hyphen, the ends trimmed, capped at :data:`MAX_LABEL_LENGTH` -- and keeps it, one
line, in :data:`IDENTITY_NAME` under the tool's state base. Every later run reads that file,
so a host-name change (macOS renames a machine on a network name clash) does not change the
identity. A host name that leaves nothing gives :data:`FALLBACK_LABEL`.

The file may be edited by hand, and an edited label is honored when it is one derivation would
leave as it is. Anything else is :class:`MachineError`, never a silent rewrite: the file is
its user's statement of which machine this is. It is read as a regular file only, so a
symlink, a directory or a FIFO in its place is refused rather than followed or waited on.

The file never travels in a snapshot (the build skips it; backup_build), so a machine set up
from another's snapshot derives an identity of its own.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from backup_fs import read_regular_file, write_private_text
from backup_model import STATE_BASE_NAME

#: The identity file's name under the tool's state base.
IDENTITY_NAME = "machine"
#: Where the identity file is, as a person types it: a home token, never this machine's path.
IDENTITY_SHOWN = f"~/{STATE_BASE_NAME}/{IDENTITY_NAME}"
#: The label a host name that leaves nothing path-safe gives.
FALLBACK_LABEL = "machine"
#: The longest label: it ends every snapshot name, which should stay readable in a listing.
MAX_LABEL_LENGTH = 32
#: More than any label needs, and a bound on what a hand-edited file can make a run decode.
IDENTITY_READ_LIMIT = 4096
#: Runs of ASCII lowercase letters and digits joined by single hyphens.
_LABEL = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
#: A run of anything a label may not hold, hyphens included, so runs collapse to one.
_UNSAFE_RUN = re.compile(r"[^a-z0-9]+")
_HYPHEN = "-"
_UTF8 = "utf-8"

HostnameFn = Callable[[], str]
WriteTextFn = Callable[[Path, str], None]


class MachineError(Exception):
    """The identity file exists but holds no usable label, or cannot be read or kept.

    Recorded state the tool cannot use, so a run and status refuse it as they refuse a ledger
    they did not write. Not a LedgerError: the manifest parser reads labels from here, and the
    ledger module reads the manifest's, so this module cannot import that one.
    """


def identity_path(base: Path) -> Path:
    """The identity file under the tool's state *base*."""
    return base / IDENTITY_NAME


# fuzz-target
def derive_label(hostname: str) -> str:
    """The path-safe label *hostname* gives; :data:`FALLBACK_LABEL` when nothing is left."""
    short = hostname.split(".", 1)[0].lower()
    label = _UNSAFE_RUN.sub(_HYPHEN, short).strip(_HYPHEN)
    # Capping can leave a hyphen at the cut, which a label never ends in.
    label = label[:MAX_LABEL_LENGTH].rstrip(_HYPHEN)
    return label or FALLBACK_LABEL


# fuzz-target
def is_valid_label(label: str) -> bool:
    """Whether *label* is one :func:`derive_label` would leave as it is.

    So it is one plain path component -- never empty, ``.`` or ``..``, and holding no ``/``
    or NUL -- that sorts after a snapshot's timestamp without changing its order.
    """
    return len(label) <= MAX_LABEL_LENGTH and _LABEL.fullmatch(label) is not None


def read_identity(base: Path, hostname: HostnameFn) -> str:
    """The kept label, or the one a first run would keep when there is none; writes nothing.

    MachineError when the identity file exists but holds no valid label or cannot be read.
    """
    kept = _kept_label(identity_path(base))
    return derive_label(hostname()) if kept is None else kept


def ensure_identity(
    base: Path, hostname: HostnameFn, *, write_text: WriteTextFn = write_private_text
) -> str:
    """The kept label; on the first run, derived from *hostname*, kept, and read back.

    MachineError as :func:`read_identity` raises it, and when the label cannot be kept or
    does not read back as written. The caller holds the run lock, so no other run of this
    tool writes the file between the check and the write.
    """
    path = identity_path(base)
    kept = _kept_label(path)
    if kept is not None:
        return kept
    label = derive_label(hostname())
    try:
        write_text(path, f"{label}\n")
    except OSError as exc:
        raise MachineError(
            f"could not keep this machine's identity in {path}: {exc}"
        ) from exc
    if _kept_label(path) != label:
        raise MachineError(
            f"this machine's identity {path} does not read back as written"
        )
    return label


def _kept_label(path: Path) -> "str | None":
    """The label the identity file at *path* holds; None when there is no file."""
    try:
        data, _ = read_regular_file(path.parent, path.name)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise MachineError(
            f"this machine's identity file {path} cannot be read: {exc}"
        ) from exc
    label = _decoded(data)
    if label is None or not is_valid_label(label):
        raise MachineError(
            f"this machine's identity file {path} does not hold a valid label: write one "
            f"line of lowercase letters and digits, joined by single hyphens, at most "
            f"{MAX_LABEL_LENGTH} long"
        )
    return label


def _decoded(data: bytes) -> "str | None":
    """*data* as text with surrounding whitespace dropped; None when it is too long or not UTF-8."""
    if len(data) > IDENTITY_READ_LIMIT:
        return None
    try:
        return data.decode(_UTF8).strip()
    except UnicodeDecodeError:
        return None
