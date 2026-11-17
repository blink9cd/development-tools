"""Install and uninstall the daily LaunchAgent, reading its state back from launchd.

The plist comes from os_schedule_gen, the shared scheduler-artifact generator, with this
agent's label, ``--mode run --if-due``, the destination install resolved, and ``RunAtLoad``.
Pinning the destination means a later edit to the configuration cannot move where the agent
writes; what it backs up is still read from the configuration on every run. No calendar firing
survives a power-off, only a sleep, so the load-time firing covers a day the Mac was off at the
scheduled time, and ``--if-due`` makes that firing a no-op on a day already covered. Every host
fact the plist bakes in (the interpreter, the script, git's location, the destination)
arrives in an AgentSpec; this module reads none itself.

Every launchd step is the shared ``launchd_agent``'s, not this module's: its absolute
``/bin/launchctl``, its bound on each call, its waits, and success judged by launchd's own
answer to ``launchctl print``, so the tools that install an agent cannot drift apart on how.
It writes the plist before unloading anything and removes it only once launchd confirms the
agent absent. This module adds only what is this tool's: the spec's checks, the private log
folder, the plan and confirmation it prints, the removed agent's report, and the mapping of a
``LaunchdError`` to this tool's ``ERROR:`` line and exit code, with the recovery each failure
leaves. :func:`host_launchctl` builds the library's ``Launchctl`` from the tool's runner, so
every launchctl call still goes through it, and a test's runner refuses one it did not route.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import sys
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import TextIO

# os_schedule_gen, the shared scheduler-artifact renderer, and launchd_agent, the shared
# LaunchAgent install, uninstall and state read, each at one fixed place: its folder
# directly under the repository root, as backup_proc.py places bounded_run.
sys.path += [
    str(Path(__file__).resolve().parents[2] / lib)
    for lib in ("os_schedule_gen", "launchd_agent")
]

import launchd_agent  # noqa: E402 - import follows the sys.path bootstrap above
import os_schedule_gen  # noqa: E402 - same bootstrap

from backup_agent_args import (  # noqa: E402 - same bootstrap block
    AgentArgsError,
    InstalledAgent,
    read_installed_agent,
    removal_report,
)
from backup_fs import make_private_dirs  # noqa: E402 - same
from backup_proc import RunCmd  # noqa: E402 - same
from backup_run import EXIT_OK, EXIT_UNRUNNABLE  # noqa: E402 - same bootstrap block
from backup_status import AGENT_ABSENT, AGENT_LOADED, AGENT_UNKNOWN  # noqa: E402 - same
from launchd_agent import AgentState, Launchctl, LaunchdError  # noqa: E402 - same

#: The key launchd looks the agent up by, and so its plist's file name: the documented-contract
#: exception to embedding a name. ``local.`` rather than a real reverse-DNS domain.
LABEL = "local.workspace-backup.daily"
#: The daily run time install uses when none is given, as ``--time`` spells it.
DEFAULT_TIME = "06:00"
#: What every firing runs: a scheduled run, skipped on a day already covered.
AGENT_ARGS = ("--mode", "run", "--if-due")
#: Directories every agent PATH carries after git's own.
_BASE_PATH = ("/usr/bin", "/bin")
#: launchd_agent's reading of the agent, as the state status judges.
_STATUS_OF: Mapping[AgentState, str] = MappingProxyType(
    {
        AgentState.LOADED: AGENT_LOADED,
        AgentState.ABSENT: AGENT_ABSENT,
        AgentState.UNKNOWN: AGENT_UNKNOWN,
    }
)
#: The plist key naming the file launchd sends the agent's stderr to.
_ERROR_LOG_KEY = "StandardErrorPath"
#: The option pinning the destination into every firing.
_DESTINATION_OPTION = "--destination"
#: The Unicode category every refused path character is in: C0, DEL and C1 controls. plistlib
#: refuses the C0 ones itself, and DEL and C1 would reach the plist and the agent's arguments.
_CONTROL_CATEGORY = "Cc"
#: The uid a platform without one reports. Never used: every mode refuses that platform first.
_NO_UID = -1
#: What any failure to load leaves behind, and the way out, said after each such failure.
_LEFT_BY_A_LOAD_FAILURE = (
    "the plist is written, and an agent loaded before this install may already be "
    "unloaded; re-running install recovers"
)


# os.getuid is POSIX-only. The split is an ``if`` so a win32 type-check pass
# still sees the name; the win32 arm exists only for that pass.
if sys.platform == "win32":

    def process_uid() -> int:  # pragma: only-on win32
        """No uid: Windows has none, and every mode refuses the platform before using one."""
        return _NO_UID

else:  # pragma: only-on posix

    def process_uid() -> int:
        """This process's uid, whose GUI launchd domain the agent is loaded into."""
        return os.getuid()


