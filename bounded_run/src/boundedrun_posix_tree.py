"""Kill a POSIX process tree that spans several sessions: freeze it, walk it, then kill it.

Private to ``bounded_run``: it lives in that folder's ``src/``, and nothing outside that
folder imports it. Its name carries the tool's prefix, so it cannot shadow another tool's
module on ``sys.path``.

Why one ``killpg`` is not enough
--------------------------------
``run_bounded`` starts every child with ``start_new_session=True``, so the child leads a group
whose id is its own pid. A descendant that itself calls ``run_bounded`` starts its child in
ANOTHER new session, and a ``killpg`` of the outer group never reaches it. Measured on a
scheduled job of nested tools: a deadline kill of the outer one left a nested run it had started
running for 35 minutes, and a live run showed five nested groups under the one the kill addressed.

Why freeze first
----------------
Parent-pid links are the only way down to those groups, and they break the moment a parent dies:
its children are re-parented to init/launchd. So nothing may die before the walk is done.
:func:`freeze_and_kill` repeats -- snapshot the process table, collect every group reachable
from the root, ``SIGSTOP`` them all -- until a snapshot finds no new group and shows every
collected process stopped or a zombie. The first snapshot comes before any signal, so the root
is checked first: one below 2, the caller's own group, or one listed in a group it does not
lead raises ValueError with nothing sent, because the walk would collect that whole group. Only then does it
``SIGKILL`` each group; ``SIGKILL`` ends a stopped process without a ``SIGCONT`` first. A process
that forks just as its group is stopped leaves a child that is still running, but its stopped
parent keeps the parent-pid link intact, so the next round finds and stops it. The loop cannot
end while anything it collected is still running.

What it still cannot reach: a descendant whose own parent had already exited before the kill and
which had left that parent's group -- no link of either kind leads to it. A member under another
uid ignores both signals (``EPERM``). And a killer that dies between the freeze and the kill
leaves the tree stopped rather than running. ``bounded_run.md`` records all three.

Entry point assumptions
-----------------------
Imported by ``bounded_run`` alone. :func:`kill_tree_posix` runs on POSIX only; everything else
is platform-neutral, taking the signal calls and the process table as seams, so its tests run on
every host. Standard library only.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import NamedTuple

#: The process-table snapshot: one row per process, the trailing ``=`` suppressing each header.
#: ``-A`` and these four keywords are understood by BSD/macOS ``ps`` and procps alike; a ``ps``
#: that refuses them (BusyBox, say) fails the snapshot, and the kill falls back to the root group.
_PS_ARGV = ("ps", "-A", "-o", "pid=,ppid=,pgid=,stat=")

#: Fields per ``_PS_ARGV`` row.
_PS_FIELDS = 4

#: Leading STAT letters of a process that can no longer fork: stopped (``T``; Linux's ``t`` is a
#: tracing stop), a zombie (``Z``) or dead (Linux's ``X``).
_INERT_STATES = frozenset("TtZX")

#: The lowest group id this module will ever signal. ``killpg(0, ...)`` addresses the CALLER's
#: own group, and group 1 is init/launchd's; neither can be a descendant's, so a garbled row
#: naming one is dropped rather than signaled.
_MIN_SIGNALABLE_GROUP = 2


class ProcessRow(NamedTuple):
    """One process as a snapshot shows it: its pid, parent, group and STAT column."""

    pid: int
    ppid: int
    pgid: int
    state: str


@dataclass(frozen=True)
class TreeKillSeams:
    """Everything :func:`freeze_and_kill` touches outside itself; tests substitute recorders.

    *send* is ``killpg``-shaped: (group id, signal). *snapshot* takes the seconds it may use and
    returns the table, or None when it could not be read. *own_group* is never stopped, because a
    stopped caller could never go on to finish the kill.
    """

    send: Callable[[int, int], None]
    stop_signal: int
    kill_signal: int
    snapshot: Callable[[float], tuple[ProcessRow, ...] | None]
    clock: Callable[[], float]
    own_group: int


# fuzz-target: the process-table parser over arbitrary ps output
def parse_process_table(text: str) -> tuple[ProcessRow, ...]:
    """The rows of *text*, ``_PS_ARGV``'s output; a line that is not a well-formed row is skipped.

    Skipping, not raising: the kill must go on with whatever the table does show, and a row this
    cannot read is one it would not know how to signal anyway.
    """
    rows: list[ProcessRow] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != _PS_FIELDS:
            continue
        try:
            pid, ppid, pgid = (int(field) for field in fields[:3])
        except ValueError:
            continue
        if min(pid, ppid, pgid) >= 0:
            rows.append(ProcessRow(pid, ppid, pgid, fields[3]))
    return tuple(rows)


def tree_groups(root: int, rows: Iterable[ProcessRow]) -> frozenset[int]:
    """Every group reachable from *root*'s: through a child's parent pid, or a shared group.

    Both links matter. A child found by its parent brings its group, and a group found brings
    every member -- including one whose parent already exited, which is re-parented away and so
    reachable through its group alone. *root* is always included, whether or not it is listed.
    """
    table = tuple(rows)
    members = {root}
    groups = {root}
    grew = True
    while grew:
        grew = False
        for row in table:
            if row.pid in members or row.pgid < _MIN_SIGNALABLE_GROUP:
                continue
            if row.pgid in groups or row.ppid in members:
                members.add(row.pid)
                groups.add(row.pgid)
                grew = True
    return frozenset(groups)


def is_frozen(rows: Iterable[ProcessRow], groups: frozenset[int]) -> bool:
    """Whether every listed member of *groups* is stopped or a zombie, so none of them can fork."""
    return all(row.state[:1] in _INERT_STATES for row in rows if row.pgid in groups)


def _signal_groups(
    send: Callable[[int, int], None], groups: Iterable[int], sig: int
) -> None:
    """Send *sig* to each group, tolerating one already gone or not ours to signal.

    macOS reports ESRCH once a group is gone and EPERM while an unreaped leader is its only
    member. EPERM also means a member under another uid could not be signaled; it keeps running.
    """
    for group in sorted(groups):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            send(group, sig)


def refuse_unsignalable_root(root: int) -> None:
    """Raise ValueError when *root* is below 2: no child can have it as its pid.

    ``killpg(0, ...)`` addresses the CALLER's own group and group 1 is init/launchd's, so a
    garbled or defaulted pid must fail here rather than reach a signal call.
    """
    if root < _MIN_SIGNALABLE_GROUP:
        raise ValueError(f"refusing to kill the tree of pid {root}: no child has it")


def _refuse_foreign_root(root: int, rows: Iterable[ProcessRow]) -> None:
    """Raise ValueError when the table lists *root* in a group other than its own.

    Every group reachable from a root is collected, so a root in someone else's group would
    take that whole group, its leader included. A root the table does not list -- a leader
    that exited and was reaped, whose group lives on -- has nothing to check and passes.
    """
    for row in rows:
        if row.pid == root and row.pgid != root:
            raise ValueError(
                f"refusing to kill the tree of pid {root}: it is in group {row.pgid}, "
                "not leading its own"
            )


def _bounded_snapshot(
    seams: TreeKillSeams, deadline: float
) -> tuple[ProcessRow, ...] | None:
    """The table, given whatever time is left before *deadline*; None once none is."""
    remaining = deadline - seams.clock()
    return seams.snapshot(remaining) if remaining > 0 else None


def freeze_and_kill(root: int, timeout: float, seams: TreeKillSeams) -> None:
    """Stop *root*'s group and every group reachable from it until none can fork, then kill all.

    *root* must lead its own group, as every child ``run_bounded`` starts does, and ValueError
    is raised with no signal sent when it cannot: a root below 2, the caller's own group, or
    one the first snapshot -- taken before any signal for that reason -- shows in another
    group. Bounded by *timeout*: past it, or once a snapshot cannot be read, every group
    collected so far is killed as it stands -- at the least *root*'s, which is exactly the
    kill this replaced, and what an unreadable first snapshot leaves.
    """
    refuse_unsignalable_root(root)
    if root == seams.own_group:
        raise ValueError(f"refusing to kill group {root}: it is the caller's own")
    collected = frozenset({root})
    deadline = seams.clock() + timeout
    # Snapshot before the first STOP, accepted trade-off: the root's group runs one `ps` call
    # longer, free to fork, but a root that leads no group of its own is refused unsignaled.
    rows = _bounded_snapshot(seams, deadline)
    if rows is not None:
        _refuse_foreign_root(root, rows)
    while rows is not None:
        found = tree_groups(root, rows) - {seams.own_group}
        watched = collected | found
        # Settled only when this snapshot found nothing new and shows nothing still running:
        # then no process in the tree can start another. The first snapshot predates every
        # stop, so it settles only a tree that was already inert.
        settled = found <= collected and is_frozen(rows, watched)
        collected = watched
        if settled:
            break
        _signal_groups(seams.send, watched, seams.stop_signal)
        rows = _bounded_snapshot(seams, deadline)
    _signal_groups(seams.send, collected, seams.kill_signal)


def take_snapshot(
    timeout: float, argv: tuple[str, ...] = _PS_ARGV
) -> tuple[ProcessRow, ...] | None:
    """The process table *argv* prints, or None when it fails, exits non-zero or outlives *timeout*.

    A bounded capturing ``subprocess.run`` is safe HERE for the reason ``taskkill`` is in
    ``bounded_run``: ``ps`` spawns nothing, so no grandchild can hold its pipes.
    """
    try:
        done = subprocess.run(  # reliability: ignore[RL005] leaf; ps spawns nothing
            list(argv),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    return parse_process_table(done.stdout)


def kill_tree_posix(pid: int, timeout: float) -> None:  # pragma: only-on posix
    """Freeze, walk and kill the tree *pid* leads, with the real signals and ``ps``.

    POSIX only: on Windows ``kill_tree`` kills through a job object and never calls this.
    """
    # The early exit is what lets mypy's win32 pass skip the names below: it narrows on
    # sys.platform, and os.killpg, os.getpgrp and the two signals are declared for POSIX alone.
    if sys.platform == "win32":
        raise NotImplementedError("POSIX only")  # pragma: only-on win32
    seams = TreeKillSeams(
        send=os.killpg,
        stop_signal=signal.SIGSTOP,
        kill_signal=signal.SIGKILL,
        snapshot=take_snapshot,
        clock=time.monotonic,
        own_group=os.getpgrp(),
    )
    freeze_and_kill(pid, timeout, seams)
