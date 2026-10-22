"""Bound a captured subprocess so a surviving grandchild cannot outlive the timeout.

Why this exists
---------------
``subprocess.run(..., capture_output=True, timeout=N)`` looks like a bound on the whole call.
It is not. On ``TimeoutExpired`` CPython kills only the DIRECT child, then goes back to reading
the captured pipes to assemble the exception's ``output`` -- and any surviving *grandchild* that
inherited those handles keeps them open, so that read blocks indefinitely. The bound is reliable
for a leaf child and unreliable for exactly the nested case the bound exists to catch, which is
the shape every orchestrator that runs other tools has.

This module inverts the order: on expiry it kills the whole process TREE first, and only then
touches the streams. Measured on Windows: the unfixed path did not return within 5 minutes
against a 3.0s bound; this path, then killing through ``taskkill /T``, returned in 3.97s.

On Windows the tree is a job object the child joins while still suspended. A nested run's job
nests inside it, so the kill reaches every descendant whether or not the child is alive. On
POSIX the child leads a process group, but a descendant that itself runs ``run_bounded`` leads a
NEW session, beyond that group's reach, so the kill freezes every group reachable from the
child's through the process table, then kills them all (``src/boundedrun_posix_tree.py``). Only
a descendant whose own parent exited before the kill, and which left that parent's group, is
out of the POSIX walk's reach. ``bounded_run.md`` records why, and what has been run where.

Entry point assumptions
-----------------------
``run_bounded`` is a drop-in for a capturing ``subprocess.run`` -- it bakes in ``capture_output``,
``encoding``/``errors`` (the policy ``stdio_utf8`` defines) and ``check=False``, returns a
``CompletedProcess``, and raises ``subprocess.TimeoutExpired`` carrying partial output. Every
existing ``except subprocess.TimeoutExpired`` handler therefore keeps working unchanged.

This module is a library: it has no CLI, and each tool imports it after putting this folder on
``sys.path`` from its own location, as ``bounded_run.md`` documents.
"""

from __future__ import annotations

import contextlib
import ctypes
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

# stdio_utf8 is the shared stream-policy library, resolved from this file's own location rather
# than from the working directory, so moving the project needs no edit here.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "stdio_utf8"))

import stdio_utf8  # noqa: E402 - import follows the sys.path bootstrap above

# The wait itself -- how long to block on the child, on which clock, and the kill-then-reread
# order when it expires -- lives in this tool's private src/, apart from the spawning and the
# Windows job containment this file owns.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from boundedrun_drain import (  # noqa: E402 - import follows the sys.path bootstrap above
    KILL_GRACE_SECONDS as KILL_GRACE_SECONDS,
    Clock,
    KillTreeFn,
    _communicate_bounded as _communicate_bounded,
    _Stream,
    refuse_bad_call,
    sleep_inclusive_clock as sleep_inclusive_clock,
)
import boundedrun_live
from boundedrun_live import live_pids as live_pids  # public: is a child now reachable?
import boundedrun_posix_tree
import boundedrun_spawn
from boundedrun_spawn_flags import spawn_creationflags

# The Win32 structure layouts and the kernel32 function table, re-exported under their old names.
from boundedrun_kernel32 import (
    _KERNEL32_SIGNATURES as _KERNEL32_SIGNATURES,
    _JobExtendedLimits as _JobExtendedLimits,
    _ThreadEntry32 as _ThreadEntry32,
)

#: Bound on the tree-kill call itself. A kill that cannot complete must not become the new hang.
KILL_TIMEOUT_SECONDS = 10.0

# Windows job containment: documented Win32 values and layouts, none exported by subprocess.

#: Lets a descendant that explicitly asks for CREATE_BREAKAWAY_FROM_JOB leave the job, instead of
#: that CreateProcess failing inside it. Settable only through the extended-limit class.
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_TH32CS_SNAPTHREAD = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002
_ERROR_NO_MORE_FILES = 18

#: PROCESS_SET_QUOTA | PROCESS_TERMINATE: the rights AssignProcessToJobObject requires.
_ASSIGN_RIGHTS = 0x0100 | 0x0001

#: Failure values: ResumeThread's (DWORD)-1, and INVALID_HANDLE_VALUE as a c_void_p restype
#: returns it -- every bit of a pointer set.
_RESUME_THREAD_FAILED = 0xFFFFFFFF
_INVALID_HANDLE_VALUE = 2 ** (8 * ctypes.sizeof(ctypes.c_void_p)) - 1

#: The exit code a job kill gives its members: the one ``taskkill /F`` gave them.
_TERMINATED_EXIT_CODE = 1


