"""Install and uninstall the daily LaunchAgent, reading its state back from launchd.

The plist comes from schedule_artifacts, the shared scheduler-artifact generator, with this
agent's label, ``--mode run --if-due``, the destination install resolved, and ``RunAtLoad``.
Pinning the destination means a later edit to the configuration cannot move where the agent
writes; what it backs up is still read from the configuration on every run. No calendar firing
survives a power-off, only a sleep, so the load-time firing covers a day the Mac was off at the
scheduled time, and ``--if-due`` makes that firing a no-op on a day already covered. Every host
fact the plist bakes in -- the interpreter, the script, git's location, the uid, the destination
-- arrives in an AgentSpec; this module reads none itself.

``launchctl print`` has three answers, not two: exit 0 is loaded, 113 is absent, and anything
else -- a timeout and a missing launchctl included -- is unknown. An unknown answer ends a wait
at once and fails the operation; it is never taken for either settled answer.

``bootout`` is not instantaneous, so install polls for "absent" before bootstrapping the same
label, then polls for "loaded" to confirm -- whatever bootstrap exited, since one racing the old
instance's teardown can fail while launchd goes on to load the agent. The plist is written
before anything is unloaded, so no failure leaves the machine with the old agent gone and no
plist; each failure to load says so, and that re-running install recovers. Uninstall removes
the plist only after launchd confirms the agent absent: while it is still loaded, or its state
unknown, the plist is what a retry needs.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import sys
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TextIO

# schedule_artifacts, the shared scheduler-artifact renderer, at ONE fixed place: its folder
# directly under the repository root, as backup_proc.py places bounded_run.
sys.path.append(str(Path(__file__).resolve().parents[2] / "schedule_artifacts"))

import schedule_artifacts  # noqa: E402 - import follows the sys.path bootstrap above

from backup_agent_args import (  # noqa: E402 - same bootstrap block
    AgentArgsError,
    InstalledAgent,
    read_installed_agent,
    removal_report,
)
from backup_fs import atomic_write_bytes, make_private_dirs  # noqa: E402 - same
from backup_proc import LAUNCHCTL, LAUNCHCTL_TIMEOUT_S, RunCmd  # noqa: E402 - same
from backup_run import EXIT_OK, EXIT_UNRUNNABLE  # noqa: E402 - same bootstrap block
from backup_status import AGENT_ABSENT, AGENT_LOADED, AGENT_UNKNOWN  # noqa: E402 - same

#: The key launchd looks the agent up by, and so its plist's file name: the documented-contract
#: exception to embedding a name. ``local.`` rather than a real reverse-DNS domain.
LABEL = "local.workspace-backup.daily"
#: The daily run time install uses when none is given, as ``--time`` spells it.
DEFAULT_TIME = "06:00"
#: What every firing runs: a scheduled run, skipped on a day already covered.
AGENT_ARGS = ("--mode", "run", "--if-due")
#: ``launchctl print``'s exit for a service its domain does not hold.
PRINT_ABSENT_RC = 113
#: How long one wait for launchd to settle may take before the operation fails.
STATE_DEADLINE_S = 30.0
#: The pause between two reads of the agent's state while waiting.
POLL_INTERVAL_S = 0.5
#: Directories every agent PATH carries after git's own.
_BASE_PATH = ("/usr/bin", "/bin")
#: The states a wait can aim for. Unknown is never one: it is not an answer to wait for.
_SETTLED = frozenset({AGENT_LOADED, AGENT_ABSENT})
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
    schedule: schedule_artifacts.DailySchedule
    git_path: str
    uid: int
    #: The destination every firing names with ``--destination``, resolved at install.
    destination: Path
    #: The repository every firing names with ``--repo``; None names none, so the agent backs
    #: up the checkout its script sits in.
    repo: "Path | None" = None


@dataclass(frozen=True)
class Waiter:
    """The clock, sleep and deadline a wait for launchd to settle uses."""

    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    deadline_s: float = STATE_DEADLINE_S


@dataclass(frozen=True)
class AgentReading:
    """One ``launchctl print``: the state it means, its exit code, and its stderr."""

    state: str
    code: int
    detail: str


def plist_path(home: Path) -> Path:
    """Where this agent's plist lives under *home*."""
    return schedule_artifacts.launchd_plist_path(home, label=LABEL)


