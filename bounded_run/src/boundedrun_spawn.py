"""Start a ``bounded_run`` child and register it at once, so no stop can orphan it.

Private to ``bounded_run``: it lives in that folder's ``src/``, and nothing outside that
folder imports it. Its name carries the tool's prefix, so it cannot shadow another tool's
module on ``sys.path``.

Why the spawn is its own module
-------------------------------
``bounded_run.py`` sat at the file-length bar, and the spawn is a seam of its own: which
streams the child gets, how it is detached from the caller's session, and which flags it
starts with. ``bounded_run`` still computes those flags (``spawn_creationflags``) and passes
them in, so its tests can substitute them where they always have.

The spawn-to-track window
-------------------------
A child leads a session of its own, so a stop signal sent to the caller never reaches it; the
caller's handler reaches it only through ``kill_live_trees``, which kills the children the
registry holds. :func:`run_text` and :func:`run_bytes` therefore call ``Popen`` and register the
pid in one frame, on consecutive statements, with nothing between: no frame hands the object
back to a caller first, so no frame return -- where a debugger's or a pure-Python coverage
tracer's code runs, and with it a pending signal's handler -- falls between them. A
``KeyboardInterrupt`` raised before the registration completes -- the stop such a handler
raises, landing on the call that registers -- finds the child unreachable through the registry,
so this module kills its tree and reaps it before re-raising. Should *kill_tree_fn* itself
raise there, its exception replaces the interrupt, which it keeps as ``__context__``, exactly
as on the drain's interrupt path.

Before this module, ``Popen`` returned to the run function, which called the drain helper,
which called the containment helper, and only then was the pid registered: two frame entries
between ``Popen`` and the registration. A frame entry is a point where stock CPython checks for
a pending signal, with or without a tracer, so a stop landing on either found the child
untracked and left it running. The one-frame shape removes both, and with them the frame
returns a Python-level tracer or profiler would add. The residual at ``Popen``'s own call
completion, below, is unchanged.

Once registered, the child is forgotten by a ``finally`` in that same frame, around both the
registration and the drain, so no frame entry -- the drain's own included, a point CPython does
check with no tracer at all -- falls between the registration and the guard that forgets. The
interrupt guard spans the drain as well, so an interrupt after the registration but before the
drain's own interrupt path is in force kills and reaps the child too, and a caller that never
calls ``kill_live_trees`` orphans nothing. An interrupt the drain's own path already acted on
is not acted on again: that path marks it handled once its kill and bounded reap have run, so
the kill happens once and the bound stays one grace period, even when the reap expired. A child
already reaped is not signaled again either: its pid may have been reused.

What stays out of reach is an interrupt delivered inside ``Popen``'s own call, before that call
hands the object back: in ``Popen``'s code after the child has started, at the pending-signal
check the interpreter makes as the call instruction completes, or in a hook run per instruction.
No object reaches this frame, so nothing here can name the child, and it is LEFT RUNNING in its
own session; CPython at most reaps it after it exits on its own (``bounded_run.md``, property 8).

Entry point assumptions
-----------------------
Imported by ``bounded_run`` alone, with this ``src/`` already on ``sys.path``. Standard library
and this ``src/``'s siblings only; the text-mode stream policy arrives as arguments, from
``stdio_utf8``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TypeVar

import boundedrun_live
from boundedrun_drain import KillTreeFn, _kill_and_reap, _Stream, interrupt_handled

#: Whatever the caller's drain returns, handed back beside the child it drained.
_Drained = TypeVar("_Drained")


def _abandon(
    proc: subprocess.Popen[_Stream] | None,
    kill_tree_fn: KillTreeFn,
    interrupt: KeyboardInterrupt,
) -> None:
    """Kill and reap the interrupted *proc*, unless there is none or it was already handled.

    *proc* is ``None`` when the interrupt was raised inside ``Popen``'s own call: no object
    came back, so there is no child here to name (``bounded_run.md``, property 8). An
    *interrupt* the drain's own path marked handled was already acted on: that path killed the
    tree and spent its one bounded reap, and a second kill would double the bound
    (property 7), whether or not the reap completed. A set ``returncode`` means the leader was
    reaped, and its pid may since have been reused, so it must not be signaled again. The
    caller's ``finally`` forgets the pid, whether or not this kill completes.
    """
    if (
        proc is not None
        and proc.returncode is None
        and not interrupt_handled(interrupt)
    ):
        _kill_and_reap(proc, kill_tree_fn)


def _stdin_mode(piped_stdin: bool) -> int:
    """A pipe when the caller feeds the child, else ``DEVNULL``: an immediate EOF.

    Deliberately never inheritance: the run captures both output streams, so the child cannot
    be interactive anyway, and a child that reads stdin would otherwise eat the caller's own.
    """
    return subprocess.PIPE if piped_stdin else subprocess.DEVNULL


def run_text(
    argv: Sequence[str],
    *,
    cwd: Path | str | None,
    env: Mapping[str, str] | None,
    piped_stdin: bool,
    creationflags: int,
    encoding: str,
    errors: str,
    drain: Callable[[subprocess.Popen[str]], _Drained],
    kill_tree_fn: KillTreeFn,
) -> tuple[subprocess.Popen[str], _Drained]:
    """Start *argv* piped as text in *encoding*, register it, drain it, and forget it.

    The registration is the statement right after ``Popen``, in this frame, with nothing
    between: no frame returns the object first. An interrupt raised anywhere after ``Popen``
    returns -- before the registration completes, or after it but before the drain's own
    interrupt path is in force -- kills the child's tree through *kill_tree_fn* and reaps it,
    then propagates: a caller need not call ``kill_live_trees`` for the child to stop. Only
    ``KeyboardInterrupt`` is caught, as in the drain's own interrupt path: a ``SystemExit`` is
    the caller's own decision, not an interruption of the child.
    """
    proc: subprocess.Popen[str] | None = None
    try:
        try:
            proc = subprocess.Popen(
                list(argv),
                cwd=cwd,
                env=env,
                stdin=_stdin_mode(piped_stdin),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding=encoding,
                errors=errors,
                # Verified accepted on win32 as well as POSIX, so no platform branch is needed. On
                # POSIX it makes the child a process-group leader, which is what killpg needs.
                start_new_session=True,
                # Suspended on Windows until bounded_run has put the child in its job, and with no
                # console when the caller asks; 0 elsewhere.
                creationflags=creationflags,
            )
            boundedrun_live.track(proc.pid)
            return proc, drain(proc)
        except KeyboardInterrupt as interrupt:
            _abandon(proc, kill_tree_fn, interrupt)
            raise
    finally:
        # Here, in the frame that registered it, so no frame entry falls between the
        # registration and this guard: however the run ends, the pid is forgotten.
        if proc is not None:
            boundedrun_live.forget(proc.pid)


def run_bytes(
    argv: Sequence[str],
    *,
    cwd: Path | str | None,
    env: Mapping[str, str] | None,
    piped_stdin: bool,
    creationflags: int,
    drain: Callable[[subprocess.Popen[bytes]], _Drained],
    kill_tree_fn: KillTreeFn,
) -> tuple[subprocess.Popen[bytes], _Drained]:
    """Start *argv* with every stream piped as bytes; otherwise exactly :func:`run_text`.

    A sibling rather than a ``text`` switch so each ``Popen`` call stays literal: mypy reads
    the stream type from it, and the registration stays the next statement after it.
    """
    proc: subprocess.Popen[bytes] | None = None
    try:
        try:
            proc = subprocess.Popen(
                list(argv),
                cwd=cwd,
                env=env,
                stdin=_stdin_mode(piped_stdin),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                creationflags=creationflags,
            )
            boundedrun_live.track(proc.pid)
            return proc, drain(proc)
        except KeyboardInterrupt as interrupt:
            _abandon(proc, kill_tree_fn, interrupt)
            raise
    finally:
        # Here, in the frame that registered it, so no frame entry falls between the
        # registration and this guard: however the run ends, the pid is forgotten.
        if proc is not None:
            boundedrun_live.forget(proc.pid)