class _JobApi(Protocol):
    """Job-object operations containment needs, each raising OSError; tests substitute a fake."""

    def create_job(self) -> int:
        """Create an empty job and return its handle."""

    def assign(self, job: int, pid: int) -> None:
        """Put process *pid* in *job*."""

    def resume(self, pid: int) -> None:
        """Resume the suspended process *pid*."""

    def terminate(self, job: int) -> None:
        """Terminate every process in *job*."""

    def close(self, job: int) -> None:
        """Close the handle to *job*."""


class _KernelJobApi:
    """The job seam over kernel32; each failing call raises the error Windows reported.

    Library and last-error reader are arguments, so the whole sequence runs on any host against
    C function pointers built from ``_KERNEL32_SIGNATURES``; only binding them is Windows-only.
    """

    def __init__(self, k32: ctypes.CDLL, last_error: Callable[[], OSError]) -> None:
        """Wrap the loaded *k32*, reading the cause of each failure through *last_error*."""
        self._k32 = k32
        self._last_error = last_error

    def _check(self, result: object) -> None:
        """Raise the last error when a call returned its failure value, NULL or FALSE."""
        if not result:
            raise self._last_error()

    def create_job(self) -> int:
        """A new, empty job whose members may still break away when they explicitly ask to."""
        job = self._k32.CreateJobObjectW(None, None)
        self._check(job)
        limits = _JobExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_BREAKAWAY_OK
        info_class = _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION
        ref, size = ctypes.byref(limits), ctypes.sizeof(limits)
        if not self._k32.SetInformationJobObject(job, info_class, ref, size):
            # Read the cause BEFORE closing: the close is a kernel32 call too, and would replace it.
            error = self._last_error()
            self.close(job)
            raise error
        return int(job)

    def assign(self, job: int, pid: int) -> None:
        """Put *pid* in *job* through a handle opened with exactly the rights assignment needs."""
        process = self._k32.OpenProcess(_ASSIGN_RIGHTS, False, pid)
        self._check(process)
        try:
            self._check(self._k32.AssignProcessToJobObject(job, process))
        finally:
            self._k32.CloseHandle(process)

    def resume(self, pid: int) -> None:
        """Resume *pid*'s threads; a process created suspended has exactly one."""
        thread_ids = self._thread_ids(pid)
        if not thread_ids:
            raise OSError(f"no thread found for the suspended child {pid}")
        for thread_id in thread_ids:
            thread = self._k32.OpenThread(_THREAD_SUSPEND_RESUME, False, thread_id)
            self._check(thread)
            try:
                if self._k32.ResumeThread(thread) == _RESUME_THREAD_FAILED:
                    raise self._last_error()
            finally:
                self._k32.CloseHandle(thread)

    def _thread_ids(self, pid: int) -> list[int]:
        """*pid*'s thread ids, from a system snapshot (Popen discards the thread handle)."""
        snapshot = self._k32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
        if snapshot == _INVALID_HANDLE_VALUE:
            raise self._last_error()
        try:
            entry = _ThreadEntry32(dwSize=ctypes.sizeof(_ThreadEntry32))
            thread_ids: list[int] = []
            more = self._k32.Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if entry.th32OwnerProcessID == pid:
                    thread_ids.append(entry.th32ThreadID)
                more = self._k32.Thread32Next(snapshot, ctypes.byref(entry))
            # FALSE ends a walk at its end and on a failure alike; only the last error tells.
            error = self._last_error()
            if getattr(error, "winerror", None) != _ERROR_NO_MORE_FILES:
                raise error
            return thread_ids
        finally:
            self._k32.CloseHandle(snapshot)

    def terminate(self, job: int) -> None:
        """Terminate every process in *job*, whoever its parent was and whether or not it lives."""
        self._check(self._k32.TerminateJobObject(job, _TERMINATED_EXIT_CODE))

    def close(self, job: int) -> None:
        """Close the job handle. Members still running keep running: no KILL_ON_JOB_CLOSE is set."""
        # Unchecked on purpose: this runs in a finally, where raising would replace the run's own
        # result or TimeoutExpired, and a failed close of our own handle leaves nothing to recover.
        self._k32.CloseHandle(job)


#: The job API this platform contains children with, or None where the process group already is
#: the tree. A module attribute rather than a constant, so tests can route spawns through a fake.
_job_api_factory: Callable[[], _JobApi] | None

