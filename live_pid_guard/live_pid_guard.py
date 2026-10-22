"""Record, probe and clean up the processes a live grandchild test leaves running.

The shared fixture ``bounded_run/samples/gc_child.py`` starts a grandchild, and neither process
exits for an hour. A live test proving that a bound holds therefore owns a cleanup its
assertions cannot see: were the tree kill to regress, an elapsed-time assertion would still pass
-- the bounded drain returns regardless -- while both processes ran on for that hour. This module is
that cleanup, in the one shape that is safe against pid reuse:

1. **Record.** :func:`record_fixture_pids` names two pid files in the ENVIRONMENT, which the
   fixture reads. Not argv: a tool under test may launch the fixture as its own subprocess and
   append flags of its own, which a positional hook would take for its pid files.
2. **Probe early.** :meth:`FixturePids.probe_exits` polls each recorded pid right after the call
   under test, before any assertion can raise and skip it.
3. **Clean up only what was not seen gone.** Leaving the block kills every recorded pid not seen
   gone. A pid seen gone is never signaled: by then it may belong to an unrelated process, and a
   probe made at cleanup time cannot tell the difference.

The probe itself never signals. ``os.kill(pid, 0)`` is no probe on Windows -- every signal but
the two console events becomes ``TerminateProcess`` -- so there a handle opened only to wait on
is polled instead. Pure library: no CLI, and nothing runs at import.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

#: The variables the fixture reads its pid-file paths from. Restated in the fixture rather than
#: imported, because it runs as a bare script under whatever interpreter a tool hands it; a drift
#: between the two fails every guarded test at its probe, as a pid that was never recorded.
CHILD_PID_ENV = "GC_CHILD_PID_FILE"
GRANDCHILD_PID_ENV = "GC_GRANDCHILD_PID_FILE"

#: A killed process is re-parented and reaped asynchronously, so its exit is polled for. A
#: ceiling, not an estimate: the poll returns as soon as the exit is seen, so only a process
#: that really survived spends it, and a loaded host gets ample time before a false survivor.
REAP_DEADLINE_SECONDS = 60.0
_REAP_POLL_SECONDS = 0.05

_CHILD_PID_FILE_NAME = "child.pid"
_GRANDCHILD_PID_FILE_NAME = "grandchild.pid"

#: What cleanup tolerates: no record or a partial one (the call failed before the child ran),
#: and a process already gone -- ESRCH on POSIX, ERROR_INVALID_PARAMETER on Windows.
_NOTHING_TO_KILL = (ValueError, OSError)

#: The lowest pid a fixture can record. ``os.kill`` reads 0 as the caller's own process group
#: and a negative pid as a whole group, and 1 is init/launchd, so a record below this names no
#: process the suite started: ``int(" 0\n")`` parses, and would have signaled the runner itself.
_MIN_RECORDED_PID = 2


def is_running(pid: int) -> bool:
    """Whether process *pid* is still running, probed without ever signaling it."""
    if sys.platform == "win32":
        return _is_running_on_windows(pid)  # pragma: only-on win32
    return _is_running_on_posix(pid)  # pragma: only-on posix


# Defined under the platform check rather than narrowed inside the function: mypy's
# other-platform passes skip this whole block, and the win32-only _winapi names with it. An
# `assert sys.platform == "win32"` inside the function body does not narrow those passes.
if sys.platform == "win32":
    import _winapi  # pragma: only-on win32

    def _is_running_on_windows(pid: int) -> bool:  # pragma: only-on win32
        """A ``SYNCHRONIZE`` handle polled with no wait: the probe that cannot terminate *pid*.

        An ``OpenProcess`` that fails means the pid is gone.
        """
        try:
            handle = _winapi.OpenProcess(_winapi.SYNCHRONIZE, False, pid)
        except OSError:
            return False
        try:
            return _winapi.WaitForSingleObject(handle, 0) != _winapi.WAIT_OBJECT_0
        finally:
            _winapi.CloseHandle(handle)


def _exited_awaiting_reap(pid: int) -> bool:  # pragma: only-on posix
    """Whether *pid* is a child of this process that has exited and is not yet reaped.

    Such a zombie holds its pid until its parent reaps it, and ``kill(pid, 0)`` succeeds on
    it, so a killed child of the probing process would read as running until that same
    process waited on it. ``WNOWAIT`` sees the exit without reaping, which leaves the real
    status for the child's owner (a ``Popen``, say). A pid that is not this process's child
    raises ``ChildProcessError``: its own parent, or init once it is re-parented, does the
    reaping, so ``kill(pid, 0)`` remains the answer for it.
    """
    # Both checks are spelled out inline so mypy narrows on them: os.waitid does not exist
    # on Windows, nor on macOS before Python 3.13, where a zombie still reads as running.
    if sys.platform == "win32":
        return False  # pragma: only-on win32
    if sys.platform == "darwin" and sys.version_info < (3, 13):
        return False  # pragma: no cover - macOS under Python 3.11-3.12 only
    try:
        exited = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return False
    return exited is not None


def _is_running_on_posix(pid: int) -> bool:  # pragma: only-on posix
    """``kill(pid, 0)``: delivers nothing, and fails once no such process exists.

    A child of this process that has exited but awaits its reap counts as gone, though the
    kernel still holds its pid (:func:`_exited_awaiting_reap`).
    """
    if _exited_awaiting_reap(pid):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def gone_within(pid: int, deadline_seconds: float) -> bool:
    """Whether process *pid* stops running at some point before the deadline."""
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if not is_running(pid):
            return True
        time.sleep(_REAP_POLL_SECONDS)
    return False


def _read_recorded_pid(pid_file: Path) -> int:
    """The pid in *pid_file*; ValueError when it does not parse or is below 2."""
    pid = int(pid_file.read_text(encoding="utf-8"))
    if pid < _MIN_RECORDED_PID:
        raise ValueError(f"{pid} is not a pid a fixture records")
    return pid


def require_recorded_pid(pid_file: Path) -> int:
    """The pid a live child recorded in *pid_file*, required as an assertion, not a bare read.

    The file is missing, or only partly written, when the bound expired before the child got as
    far as starting its grandchild: a machine too loaded for the test's timing. That is reported
    as the finding it is, rather than as a FileNotFoundError traceback. A record below 2 is
    refused the same way, as one no fixture writes, so the caller never probes or signals it.
    """
    try:
        pid = int(pid_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        raise AssertionError(
            "child did not start the grandchild before the bound "
            f"(no pid recorded in {pid_file.name})"
        ) from None
    if pid < _MIN_RECORDED_PID:
        raise AssertionError(
            f"{pid} is not a pid a fixture records (read from {pid_file.name})"
        )
    return pid


def seen_gone(pid_file: Path) -> bool:
    """Whether the process recorded in *pid_file* is seen to exit within the reap deadline.

    The pid is required either way.
    """
    return gone_within(require_recorded_pid(pid_file), REAP_DEADLINE_SECONDS)


def kill_unless_seen_gone(pid_file: Path, *, seen_gone: bool) -> None:
    """Kill the process recorded in *pid_file* unless it was already seen gone.

    SIGKILL on POSIX rather than SIGTERM, because a survivor can inherit an ignored SIGTERM from
    whatever launched the suite. On Windows ``os.kill`` with SIGTERM is ``TerminateProcess``,
    which nothing ignores. A record below 2 is skipped like an unreadable one: it names no
    process the suite started, and 0 would signal the runner's own group.
    """
    if seen_gone:
        return
    with contextlib.suppress(*_NOTHING_TO_KILL):
        pid = _read_recorded_pid(pid_file)
        if sys.platform == "win32":
            os.kill(pid, signal.SIGTERM)  # pragma: only-on win32
        else:  # pragma: only-on posix
            os.kill(pid, signal.SIGKILL)


@dataclass
class FixturePids:
    """The two pid files one fixture run records into, and what was seen of each process."""

    child: Path
    grandchild: Path
    child_gone: bool = False
    grandchild_gone: bool = False
    probed: bool = False

    def probe_exits(self) -> None:
        """Poll both recorded pids for their exit; call it before any assertion.

        Raises AssertionError when a pid was never recorded. The child is probed first, so a
        failure there leaves the grandchild not-seen-gone, and cleanup still kills it.
        """
        self.probed = True
        self.child_gone = seen_gone(self.child)
        self.grandchild_gone = seen_gone(self.grandchild)

    def kill_survivors(self) -> None:
        """Kill each recorded process that was not seen gone."""
        kill_unless_seen_gone(self.child, seen_gone=self.child_gone)
        kill_unless_seen_gone(self.grandchild, seen_gone=self.grandchild_gone)


@contextlib.contextmanager
def record_fixture_pids() -> Iterator[FixturePids]:
    """Export fresh pid-file paths for the fixture, and kill its survivors on the way out.

    Cleanup runs whether or not the block raised. A block that ends normally without calling
    :meth:`FixturePids.probe_exits` fails after cleanup: its cleanup had to signal pids it never
    saw, which is the pid-reuse hazard this module exists to prevent, so it must not pass quietly.
    """
    with tempfile.TemporaryDirectory() as tmp:
        pids = FixturePids(
            Path(tmp) / _CHILD_PID_FILE_NAME, Path(tmp) / _GRANDCHILD_PID_FILE_NAME
        )
        exported = {
            CHILD_PID_ENV: str(pids.child),
            GRANDCHILD_PID_ENV: str(pids.grandchild),
        }
        with mock.patch.dict(os.environ, exported):
            try:
                yield pids
            finally:
                pids.kill_survivors()
    if not pids.probed:
        raise AssertionError(
            "record_fixture_pids block ended without probe_exits(): call it right after the "
            "call under test, before any assertion"
        )
