"""Drain a contained child under its bound, measured on the caller's choice of clock.

Private to ``bounded_run``: it lives in that folder's ``src/``, and nothing outside that
folder imports it. Its name carries the tool's prefix, so it cannot shadow another tool's
module on ``sys.path``.

Why a clock is a parameter
--------------------------
``Popen.communicate(timeout=...)`` measures its bound on ``time.monotonic()``. On macOS that is
``mach_absolute_time()``, which stops while the machine sleeps. A two-hour bound on a laptop
that sleeps mid-run is therefore two hours of AWAKE time, and a run that sleeps is never killed.
A caller that means wall time passes :func:`sleep_inclusive_clock`. The drain then waits in
slices of at most :data:`SLICE_SECONDS` and re-reads that clock between them. With no clock, the
single ``communicate`` call is exactly what it always was. A clock cannot be combined with
stdin input: see :func:`refuse_clock_with_stdin`.

Entry point assumptions
-----------------------
Imported by ``bounded_run`` alone, which owns spawning and job containment. This module owns the
wait and the post-kill re-read. Standard library only.
"""

from __future__ import annotations

import functools
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from typing import TypeVar

#: Seconds allowed for the pipes to drain AFTER the tree kill. Generous relative to the 0.94s
#: taskkill + immediate drain measured on Windows: this is a safety net, not a tuning knob, and
#: exceeding it means the kill did not work -- which we report rather than wait out.
KILL_GRACE_SECONDS = 5.0

#: The longest single wait when a clock is given. The kill lands within one slice of AWAKE time
#: after the deadline passes, because nothing runs while the machine sleeps, the kill included.
SLICE_SECONDS = 30.0

#: Injectable tree-kill seam: pid -> None. Tests substitute a recorder so the expiry branches are
#: reachable without spawning a real process or waiting out a real timeout.
KillTreeFn = Callable[[int], None]

#: A clock: seconds as a float, meaningful only against another reading of the same clock.
Clock = Callable[[], float]

#: Streams are str or bytes depending on which entry point ran; the bounded-communicate helper is
#: generic over that so both variants share one implementation of the ordering rule.
_Stream = TypeVar("_Stream", str, bytes)

#: Per ``sys.platform``, the ``clock_gettime`` clock that keeps counting while the machine
#: sleeps. macOS's CLOCK_MONOTONIC counts sleep (``time.monotonic()`` there is CLOCK_UPTIME_RAW,
#: which does not). Linux's CLOCK_MONOTONIC stops in suspend, and CLOCK_BOOTTIME is the one
#: that counts it. Held as NAMES and read with getattr because typeshed declares each constant
#: for some platforms only, and this file must type-check as win32, linux and darwin alike.
_SLEEP_INCLUSIVE_CLOCKS = {"darwin": "CLOCK_MONOTONIC", "linux": "CLOCK_BOOTTIME"}

#: The attribute the drain's interrupt path sets on the interrupt it has already acted on, so
#: the spawn frame's guard further out does not kill and reap the same child a second time.
_HANDLED_ATTR = "_bounded_run_handled"


def sleep_inclusive_clock() -> float:
    """Seconds on a clock that keeps counting while the machine sleeps.

    Falls back to ``time.time()`` where no such clock is known, Windows among them. It counts
    sleep too, but it steps when the system clock is set, so it is the fallback, not the rule.
    """
    return clock_reader(sys.platform)()


def clock_reader(platform: str) -> Clock:
    """The reader :func:`sleep_inclusive_clock` uses on *platform*, a ``sys.platform`` value."""
    name = _SLEEP_INCLUSIVE_CLOCKS.get(platform)
    clock_id: "int | None" = None if name is None else getattr(time, name, None)
    read: "Callable[[int], float] | None" = getattr(time, "clock_gettime", None)
    if clock_id is None or read is None:
        return time.time
    return functools.partial(read, clock_id)


def refuse_clock_with_stdin(
    clock: Clock | None, stdin_data: str | bytes | None
) -> None:
    """Raise ValueError when *clock* is given with non-empty *stdin_data*; call before spawning.

    The sliced wait retries ``communicate``, and a retry may not pass the input again. On CPython
    3.12 and earlier (the 3.9, 3.10 and 3.12 sources) the POSIX drain arms the stdin pipe only
    for a truthy input ARGUMENT, so a retry never writes what the first slice could not; 3.14
    resumes from the input it kept. Input larger than the pipe buffer would stall the child until the
    deadline, and the kill would read as a spurious timeout. Empty input is only an EOF.
    """
    if clock is not None and stdin_data:
        raise ValueError("clock= cannot be combined with stdin input")


