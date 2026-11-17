"""Install, uninstall and read a user LaunchAgent, each step confirmed by launchd's own answer.

Why this exists
---------------
Every scheduled job in the workspace that runs under launchd repeats one sequence: write the
plist, boot out any loaded copy, wait until launchd says it is gone, bootstrap the new one, and
wait until launchd says it is loaded. This module is that sequence written once. It decides
nothing about what an agent runs: the caller hands it a label, a plist path and the plist bytes.

``launchctl print`` has three answers, not two: exit 0 is loaded, 113 is absent, and anything
else (a timeout and a missing launchctl included) is unknown. An unknown answer ends a wait at
once with :class:`LaunchdError`; it is never taken for either settled answer, and polling it
would only hide its exit code behind the deadline.

``bootout`` is not instantaneous, so install waits for absent before bootstrapping the same
label, and then waits for loaded whatever bootstrap exited: a bootstrap racing the old
instance's teardown can exit non-zero while launchd goes on to load the agent, so the read-back,
not the exit code, is the confirmation. The plist is written before anything is unloaded, so a
failure never leaves the old agent gone with no plist to retry from. Uninstall removes the plist
only once launchd confirms the agent absent: until then the plist is what a retry needs.

Entry point assumptions
-----------------------
Import-only: no CLI and no output. Every launchctl call goes through the :class:`Launchctl`
handed in, so a test scripts launchd and never reaches it; :func:`real_launchctl` is the one
host seam. The shared libraries it imports are found by a bounded upward search from this
file, which looks for each library in its own folder and its parent.
"""

from __future__ import annotations

import enum
import os
import plistlib
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, Union

#: The shared libraries this module reaches: the atomic file replacement, the bounded
#: subprocess runner, and the scheduler artifacts whose label rule a plist must pass.
_LIBRARIES: Final = ("atomic_write", "bounded_run", "os_schedule_gen")
#: How many of the starting file's ancestors the search reads: its own folder and its
#: parent. Each library is a sibling of this module's folder in both layouts it ships in,
#: ``<repo>/utilities/<name>`` and an export's ``<root>/<name>``, so the parent finds it
#: in either. Stopping there means that in an export the search never reads above the
#: export root, where a same-named folder beside a clone would be imported.
_ANCESTORS_SEARCHED: Final = 2


def _nearest_library(name: str, start: Path) -> Path | None:
    """The nearest folder above *start* holding ``<name>.py``, if any.

    Each ancestor is tried as ``<ancestor>/<name>``, and only the first
    ``_ANCESTORS_SEARCHED`` ancestors are read: its own folder and its parent.
    """
    for ancestor in list(start.parents)[:_ANCESTORS_SEARCHED]:
        folder = ancestor / name
        if (folder / f"{name}.py").is_file():
            return folder
    return None


sys.path += [
    str(folder)
    for name in _LIBRARIES
    for folder in [_nearest_library(name, Path(__file__).resolve())]
    if folder is not None
]

import atomic_write  # noqa: E402 - import follows the sys.path bootstrap above
import os_schedule_gen  # noqa: E402 - same bootstrap
from bounded_run import run_bounded  # noqa: E402 - same bootstrap

#: The launchd control tool, by absolute path so an agent's minimal PATH cannot shadow it.
LAUNCHCTL: Final = "/bin/launchctl"
#: ``launchctl print``'s exit for a service its domain does not hold.
PRINT_ABSENT_RC: Final = 113
#: How long one wait for launchd to settle may take before the operation fails.
DEFAULT_DEADLINE_S: Final = 30.0
#: The pause between two reads of an agent's state while waiting.
POLL_S: Final = 0.5
#: The bound on any one launchctl call.
LAUNCHCTL_TIMEOUT_S: Final = 10.0
#: The pause launchd keeps between two starts of a kept-alive agent that keeps exiting.
_THROTTLE_S: Final = 10
#: An agent's restart policy, the plist's ``KeepAlive`` value: ``True`` restarts it after
#: every exit, and a dictionary of launchd's conditions restarts it only when they hold.
KeepAlivePolicy = Union[bool, Mapping[str, bool]]
#: Restart after every exit, a clean one included: for a server that should never stop.
KEEP_ALIVE_ALWAYS: Final[KeepAlivePolicy] = True
#: Restart only after a nonzero exit or a crash, so an agent that exits 0 stays stopped
#: until its next load: for one that ends a refusal no restart can cure that way.
RESTART_AFTER_FAILURE: Final[Mapping[str, bool]] = MappingProxyType(
    {"SuccessfulExit": False}
)
#: The plist mode: readable by launchd and the owner's tools, writable by the owner only.
_PLIST_MODE: Final = 0o644

#: Runs one argv under a timeout in seconds and returns the completed process.
RunCmd = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]


class AgentState(enum.Enum):
    """What one ``launchctl print`` says about an agent."""

    LOADED = "loaded"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class LaunchdError(Exception):
    """launchd did not settle in the wanted state, or answered unknown."""