@dataclass(frozen=True)
class AgentSpec:
    """What the installed agent runs, and where: every host fact the plist bakes in.

    Every path must be absolute: launchd starts the agent with no shell and no directory of
    the installer's to resolve a relative one against.
    """

    home: Path
    state_base: Path
    python: str
    script: Path
    schedule: os_schedule_gen.DailySchedule
    git_path: str
    #: The destination every firing names with ``--destination``, resolved at install.
    destination: Path
    #: The repository every firing names with ``--repo``; None names none, so the agent backs
    #: up the checkout its script sits in.
    repo: "Path | None" = None


@dataclass(frozen=True)
class Waiter:
    """The clock and sleep launchd_agent's waits use; their deadline is the library's own."""

    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


def plist_path(home: Path) -> Path:
    """Where this agent's plist lives under *home*."""
    return os_schedule_gen.launchd_plist_path(home, label=LABEL)


def baked_path(git_path: str) -> str:
    """The agent's PATH: git's directory, then the system directories, without repeats.

    An agent gets launchd's minimal environment rather than a login shell's, so this is what
    makes a scheduled run find the git install found.
    """
    git_dir = str(PurePosixPath(git_path).parent)
    return ":".join(dict.fromkeys((git_dir, *_BASE_PATH)))


def render_plist(spec: AgentSpec) -> bytes:
    """The plist bytes for *spec*: daily at its time and at load, every firing ``--if-due``."""
    return os_schedule_gen.build_launchd_plist(
        spec.schedule,
        spec.python,
        str(spec.script),
        spec.state_base,
        baked_path(spec.git_path),
        label=LABEL,
        args=agent_args(spec),
        run_at_load=True,
    )


def agent_args(spec: AgentSpec) -> "tuple[str, ...]":
    """What every firing of *spec*'s agent passes the script: AGENT_ARGS, then the pins."""
    return (*AGENT_ARGS, *_pins(spec))


def _pins(spec: AgentSpec) -> "tuple[str, ...]":
    """Any ``--repo``, then ``--destination``: what makes a command judge this agent's own."""
    repo = () if spec.repo is None else ("--repo", str(spec.repo))
    return (*repo, _DESTINATION_OPTION, str(spec.destination))


def host_launchctl(run: RunCmd, uid: int, waiter: Waiter) -> Launchctl:
    """The ``Launchctl`` launchd_agent is handed: the tool's *run*, *uid* and *waiter*.

    Each call goes through *run*, in no particular folder, so a test's runner, which refuses
    any launchctl it has not routed, holds for launchd as for every other command. The real
    runner never raises for a timeout or a launchctl that cannot start; it answers 124 or
    127, which launchd_agent reads as unknown on a print, the verdict its own would give.
    """

    def run_launchctl(
        argv: Sequence[str], timeout: float
    ) -> "subprocess.CompletedProcess[str]":
        code, stdout, stderr = run(argv, None, timeout)
        return subprocess.CompletedProcess(list(argv), code, stdout, stderr)

    return Launchctl(run=run_launchctl, uid=uid, clock=waiter.clock, sleep=waiter.sleep)