# An if, not a function-level assert: mypy did not narrow on `assert sys.platform == "win32"`
# there (measured), and WinDLL, WinError and get_last_error are declared for win32 alone.
if sys.platform == "win32":

    def _bind_kernel32() -> ctypes.CDLL:  # pragma: only-on win32
        """Load kernel32 with every ``_KERNEL32_SIGNATURES`` entry applied to its function."""
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, restype, argtypes in _KERNEL32_SIGNATURES:
            function = getattr(kernel32, name)
            function.restype = restype
            function.argtypes = argtypes
        return kernel32

    def _last_windows_error() -> OSError:  # pragma: only-on win32
        """The calling thread's last kernel32 error, as the ``OSError`` subclass it maps to."""
        return ctypes.WinError(ctypes.get_last_error())

    def _win32_job_api() -> _JobApi:  # pragma: only-on win32
        """The real job API, bound per spawn: reloading a loaded DLL only takes a reference."""
        return _KernelJobApi(_bind_kernel32(), _last_windows_error)

    _job_api_factory = _win32_job_api  # pragma: only-on win32
else:  # pragma: only-on posix
    _job_api_factory = None

#: Each running child's job, by pid, until its run ends: how kill_tree(pid) reaches a tree whose
#: leader already exited. The pid cannot be reused meanwhile; the child's Popen holds its handle.
_JOBS: dict[int, tuple[_JobApi, int]] = {}


def kill_tree(pid: int) -> None:
    """Kill process *pid* AND every descendant it spawned.

    A child this module started, while its run lasts, is killed through its job, which reaches
    every descendant whether or not *pid* is still alive. Any other pid gets the platform's tree
    kill: ``taskkill /T`` on Windows, which reaches only descendants still linked to a live *pid*,
    or on POSIX a freeze of every group reachable from *pid*'s, then a kill of each
    (``boundedrun_posix_tree.freeze_and_kill``), where *pid* must lead its own process group.
    A *pid* below 2 raises ValueError on every platform, before anything is signaled.

    The branch tests ``sys.platform == "win32"``, not ``os.name``, because mypy narrows on
    ``sys.platform`` alone; ``kill_tree_posix`` narrows the same way, so its POSIX-only
    ``os.killpg`` and ``signal.SIGKILL`` type-check on a Windows checkout.
    """
    boundedrun_posix_tree.refuse_unsignalable_root(pid)
    registered = _JOBS.get(pid)
    if registered is not None:
        api, job = registered
        # A member that may not be terminated keeps running, as with the POSIX arm's EPERM: the
        # bounded re-read then expires without output, rather than the kill raising in its place.
        with contextlib.suppress(PermissionError):
            api.terminate(job)
        return
    if sys.platform == "win32":
        # taskkill /T walks the descendant tree by parent PID; /F is unconditional. A bounded
        # capturing subprocess.run is safe HERE and only here: taskkill spawns nothing, so no
        # grandchild can hold its pipes -- the precondition the general case violates and this
        # whole module exists to handle.
        subprocess.run(  # reliability: ignore[RL005] leaf; pragma: only-on win32
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            timeout=KILL_TIMEOUT_SECONDS,
            check=False,
        )
    else:  # pragma: only-on posix
        # run_bounded and run_bounded_bytes start the child with start_new_session=True, so
        # its process-group id IS its pid and the group needs no lookup. os.getpgid must not
        # be used: macOS refuses it (ESRCH) for a leader that has exited but is not yet
        # reaped, which is exactly when a grandchild can still hold the pipes -- the lookup
        # raised, killed nothing, and the timeout surfaced as an OSError instead. A member the
        # kill cannot signal cannot hang the caller either: a survivor still holding the pipes
        # makes the bounded re-read expire, which raises without output.
        boundedrun_posix_tree.kill_tree_posix(pid, KILL_TIMEOUT_SECONDS)


def kill_live_trees() -> int:
    """Kill every running child's tree, for a caller's signal handler; the count killed."""
    return boundedrun_live.kill_each(kill_tree)


def _contain(proc: subprocess.Popen[_Stream], api: _JobApi) -> int:
    """Put the still-suspended *proc* in a new job, then let it run; return the job.

    Resuming first would let it start a grandchild outside the job. Any failure kills the child
    before re-raising: left suspended, it would sit out the timeout and be reported as a hang.
    """
    job: int | None = None
    try:
        job = api.create_job()
        api.assign(job, proc.pid)
        api.resume(proc.pid)
    except OSError:
        proc.kill()
        if job is not None:
            api.close(job)
        raise
    return job


def _start_contained(proc: subprocess.Popen[_Stream]) -> None:
    """Register a job for *proc* where needed; on POSIX its process group already is the tree."""
    if _job_api_factory is None:
        return
    api = _job_api_factory()
    _JOBS[proc.pid] = (api, _contain(proc, api))


def _release(pid: int) -> None:
    """Forget *pid*'s job and close its handle, if it has one."""
    registered = _JOBS.pop(pid, None)
    if registered is not None:
        api, job = registered
        api.close(job)