@dataclass(frozen=True)
class Launchctl:
    """The host facts every step uses: how to run launchctl, the uid, and time."""

    #: Runs an argv under a timeout in seconds; raises ``subprocess.TimeoutExpired`` on expiry.
    run: RunCmd
    #: The uid whose GUI domain the agent is loaded into.
    uid: int
    clock: Callable[[], float]
    sleep: Callable[[float], None]


@dataclass(frozen=True)
class ServicePrint:
    """One ``launchctl print``: its state, its stdout when loaded, and why when unknown."""

    state: AgentState
    #: The print's stdout when the agent is loaded, and ``""`` for any other state.
    stdout: str
    #: Why the state is unknown, and ``""`` for a settled state.
    why: str


def service_target(uid: int, label: str) -> str:
    """The agent's service target in *uid*'s GUI domain: ``gui/<uid>/<label>``.

    Raises ValueError for a label os_schedule_gen would refuse as a plist file name, the rule
    :func:`build_keepalive_plist` applies: a ``/`` would name another target, and a leading
    ``-`` would read as an option. Every verb here builds its target through this, so each
    refuses such a label before launchctl runs.
    """
    # The plist path is computed only for the check it makes, as build_keepalive_plist does.
    os_schedule_gen.launchd_plist_path(Path(), label)
    return f"{_domain(uid)}/{label}"


def _domain(uid: int) -> str:
    """*uid*'s GUI domain, which bootstrap loads a plist into."""
    return f"gui/{uid}"


def kickstart_argv(uid: int, label: str) -> list[str]:
    """The command that starts the agent now, as launchd would; this module never runs it."""
    return [LAUNCHCTL, "kickstart", service_target(uid, label)]