def agent_state(ctl: Launchctl) -> str:
    """Loaded, absent or unknown, from one ``launchctl print``: what status judges."""
    return _STATUS_OF[launchd_agent.agent_state(ctl, LABEL)]


def install(
    spec: AgentSpec,
    ctl: Launchctl,
    *,
    dry_run: bool,
    out: TextIO,
    err: TextIO,
) -> int:
    """Make the log folder, then have launchd_agent write the plist and load it; the exit code.

    A dry run validates *spec* and prints the plan: it writes nothing and runs no launchctl
    verb. Exit 2 for an unusable *spec*, a log folder or plist that cannot be written, and any
    ``LaunchdError``: a launchd step that does not settle in time or answers unknown, or a
    launchctl that cannot run, each reported on this tool's ``ERROR:`` line.
    """
    problem = _spec_problem(spec)
    if problem is not None:
        return _fail(err, problem)
    data = render_plist(spec)
    target = plist_path(spec.home)
    if dry_run:
        _print_plan(spec, ctl.uid, target, data, out)
        return EXIT_OK
    problem = _make_log_dir(spec)
    if problem is not None:
        return _fail(err, problem)
    try:
        launchd_agent.install_agent(ctl, label=LABEL, plist_path=target, plist=data)
    except LaunchdError as exc:
        return _fail(err, f"{exc}; {_LEFT_BY_A_LOAD_FAILURE}")
    except OSError as exc:
        # launchd_agent writes the plist before it runs any launchctl verb, and turns a
        # launchctl that cannot run into a LaunchdError, so this is the write alone.
        return _fail(err, f"could not write {target}: {exc}")
    _print_installed(spec, ctl.uid, data, out)
    return EXIT_OK


def uninstall(
    home: Path,
    ctl: Launchctl,
    *,
    out: TextIO,
    err: TextIO,
    named_repo: "Path | None" = None,
) -> int:
    """Have launchd_agent unload the agent, then remove its plist; the exit code.

    Once removed, says which repository the agent was backing up, read from its own plist
    before anything else runs, and when *named_repo* is another, says that too: there is one
    agent per account, so it is removed all the same. Snapshots and recorded state are never
    touched. Exit 2 when launchd still reports the agent or answers unknown (the plist is
    kept for a retry), or when the plist cannot be removed.
    """
    target = plist_path(home)
    try:
        installed: "InstalledAgent | AgentArgsError | None" = read_installed_agent(
            target
        )
    except AgentArgsError as exc:
        installed = exc
    try:
        launchd_agent.uninstall_agent(ctl, label=LABEL, plist_path=target)
    except LaunchdError as exc:
        return _fail(err, f"{exc}; its plist is kept")
    except OSError as exc:
        # Raised only by the removal: launchd_agent has already confirmed the agent gone.
        return _fail(
            err,
            f"the agent is unloaded, but could not remove {target}, so it loads again at "
            f"the next login: {exc}",
        )
    print(f"Uninstalled {LABEL}; snapshots and state are kept.", file=out)
    _report_removed(installed, target, named_repo, out=out, err=err)
    return EXIT_OK


def _report_removed(
    installed: "InstalledAgent | AgentArgsError | None",
    target: Path,
    named_repo: "Path | None",
    *,
    out: TextIO,
    err: TextIO,
) -> None:
    """Which repository the removed agent was backing up, or why that cannot be told."""
    if isinstance(installed, AgentArgsError):
        print(
            f"WARNING: cannot tell which repository the removed agent was backing up: "
            f"{installed}",
            file=err,
        )
    elif installed is None:
        print(f"Found no agent plist at {target}, so no repository to name.", file=out)
    else:
        print("\n".join(removal_report(installed, named_repo)), file=out)


