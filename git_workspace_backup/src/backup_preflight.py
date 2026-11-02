"""The CLI's refusals about the tool's own directory, made before a mode does anything.

Two of them: a backup run holding the run lock, and a directory that is not this user's
alone: not a directory, not owned by the effective uid, or writable by anyone else. Run and
install then make it owner-only, as everything the tool creates beneath it already is.

Kept apart from the entry script, which only routes modes: each check here reads the state base
under the home directory, never the checkout or its configuration, so uninstall can share the
one it needs without reading either.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
from pathlib import Path
from typing import TextIO

from backup_checkout import Refused
from backup_fs import PRIVATE_DIR_MODE, lock_is_held
from backup_run import lock_path

#: What install and uninstall refuse with while a backup run holds the run lock.
RUN_IN_PROGRESS = "a backup run is in progress; retry when it finishes"
#: Write permission for the group and for everyone else.
_FOREIGN_WRITE_BITS = stat.S_IWGRP | stat.S_IWOTH
#: The uid a platform without one reports. Never used: every mode refuses that platform first.
_NO_UID = -1
#: What opening the base without following a symlink, as a directory, fails with when the base
#: is a symlink or is not a directory.
_NOT_A_DIRECTORY_ERRNOS = frozenset({errno.ELOOP, errno.ENOTDIR})


# os.geteuid, os.fchmod, O_DIRECTORY and O_NOFOLLOW are POSIX-only. The split is an ``if`` so
# a win32 type-check pass still sees the names; the win32 arm exists only for that pass.
if sys.platform == "win32":

    def effective_uid() -> int:  # pragma: only-on win32
        """No uid: Windows has none, and every mode refuses the platform before using one."""
        return _NO_UID

    _OPEN_BASE = os.O_RDONLY  # pragma: only-on win32

    def _make_private(descriptor: int) -> None:  # pragma: only-on win32
        """Refuse: Windows has no fchmod, and every mode refuses the platform first."""
        raise OSError(errno.ENOSYS, "making the directory private needs fchmod")

else:  # pragma: only-on posix

    def effective_uid() -> int:
        """This process's effective uid, which must own the tool's directory."""
        return os.geteuid()

    #: The one open the base is checked and changed through: never a symlink in its place.
    _OPEN_BASE = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def _make_private(descriptor: int) -> None:
        """Make the directory open on *descriptor* owner-only."""
        os.fchmod(descriptor, PRIVATE_DIR_MODE)


def refuse_unsafe_state_base(base: Path, euid: int) -> None:
    """Refuse a *base* that exists but is not a directory, not *euid*'s, or others can write.

    Read by ``lstat``, so a symlink in its place is not a directory. Absent is not refused:
    run and install create it, and status reads nothing there yet. Staging holds a build in
    progress, and a run trusts what it finds there, so a base another account can write to
    lets that account plant or read what a run writes.
    """
    try:
        info = base.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise Refused(f"the tool's directory {base} cannot be examined: {exc}") from exc
    _refuse_unsafe_info(base, info, euid)


def _refuse_unsafe_info(base: Path, info: os.stat_result, euid: int) -> None:
    """Refuse *base* when *info* is not a directory, not *euid*'s, or others can write it."""
    if not stat.S_ISDIR(info.st_mode):
        raise Refused(f"the tool's directory {base} is not a directory")
    if info.st_uid != euid:
        raise Refused(
            f"the tool's directory {base} is not owned by this user (owner uid "
            f"{info.st_uid}, this process {euid})"
        )
    mode = stat.S_IMODE(info.st_mode)
    if mode & _FOREIGN_WRITE_BITS:
        raise Refused(
            f"the tool's directory {base} is writable by other users (mode {oct(mode)}); "
            "remove their write access: chmod go-w"
        )


def secure_state_base(base: Path, euid: int) -> None:
    """Create *base* if absent, refuse it unsafe, and make it owner-only, all through one open.

    The checks and the mode change act on one descriptor, opened without following a symlink,
    so a swap after the check cannot redirect the change to another directory. ``mkdir`` never
    follows a symlink in the base's place either. The base's parent is the home directory,
    which exists; anything else making it, or failing to open or change it, is refused.
    """
    try:
        os.mkdir(base, PRIVATE_DIR_MODE)
    except FileExistsError:
        pass  # Already there: whatever it is, the open below examines it.
    except OSError as exc:
        raise Refused(
            f"the tool's directory {base} cannot be made private: {exc}"
        ) from exc
    descriptor = _open_state_base(base)
    try:
        _refuse_unsafe_info(base, os.fstat(descriptor), euid)
        _make_private(descriptor)
    except OSError as exc:
        raise Refused(
            f"the tool's directory {base} cannot be made private: {exc}"
        ) from exc
    finally:
        os.close(descriptor)


def _open_state_base(base: Path) -> int:
    """A read-only descriptor on the directory *base*, never through a symlink in its place."""
    try:
        return os.open(base, _OPEN_BASE)
    except OSError as exc:
        # A symlink in its place is ENOTDIR on macOS and ELOOP on Linux; anything else that
        # is not a directory is ENOTDIR on both.
        if exc.errno in _NOT_A_DIRECTORY_ERRNOS:
            raise Refused(f"the tool's directory {base} is not a directory") from exc
        raise Refused(
            f"the tool's directory {base} cannot be examined: {exc}; "
            "if you own it, make it readable and searchable by you (chmod u+rwx)"
        ) from exc


def refuse_during_a_run(base: Path, err: TextIO, *, uninstalling: bool = False) -> None:
    """Refuse while a backup run holds the run lock under *base*, which bootout would kill.

    Its git children run in sessions of their own, so they can outlive it and write into
    staging after the lock is freed. The lock is probed without waiting and nothing is created.
    A lock that cannot be opened is one no run holds, since a run opens it read-write.
    """
    path = lock_path(base)
    try:
        held = lock_is_held(path)
    except OSError as exc:
        # Deliberately asymmetric. Uninstall warns and goes ahead: refusing protects no run and
        # would leave the agent impossible to remove. Install refuses: every run of the agent
        # it would install fails at this lock, which is a problem to surface, not paper over.
        if not uninstalling:
            detail = f"could not check whether a backup run is in progress: {exc}"
            raise Refused(detail) from exc
        print(
            f"WARNING: cannot open the run lock {path}, so no run holds it: {exc}",
            file=err,
        )
        return
    if held:
        raise Refused(RUN_IN_PROGRESS)