def print_service(ctl: Launchctl, label: str) -> ServicePrint:
    """One bounded ``launchctl print`` of *label*, kept whole.

    The stdout is kept only on exit 0, since only a loaded agent's print describes it; an
    unknown answer carries its reason: a timeout, a launchctl that cannot run, or the exit
    code with stderr's first line.
    """
    argv = [LAUNCHCTL, "print", service_target(ctl.uid, label)]
    try:
        done = ctl.run(argv, LAUNCHCTL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return ServicePrint(
            AgentState.UNKNOWN, "", f"timed out after {LAUNCHCTL_TIMEOUT_S:g} s"
        )
    except OSError as error:
        # A missing or unrunnable launchctl is an unknown answer too, never a settled one.
        return ServicePrint(AgentState.UNKNOWN, "", f"could not run: {error}")
    if done.returncode == 0:
        return ServicePrint(AgentState.LOADED, done.stdout, "")
    if done.returncode == PRINT_ABSENT_RC:
        return ServicePrint(AgentState.ABSENT, "", "")
    lines = done.stderr.strip().splitlines()
    first = f": {lines[0]}" if lines else ""
    return ServicePrint(AgentState.UNKNOWN, "", f"exit {done.returncode}{first}")


def agent_state(ctl: Launchctl, label: str) -> AgentState:
    """Loaded, absent or unknown, from one ``launchctl print``."""
    return print_service(ctl, label).state


def wait_for(
    ctl: Launchctl,
    label: str,
    wanted: AgentState,
    deadline_s: float = DEFAULT_DEADLINE_S,
) -> None:
    """Poll every ``POLL_S`` until launchd reports *wanted*.

    Raises LaunchdError at once on an unknown answer, naming its exit code and stderr's first
    line, and at the deadline naming the state still read. Raises ValueError when *wanted* is
    unknown, which is not an answer to wait for.
    """
    if wanted is AgentState.UNKNOWN:
        raise ValueError("a wait needs loaded or absent, not unknown")
    deadline = ctl.clock() + deadline_s
    while True:
        reading = print_service(ctl, label)
        if reading.state is wanted:
            return
        if reading.state is AgentState.UNKNOWN:
            raise LaunchdError(
                f"{label}: launchctl print answered unknown ({reading.why})"
            )
        if ctl.clock() >= deadline:
            raise LaunchdError(
                f"{label}: still {reading.state.value} after {deadline_s:g} s"
            )
        ctl.sleep(POLL_S)


def _run_unchecked(
    ctl: Launchctl, argv: list[str], *, label: str
) -> subprocess.CompletedProcess[str] | None:
    """Run a launchctl verb whose exit is not the check: the read-back after it is.

    Returns the completed process, kept only to explain a read-back that fails, or None on a
    timeout, which is ignored for the same reason: the wait that follows reads the real state.
    Raises LaunchdError, chained to the OSError, when launchctl cannot be run at all: no
    read-back could follow, and a caller reports one LaunchdError rather than a traceback.
    It leads with *label*, as every LaunchdError does, since not every verb's argv names
    the agent (bootstrap's names only the domain and the plist).
    """
    try:
        return ctl.run(argv, LAUNCHCTL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return None
    except OSError as error:
        raise LaunchdError(
            f"{label}: could not run launchctl {argv[1]}: {error}"
        ) from error


def _bootstrap_failure(done: subprocess.CompletedProcess[str] | None) -> str | None:
    """What a non-zero bootstrap said, its exit and first output line, or None."""
    if done is None or done.returncode == 0:
        return None
    lines = (done.stderr.strip() or done.stdout.strip()).splitlines()
    first = lines[0] if lines else ""
    return f"launchctl bootstrap failed (exit {done.returncode}): {first}"


def install_agent(
    ctl: Launchctl,
    *,
    label: str,
    plist_path: Path,
    plist: bytes,
    deadline_s: float = DEFAULT_DEADLINE_S,
) -> None:
    """Write *plist*, replace any loaded copy of *label*, and confirm launchd loaded it.

    Raises ValueError for a label :func:`service_target` refuses, before anything is written;
    OSError when the plist cannot be written (nothing is unloaded then); and LaunchdError when
    launchd does not settle, when the plist stays written, so re-running install recovers.
    """
    target = service_target(ctl.uid, label)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write.replace_bytes(plist_path, plist, mode=_PLIST_MODE)
    # A first install has nothing to boot out, so its exit is ignored.
    _run_unchecked(ctl, [LAUNCHCTL, "bootout", target], label=label)
    wait_for(ctl, label, AgentState.ABSENT, deadline_s)
    # Bootstrap's exit codes are unreliable; the read-back below is the confirmation. A
    # non-zero exit is kept only to explain an agent that then never loads.
    done = _run_unchecked(
        ctl, [LAUNCHCTL, "bootstrap", _domain(ctl.uid), str(plist_path)], label=label
    )
    try:
        wait_for(ctl, label, AgentState.LOADED, deadline_s)
    except LaunchdError as error:
        failure = _bootstrap_failure(done)
        if failure is None:
            raise
        raise LaunchdError(f"{error}; {failure}") from error


def uninstall_agent(
    ctl: Launchctl,
    *,
    label: str,
    plist_path: Path,
    deadline_s: float = DEFAULT_DEADLINE_S,
) -> None:
    """Unload *label*, and remove its plist only once launchd confirms the agent gone.

    An absent plist is not an error. Raises LaunchdError, keeping the plist for a retry, when
    launchd still reports the agent or answers unknown.
    """
    stop_agent(ctl, label=label, deadline_s=deadline_s)
    plist_path.unlink(missing_ok=True)


def stop_agent(
    ctl: Launchctl, *, label: str, deadline_s: float = DEFAULT_DEADLINE_S
) -> None:
    """Unload *label* and wait until launchd confirms it gone, leaving its plist in place.

    For a caller that replaces what the agent runs before loading it again: the plist stays,
    so a failure between the stop and the next load still reads as an installed agent.
    Raises LaunchdError when launchd still reports the agent or answers unknown.
    """
    # An agent that was never loaded makes bootout fail; the wait is the check.
    _run_unchecked(
        ctl, [LAUNCHCTL, "bootout", service_target(ctl.uid, label)], label=label
    )
    wait_for(ctl, label, AgentState.ABSENT, deadline_s)


def build_keepalive_plist(
    *,
    label: str,
    program_arguments: Sequence[str],
    working_directory: str,
    log_paths: tuple[str, str],
    path_value: str,
    keep_alive: KeepAlivePolicy = KEEP_ALIVE_ALWAYS,
) -> bytes:
    """The plist of an agent launchd starts at load and restarts as *keep_alive* says.

    *log_paths* is stdout's file then stderr's; launchd opens them at spawn and does not create
    their folder. *path_value* is the agent's whole PATH, since launchd's own is minimal.
    *keep_alive* is the restart policy: after every exit by default, or a dictionary of
    launchd's conditions such as `RESTART_AFTER_FAILURE`. Raises ValueError for a label
    os_schedule_gen would refuse as a plist file name.
    """
    os_schedule_gen.launchd_plist_path(Path(), label)
    stdout_path, stderr_path = log_paths
    return plistlib.dumps(
        {
            "Label": label,
            "ProgramArguments": list(program_arguments),
            "WorkingDirectory": working_directory,
            "RunAtLoad": True,
            # plistlib writes a dict, not a read-only view of one.
            "KeepAlive": (
                keep_alive if isinstance(keep_alive, bool) else dict(keep_alive)
            ),
            "ThrottleInterval": _THROTTLE_S,
            "ProcessType": "Background",
            "StandardOutPath": stdout_path,
            "StandardErrorPath": stderr_path,
            "EnvironmentVariables": {"PATH": path_value},
        }
    )


def real_launchctl() -> Launchctl:  # pragma: no cover - real host seam
    """The Launchctl of this process: the real launchctl, its own uid, and real time."""
    if sys.platform == "win32":
        raise LaunchdError("launchd exists only on macOS")
    return Launchctl(
        run=lambda argv, timeout: run_bounded(argv, timeout=timeout),
        uid=os.getuid(),
        clock=time.monotonic,
        sleep=time.sleep,
    )
