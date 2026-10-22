"""The children ``bounded_run`` has started and not yet finished, so a caller can kill them all.

Private to ``bounded_run``: it lives in that folder's ``src/``, and nothing outside that
folder imports it. Its name carries the tool's prefix, so it cannot shadow another tool's
module on ``sys.path``.

Why a registry
--------------
Every child starts in a session of its own (``start_new_session=True``), which is what lets a
timeout kill its whole tree. The same choice puts it beyond a signal sent to the caller: a
logout's SIGTERM, or a scheduler stopping the job, ends the caller and leaves every child
running unattended -- and with the caller gone, any lock it held is free while they run. A
caller that catches such a signal needs the pids it cannot otherwise see; this module keeps
them, and ``bounded_run.kill_live_trees`` kills each one's tree.

The races, and why they are harmless
------------------------------------
A pid is added once ``Popen`` has returned and removed when its drain ends, however it ends. A
signal handler runs in the main thread between two bytecodes, so it can run after the drain has
reaped the child and before the pid is removed. Its kill then addresses a group whose leader is
gone: either members of that group are still alive, and killing them is what was asked, or
none are, and the kill finds nothing (``ProcessLookupError``, suppressed in the tree kill). A
reaped pid could only be reused by a new process in that window of a few bytecodes, which is
not a real hazard. Threads that run children concurrently share the set safely: every update is
one set operation under the GIL, and the kill walks a snapshot.

Entry point assumptions
-----------------------
Imported by ``bounded_run`` alone. Standard library only.
"""

from __future__ import annotations

from collections.abc import Callable

#: The pid of every child a run has started and whose drain has not yet ended.
_LIVE: set[int] = set()


def track(pid: int) -> None:
    """Note that child *pid* is running."""
    _LIVE.add(pid)


def forget(pid: int) -> None:
    """Note that child *pid*'s run has ended; a pid never tracked is ignored."""
    _LIVE.discard(pid)


def live_pids() -> tuple[int, ...]:
    """The pids of the children running now, in no particular order: what a kill would reach."""
    return tuple(_LIVE)


def kill_each(kill: Callable[[int], None]) -> int:
    """Call *kill* on every running child's pid; how many kills completed.

    A kill that raises ``OSError`` (a process-table snapshot that could not run, say) or
    ``ValueError`` (a root the tree kill refuses, with nothing sent) is skipped and not
    counted, so one tree that cannot be killed does not spare the rest.
    """
    killed = 0
    for pid in live_pids():
        try:
            kill(pid)
        except (OSError, ValueError):
            continue
        killed += 1
    return killed