def refuse_bad_call(
    argv: Sequence[str], clock: Clock | None, stdin_data: str | bytes | None
) -> None:
    """Every check a run makes before spawning: a ``str`` or ``bytes`` *argv*, then the clock.

    A string argv raises TypeError. ``list(argv)`` would split it one character per argument,
    so ``"ls -l"`` would try to run a program named ``l``. Then :func:`refuse_clock_with_stdin`.
    """
    if isinstance(argv, (str, bytes)):
        raise TypeError("argv must be a sequence of arguments, not a str or bytes")
    refuse_clock_with_stdin(clock, stdin_data)


def _communicate_bounded(
    proc: subprocess.Popen[_Stream],
    argv: Sequence[str],
    timeout: float,
    kill_tree_fn: KillTreeFn,
    stdin_data: _Stream | None = None,
    clock: Clock | None = None,
) -> tuple[_Stream, _Stream]:
    """Drain *proc* under *timeout*; on expiry kill its tree BEFORE re-reading the streams.

    Raises ``subprocess.TimeoutExpired`` on expiry, carrying whatever output the post-kill read
    recovered -- the same shape ``subprocess.run`` raises, so existing handlers are unaffected.
    If that read also expires the tree kill did not work; the partial output is abandoned and the
    exception raised without it, because a bound that always returns beats one that sometimes
    hangs.

    With a *clock*, the bound is measured on it instead of ``time.monotonic()``: see
    :func:`_wait_on_clock`. The public entry points then allow only empty *stdin_data*.

    A ``KeyboardInterrupt`` during the wait kills the tree too, before it propagates: see
    :func:`_kill_and_reap`, after which the same interrupt is marked handled and re-raised, so
    the spawn frame's guard does not repeat the kill: see :func:`interrupt_handled`.
    """
    try:
        if clock is None:
            return proc.communicate(input=stdin_data, timeout=timeout)
        return _wait_on_clock(proc, stdin_data, timeout, clock)
    except KeyboardInterrupt as interrupt:
        _kill_and_reap(proc, kill_tree_fn)
        setattr(interrupt, _HANDLED_ATTR, True)
        raise
    except subprocess.TimeoutExpired:
        kill_tree_fn(proc.pid)
        try:
            stdout, stderr = proc.communicate(timeout=KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            raise subprocess.TimeoutExpired(list(argv), timeout) from None
        raise subprocess.TimeoutExpired(
            list(argv), timeout, output=stdout, stderr=stderr
        ) from None


def interrupt_handled(interrupt: BaseException) -> bool:
    """Whether the drain's interrupt path already killed and reaped for *interrupt*.

    Set only once that path's kill and its bounded reap have both run, so a second interrupt
    that cut the path short arrives unmarked and is acted on again further out. A reap that
    expired leaves ``returncode`` unset; without this mark the spawn frame would kill and reap
    once more, and the bound would grow to two grace periods (``bounded_run.md``, property 7).
    """
    return getattr(interrupt, _HANDLED_ATTR, False) is True


def _kill_and_reap(proc: subprocess.Popen[_Stream], kill_tree_fn: KillTreeFn) -> None:
    """Kill *proc*'s interrupted tree and reap it under ``KILL_GRACE_SECONDS``.

    Every child starts in a session of its own, so the terminal's SIGINT reaches only the caller:
    without this, Ctrl-C would leave the child running unattended after the caller said it had
    stopped. Only ``KeyboardInterrupt`` is caught, never ``BaseException`` at large: a
    ``SystemExit`` is the caller's own decision, not an interruption of the child. The reap is a
    ``communicate`` (which also closes the pipes) rather than a bare ``wait``. A reap that expires
    means the kill did not work; there is nothing more to do here, and the interrupt is what the
    caller must see, so the expiry is dropped deliberately rather than raised in its place.
    """
    kill_tree_fn(proc.pid)
    try:
        proc.communicate(timeout=KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass  # the caller's re-raised interrupt is the outcome; see the docstring


def _wait_on_clock(
    proc: subprocess.Popen[_Stream],
    stdin_data: _Stream | None,
    timeout: float,
    clock: Clock,
) -> tuple[_Stream, _Stream]:
    """Wait for *proc* in slices until it exits or *clock* has advanced *timeout* seconds.

    Each slice is ``communicate(timeout=min(SLICE_SECONDS, remaining))``, and *remaining* is
    re-read from *clock* between slices. Retrying ``communicate`` after its TimeoutExpired loses
    no output (documented), but it refuses input a second time, so *stdin_data* goes to the
    first slice only. Whether a retry goes on writing the rest depends on the CPython version,
    which is why :func:`refuse_clock_with_stdin` keeps non-empty input away from here. Raises
    TimeoutExpired once the clock passes the deadline, for the caller's tree kill to handle.
    """
    deadline = clock() + timeout
    pending = stdin_data
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            # Replaced by the caller's own raise, which names argv and carries the output.
            raise subprocess.TimeoutExpired([], timeout)
        try:
            return proc.communicate(
                input=pending, timeout=min(SLICE_SECONDS, remaining)
            )
        except subprocess.TimeoutExpired:
            pending = None