def _drain_contained(
    proc: subprocess.Popen[_Stream],
    argv: Sequence[str],
    timeout: float,
    kill_tree_fn: KillTreeFn,
    stdin_data: _Stream | None,
    clock: Clock | None = None,
) -> tuple[_Stream, _Stream]:
    """Contain the registered *proc*, drain it under the bound, and release its job after."""
    try:
        _start_contained(proc)
        return _communicate_bounded(
            proc, argv, timeout, kill_tree_fn, stdin_data, clock
        )
    finally:
        _release(proc.pid)


def _drainer(
    argv: Sequence[str],
    timeout: float,
    kill_tree_fn: KillTreeFn,
    stdin_data: _Stream | None,
    clock: Clock | None = None,
) -> Callable[[subprocess.Popen[_Stream]], tuple[_Stream, _Stream]]:
    """The drain the spawn runs on a child once it is registered (property 8)."""
    return lambda child: _drain_contained(
        child, argv, timeout, kill_tree_fn, stdin_data, clock
    )


def run_bounded(
    argv: Sequence[str],
    *,
    timeout: float,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    stdin_text: str | None = None,
    kill_tree_fn: KillTreeFn = kill_tree,
    clock: Clock | None = None,
    hide_window: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run *argv*, capturing text output, bounded by *timeout* with a real tree kill.

    *stdin_text* feeds the child on stdin; a pipe is opened only when it is given, and a caller
    that passes nothing gets ``DEVNULL`` -- an immediate EOF. Deliberately NOT inheritance: this
    run captures stdout AND stderr, so the child cannot be interactive anyway, and a child that
    READS stdin then eats the caller's own. `dnf` does exactly that, so a probe launched from a
    script delivered on stdin (``bash -s``, ``curl | bash``) silently swallowed the rest of that
    script and the shell exited 0. DEVNULL is also not the hazard inheritance was chosen to
    avoid -- a pipe nobody writes to would block; an EOF does not. Text rather than bytes because
    the callers that need it drive a shell hook whose transaction is text -- see
    :func:`run_bounded_bytes` for the binary case. A ``str`` or ``bytes`` *argv* is a TypeError.

    *clock*, when given, measures *timeout* on it instead of ``time.monotonic()``, which stops
    while a Mac sleeps: pass :func:`sleep_inclusive_clock` for a bound in wall time. The kill
    then lands within one 30 s slice of awake time after the deadline. It raises ValueError with
    non-empty *stdin_text*, before spawning: see ``refuse_clock_with_stdin`` for why.

    *hide_window* starts the child with no console window on Windows, and is ignored elsewhere.
    A console-subsystem child of a parent with no console (``pythonw.exe``) is otherwise given a
    new, visible one.

    Deliberately NOT used as a context manager: ``Popen.__exit__`` calls an unbounded ``wait()``,
    which would reintroduce the very hang this module removes on the failed-kill path.
    """
    refuse_bad_call(argv, clock, stdin_text)
    proc, (stdout, stderr) = boundedrun_spawn.run_text(
        argv,
        cwd=cwd,
        env=env,
        piped_stdin=stdin_text is not None,
        creationflags=spawn_creationflags(
            platform=sys.platform, hide_window=hide_window
        ),
        encoding=stdio_utf8.ENCODING,
        errors=stdio_utf8.CAPTURE_ERRORS,
        drain=_drainer(argv, timeout, kill_tree_fn, stdin_text, clock),
        kill_tree_fn=kill_tree_fn,
    )
    return subprocess.CompletedProcess(list(argv), proc.returncode, stdout, stderr)


def run_bounded_bytes(
    argv: Sequence[str],
    *,
    timeout: float,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    stdin_bytes: bytes | None = None,
    kill_tree_fn: KillTreeFn = kill_tree,
    clock: Clock | None = None,
    hide_window: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    """Binary-mode sibling of :func:`run_bounded`, for a caller that feeds and reads raw bytes.

    Git run in binary mode with piped stdin is one such caller. Kept a
    separate named function rather than a ``text: bool`` switch so each has one reason to exist
    and mypy needs no ``@overload``. Its no-stdin arm is ``DEVNULL``, and *clock* means
    what it does there, and so does *hide_window*, for the same reasons as :func:`run_bounded`'s.
    """
    refuse_bad_call(argv, clock, stdin_bytes)
    proc, (stdout, stderr) = boundedrun_spawn.run_bytes(
        argv,
        cwd=cwd,
        env=env,
        piped_stdin=stdin_bytes is not None,
        creationflags=spawn_creationflags(
            platform=sys.platform, hide_window=hide_window
        ),
        drain=_drainer(argv, timeout, kill_tree_fn, stdin_bytes, clock),
        kill_tree_fn=kill_tree_fn,
    )
    return subprocess.CompletedProcess(list(argv), proc.returncode, stdout, stderr)