def _spec_problem(spec: AgentSpec) -> "str | None":
    """Why *spec* cannot become a working agent, or None."""
    paths = {
        "home": str(spec.home),
        "state directory": str(spec.state_base),
        "Python": spec.python,
        "script": str(spec.script),
        "git": spec.git_path,
        "destination": str(spec.destination),
    }
    if spec.repo is not None:
        paths["repository"] = str(spec.repo)
    for name, value in paths.items():
        if any(unicodedata.category(char) == _CONTROL_CATEGORY for char in value):
            return f"the agent's {name} path holds a control character: {value!r}"
        if not PurePosixPath(value).is_absolute():
            return f"the agent's {name} path must be absolute, got {value!r}"
    if ":" in str(PurePosixPath(spec.git_path).parent):
        return (
            f"git's directory cannot go on PATH, which ':' separates: {spec.git_path!r}"
        )
    return None


def _make_log_dir(spec: AgentSpec) -> "str | None":
    """Create the log directory, private to this account; what failed, or None."""
    # launchd opens the log redirects at spawn and does not create their directory.
    logs = os_schedule_gen.logs_dir(spec.state_base)
    try:
        make_private_dirs(logs)
    except OSError as exc:
        return f"could not create {logs}: {exc}"
    return None


def _print_plan(
    spec: AgentSpec, uid: int, target: Path, data: bytes, out: TextIO
) -> None:
    """What an install would write and run, for a dry run: launchd_agent's two verbs."""
    logs = os_schedule_gen.logs_dir(spec.state_base)
    print(f"would create {logs} and write {target}:", file=out)
    print(data.decode("utf-8"), file=out)
    print("would run, waiting for launchd to confirm each:", file=out)
    bootout = [launchd_agent.LAUNCHCTL, "bootout", _service(uid)]
    print(f"  {shlex.join(bootout)}", file=out)
    bootstrap = [launchd_agent.LAUNCHCTL, "bootstrap", f"gui/{uid}", str(target)]
    print(f"  {shlex.join(bootstrap)}", file=out)


def _print_installed(spec: AgentSpec, uid: int, data: bytes, out: TextIO) -> None:
    """The confirmation, then which run proves the install and where its failure shows.

    Printed, never run. No kickstart is offered: every firing runs ``--if-due``, so on a day
    already covered a kickstart only records a skip, and a status of 0 after it proves nothing
    about a launchd-started run. The error log's path is read back from the plist *data*
    installed, so it is the path launchd writes rather than a second spelling of it.
    """
    error_log = str(plistlib.loads(data)[_ERROR_LOG_KEY])
    # The agent's own pins, so the command judges what this agent backs up, wherever it runs.
    status = [spec.python, str(spec.script), "--mode", "status", *_pins(spec)]
    lines = (
        f"Installed {LABEL}: daily at {spec.schedule.hhmm} and at login, each firing "
        f"{shlex.join(agent_args(spec))}.",
        "Prove it end to end with a run launchd itself starts: loading the agent has already "
        "started one unless today is covered, and on a covered day the next scheduled firing "
        "is that run. A kickstart proves nothing on a covered day, where a firing only records "
        "a skip.",
        "Once that run has finished, status exits 0 for a snapshot that is complete, verified "
        "and, in iCloud Drive, uploaded:",
        f"  {shlex.join(status)}",
        "A run that failed says why in the agent's error log, and launchd shows its \"last "
        'exit code" in launchctl print:',
        f"  {shlex.quote(error_log)}",
        f"  {shlex.join([launchd_agent.LAUNCHCTL, 'print', _service(uid)])}",
    )
    print("\n".join(lines), file=out)


def _service(uid: int) -> str:
    """This agent's service target in *uid*'s GUI domain."""
    return launchd_agent.service_target(uid, LABEL)


def _fail(err: TextIO, problem: str) -> int:
    """Report *problem* on *err*; the could-not-run exit code."""
    print(f"ERROR: {problem}", file=err)
    return EXIT_UNRUNNABLE