def baked_path(git_path: str) -> str:
    """The agent's PATH: git's directory, then the system directories, without repeats.

    An agent gets launchd's minimal environment rather than a login shell's, so this is what
    makes a scheduled run find the git install found.
    """
    git_dir = str(PurePosixPath(git_path).parent)
    return ":".join(dict.fromkeys((git_dir, *_BASE_PATH)))


def render_plist(spec: AgentSpec) -> bytes:
    """The plist bytes for *spec*: daily at its time and at load, every firing ``--if-due``."""
    return schedule_artifacts.build_launchd_plist(
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


def read_agent(uid: int, *, run: RunCmd) -> AgentReading:
    """One bounded ``launchctl print`` of the agent in *uid*'s GUI domain."""
    code, _, stderr = run(
        [LAUNCHCTL, "print", _service(uid)], None, LAUNCHCTL_TIMEOUT_S
    )
    if code == 0:
        state = AGENT_LOADED
    elif code == PRINT_ABSENT_RC:
        state = AGENT_ABSENT
    else:
        state = AGENT_UNKNOWN
    return AgentReading(state, code, stderr.strip())


def agent_state(uid: int, *, run: RunCmd) -> str:
    """Loaded, absent or unknown, from one ``launchctl print``."""
    return read_agent(uid, run=run).state


def wait_for_state(
    uid: int, wanted: str, *, run: RunCmd, waiter: Waiter
) -> AgentReading:
    """Poll until launchd reports *wanted*, and return the last reading.

    That reading is *wanted* once launchd confirms it, the other settled state if the deadline
    passes first, and unknown as soon as any read is. An unknown answer is not "not yet":
    polling it would hide its exit code behind a timeout, and for a hung launchctl repeat a
    whole call's bound on every poll. Raises ValueError when *wanted* is not loaded or absent.
    """
    if wanted not in _SETTLED:
        raise ValueError(f"a wait needs loaded or absent, got {wanted!r}")
    deadline = waiter.clock() + waiter.deadline_s
    while True:
        reading = read_agent(uid, run=run)
        if reading.state in (wanted, AGENT_UNKNOWN) or waiter.clock() >= deadline:
            return reading
        waiter.sleep(POLL_INTERVAL_S)


def install(
    spec: AgentSpec,
    *,
    run: RunCmd,
    waiter: Waiter,
    dry_run: bool,
    out: TextIO,
    err: TextIO,
) -> int:
    """Write the plist, replace any loaded copy, and confirm launchd loaded it; the exit code.

    A dry run validates *spec* and prints the plan: it writes nothing and runs no launchctl
    verb. Exit 2 for an unusable *spec*, a plist that cannot be written, and any launchd step
    that fails, does not settle in time, or answers unknown.
    """
    problem = _spec_problem(spec)
    if problem is not None:
        return _fail(err, problem)
    data = render_plist(spec)
    target = plist_path(spec.home)
    if dry_run:
        _print_plan(spec, target, data, out)
        return EXIT_OK
    problem = _write_plist(spec, target, data)
    if problem is not None:
        return _fail(err, problem)
    problem = _load_agent(spec, target, run=run, waiter=waiter)
    if problem is not None:
        return _fail(err, f"{problem}; {_LEFT_BY_A_LOAD_FAILURE}")
    _print_installed(spec, data, out)
    return EXIT_OK


def uninstall(
    home: Path,
    uid: int,
    *,
    run: RunCmd,
    waiter: Waiter,
    out: TextIO,
    err: TextIO,
    named_repo: "Path | None" = None,
) -> int:
    """Unload the agent, and remove its plist only once launchd confirms it gone; the exit code.

    Once removed, says which repository the agent was backing up, read from its own plist
    before anything else runs, and when *named_repo* is another, says that too: there is one
    agent per account, so it is removed all the same. Snapshots and recorded state are never
    touched. Exit 2 when launchd still reports the agent or answers unknown -- the plist is
    kept for a retry -- or the plist cannot be removed.
    """
    target = plist_path(home)
    try:
        installed: "InstalledAgent | AgentArgsError | None" = read_installed_agent(
            target
        )
    except AgentArgsError as exc:
        installed = exc
    # Exit ignored: the agent may never have been loaded. The wait below is the check.
    run([LAUNCHCTL, "bootout", _service(uid)], None, LAUNCHCTL_TIMEOUT_S)
    gone = wait_for_state(uid, AGENT_ABSENT, run=run, waiter=waiter)
    if gone.state != AGENT_ABSENT:
        unsettled = _unsettled(gone, waiter, "the agent did not unload")
        return _fail(err, f"{unsettled}; its plist is kept")
    try:
        target.unlink(missing_ok=True)
    except OSError as exc:
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


def _write_plist(spec: AgentSpec, target: Path, data: bytes) -> "str | None":
    """Create the log directory, then write the plist atomically; what failed, or None."""
    # launchd opens the log redirects at spawn and does not create their directory.
    logs = schedule_artifacts.logs_dir(spec.state_base)
    try:
        make_private_dirs(logs)
        atomic_write_bytes(target, data)
    except OSError as exc:
        return f"could not create {logs} or write {target}: {exc}"
    return None


def _load_agent(
    spec: AgentSpec, target: Path, *, run: RunCmd, waiter: Waiter
) -> "str | None":
    """Boot out any loaded copy, wait for absent, bootstrap, wait for loaded; what failed."""
    # Exit ignored: a first install has nothing to boot out. The wait below is the check.
    run([LAUNCHCTL, "bootout", _service(spec.uid)], None, LAUNCHCTL_TIMEOUT_S)
    gone = wait_for_state(spec.uid, AGENT_ABSENT, run=run, waiter=waiter)
    if gone.state != AGENT_ABSENT:
        what = "after bootout, launchd did not report the agent absent"
        return _unsettled(gone, waiter, what)
    code, stdout, stderr = run(
        [LAUNCHCTL, "bootstrap", _domain(spec.uid), str(target)],
        None,
        LAUNCHCTL_TIMEOUT_S,
    )
    # Not the check either: bootstrapping a label whose old instance is still tearing down
    # can exit non-zero (an I/O error) although print already said absent, so launchd's own
    # polled answer decides, as it does after a bootstrap that exited 0.
    loaded = wait_for_state(spec.uid, AGENT_LOADED, run=run, waiter=waiter)
    if loaded.state == AGENT_LOADED:
        return None
    what = "the agent was bootstrapped but did not load"
    if code != 0:
        failure = (stderr or stdout).strip()
        what = f"launchctl bootstrap failed (exit {code}): {failure}, and the agent did not load"
    return _unsettled(loaded, waiter, what)


def _unsettled(reading: AgentReading, waiter: Waiter, what: str) -> str:
    """Why a wait ended without its state: an unknown answer, or the deadline."""
    if reading.state == AGENT_UNKNOWN:
        return (
            f"{what}: launchctl print exited {reading.code}, which is neither loaded (0) "
            f"nor absent ({PRINT_ABSENT_RC}): {reading.detail}"
        )
    return f"{what}: it is still {reading.state} after {waiter.deadline_s:g} s"


def _print_plan(spec: AgentSpec, target: Path, data: bytes, out: TextIO) -> None:
    """What an install would write and run, for a dry run."""
    logs = schedule_artifacts.logs_dir(spec.state_base)
    print(f"would create {logs} and write {target}:", file=out)
    print(data.decode("utf-8"), file=out)
    print("would run, waiting for launchd to confirm each:", file=out)
    print(f"  {shlex.join([LAUNCHCTL, 'bootout', _service(spec.uid)])}", file=out)
    bootstrap = [LAUNCHCTL, "bootstrap", _domain(spec.uid), str(target)]
    print(f"  {shlex.join(bootstrap)}", file=out)


def _print_installed(spec: AgentSpec, data: bytes, out: TextIO) -> None:
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
        f"  {shlex.join([LAUNCHCTL, 'print', _service(spec.uid)])}",
    )
    print("\n".join(lines), file=out)


def _domain(uid: int) -> str:
    """*uid*'s GUI domain, where a user LaunchAgent is bootstrapped."""
    return f"gui/{uid}"


def _service(uid: int) -> str:
    """This agent's service target in *uid*'s GUI domain."""
    return f"{_domain(uid)}/{LABEL}"


def _fail(err: TextIO, problem: str) -> int:
    """Report *problem* on *err*; the could-not-run exit code."""
    print(f"ERROR: {problem}", file=err)
    return EXIT_UNRUNNABLE
