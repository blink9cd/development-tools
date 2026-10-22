#!/usr/bin/env python3
"""Back the workspace up to iCloud Drive, daily: git bundles plus a whitelisted zip.

Every snapshot is verified by restoring it, pruned only through a per-destination ledger,
and confirmed uploaded by ``--mode status``. A handful of large files per snapshot is the
shape iCloud syncs best. Rejected: restic, whose password is a second secret that must itself
be backed up, and whose restore needs restic installed; an rsync mirror of ``.git``, thousands
of churning small files, whose hardlinked snapshots iCloud uploads in full; and a bare remote
in iCloud Drive, the same small files, covering no ignored file.

Entry point assumptions: macOS, the standard library alone, run from the primary checkout
unless ``--repo`` names one. The private modules live in ``src/`` and are importable by plain
name once the bootstrap below has run.

Every process-level fact main() reads -- home, clock, PATH lookup, uid, platform, streams,
the command runner and launchd's waiter -- arrives in one :class:`Host`, and only the
``__main__`` guard builds the real one. main() has no default Host, so no caller can reach
this machine's launchd, iCloud state or memory corpora by leaving an argument out.

An option the chosen mode does not consume is refused, never ignored: ``uninstall
--dry-run`` would otherwise uninstall, and ``install --keep`` would install an agent that goes
on reading the configured keep. The configuration is the agent's source for everything but its
destination, which install pins so a later edit cannot move it. Uninstall reads neither the
checkout nor the configuration: an agent is no less worth removing because either is broken,
and that is when someone most wants it gone.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TextIO

_HERE = Path(__file__).resolve().parent
# The shared libraries, at ONE fixed place each: their folders directly under the repository
# root, this folder's parent. Appended, so none can shadow src/ or the standard library; a
# search of the parent directories would also take a same-named folder above the clone.
sys.path += [
    str(_HERE.parent / _lib)
    for _lib in ("stdio_utf8", "help_formatter", "schedule_artifacts")
]
sys.path.insert(0, str(_HERE / "src"))

import help_formatter  # noqa: E402 - import follows the sys.path bootstrap above
import schedule_artifacts  # noqa: E402 - same bootstrap
import stdio_utf8  # noqa: E402 - same bootstrap

import backup_launchd  # noqa: E402 - a private src/ module; same bootstrap
import backup_notify  # noqa: E402 - same bootstrap
import backup_status  # noqa: E402 - same bootstrap
from backup_checkout import (  # noqa: E402 - same bootstrap
    Refused as _Refused,
    checkout_and_config,
    destination_override,
    in_linked_worktree,
    named_repo,
    repo_start,
)
from backup_agent_args import pinned_destination_problems  # noqa: E402 - same
from backup_config import BackupConfig  # noqa: E402 - same bootstrap
from backup_ledger import LedgerError, state_dir_for  # noqa: E402 - same bootstrap
from backup_machine import MachineError, read_identity  # noqa: E402 - same bootstrap
from backup_model import STATE_BASE_NAME  # noqa: E402 - same bootstrap
from backup_proc import RunCmd, run_cmd  # noqa: E402 - same bootstrap
from backup_roots import Roots  # noqa: E402 - same bootstrap
from backup_build import memory_tools_dir  # noqa: E402 - same bootstrap
from backup_preflight import (  # noqa: E402 - same bootstrap
    effective_uid,
    refuse_during_a_run,
    refuse_unsafe_state_base,
    secure_state_base,
)
from backup_run import (  # noqa: E402 - same bootstrap
    DARWIN,
    EXIT_OK,
    EXIT_UNRUNNABLE,
    GIT,
    RunDeps,
    RunRequest,
    run_backup,
)

#: What install, and its dry run, say last: the label is fixed, so there is one agent.
BACKS_UP = (
    "The agent backs up {repo}. There is one agent per account: installing for another "
    "repository replaces it."
)
#: Where --help says the agent's logs are: a home token, never this machine's home.
_LOGS_SHOWN = schedule_artifacts.logs_dir(Path("$HOME", STATE_BASE_NAME))
#: The companion doc --help points at for the full exit and problem lists: named for this
#: script, so the pointer follows a rename.
_COMPANION_DOC = Path(__file__).with_suffix(".md").name

EPILOG = (
    "Prerequisites: macOS; git on PATH; a [tool.workspace_backup] table in the nearest "
    "pyproject.toml at or above the primary checkout, naming a destination whose parent "
    "folder exists (for iCloud Drive: signed in, with Drive turned on). "
    "Without --repo, the repository backed up is the checkout this script sits in. "
    "Run order: (1) --mode run --dry-run prints the plan and writes nothing; (2) --mode "
    "install loads the daily agent, its script in a primary checkout or, with --repo, "
    "anywhere outside a linked worktree; loading it starts a run "
    "unless today already has a complete snapshot; (3) --mode status, once that run has "
    "finished. Only a launchd-started run that takes a snapshot shows launchd can run the "
    "backup, and one that fails says why in its error log under "
    f"{_LOGS_SHOWN}. There is one agent per account: installing for another repository "
    "replaces it. --mode uninstall removes the agent and keeps every snapshot and all "
    "recorded state. Each option's help names the modes that use it; any other mode refuses "
    "it rather than ignoring it. "
    "Exit codes: 0 = done, as asked. 1 = needs attention: the run, or the destination "
    "status judged, has a problem, which is printed. 2 = could not run or judge. "
    f"{_COMPANION_DOC}, beside this script, lists what each mode exits 1 and 2 for, and "
    "every problem status reports."
)


@dataclass(frozen=True)
class Host:
    """Every process-level fact main() reads; :func:`real_host` builds this process's."""

    home: Path
    #: Where the primary checkout is looked for without --repo: the script's own folder, for
    #: a real run.
    start: Path
    #: The CLI the installed agent runs.
    script: Path
    python: str
    platform: str
    uid: int
    #: The effective uid, which must own the tool's directory under the home.
    euid: int
    run: RunCmd
    #: Naive local time, the basis snapshot names record.
    now: Callable[[], datetime]
    which: Callable[[str], "str | None"]
    #: Read only to derive this machine's identity before its first run keeps one.
    hostname: Callable[[], str]
    waiter: backup_launchd.Waiter
    out: TextIO
    err: TextIO


def real_host() -> Host:
    """The Host for this process: home, script, interpreter, platform, uid, host name, streams."""
    script = Path(__file__).resolve()
    return Host(
        home=Path.home(),
        start=script.parent,
        script=script,
        python=sys.executable,
        platform=sys.platform,
        uid=backup_launchd.process_uid(),
        euid=effective_uid(),
        run=run_cmd,
        now=datetime.now,
        which=shutil.which,
        hostname=socket.gethostname,
        waiter=backup_launchd.Waiter(),
        out=sys.stdout,
        err=sys.stderr,
    )


def build_parser() -> argparse.ArgumentParser:
    """The CLI's options: named only, every default rendered from its live value."""
    parser = argparse.ArgumentParser(
        prog=os.path.basename(sys.argv[0]),
        description=(
            "Back the workspace up to iCloud Drive: git bundles plus a whitelisted zip, "
            "verified by restoring, pruned through a ledger, and confirmed uploaded."
        ),
        formatter_class=help_formatter.ReqAwareDefaultsFormatter,
        epilog=EPILOG,
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=MODES,
        help="run = take one snapshot; install / uninstall = add or remove the daily "
        "LaunchAgent; status = judge the destination's health.",
    )
    parser.add_argument(
        "--if-due",
        action="store_true",
        help="run only, and not with --dry-run: do nothing when a complete snapshot was "
        "already taken today (what the agent runs).",
    )
    parser.add_argument(
        "--time",
        default=backup_launchd.DEFAULT_TIME,
        help="install only: the daily run time, as 24-hour HH:MM.",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=None,
        help="run and status: complete snapshots to retain, in place of the configured "
        "keep; the configured keep when unset, as by default (%(default)s).",
    )
    parser.add_argument(
        "--destination",
        default=None,
        help="run, status and install: the destination directory, absolute or starting "
        "with ~/, in place of the configured one; the configured destination when "
        "unset, as by default (%(default)s). "
        "Install pins whichever it is into the agent.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run and install only: print the plan and write nothing.",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="every mode: the repository to back up, absolute or starting with ~/; a "
        "linked worktree means its primary checkout, and install bakes it into the agent. "
        "When unset, as by default (%(default)s), the checkout this script sits in is "
        "backed up.",
    )
    return parser


def main(argv: "Sequence[str] | None", host: Host) -> int:
    """Parse *argv* (None: this process's own) and run one mode on *host*; the exit code."""
    stdio_utf8.force_utf8_streams(sys.stdout, sys.stderr)
    parser = build_parser()
    args = parser.parse_args(argv)
    unconsumed = _unconsumed_options(parser, args)
    if unconsumed:
        row = _consumer_row(args)
        return _refuse(host, f"--mode {row} does not use {', '.join(unconsumed)}")
    if host.platform != DARWIN:
        return _refuse(host, f"macOS only; this platform is {host.platform}")
    try:
        return _HANDLERS[args.mode](args, host)
    except _Refused as exc:
        return _refuse(host, str(exc))


def _consumer_row(args: argparse.Namespace) -> str:
    """The CONSUMED_OPTIONS row *args* is checked against: its mode, or the plan's row."""
    if args.mode == _RUN_MODE and args.dry_run:
        return RUN_PLAN_ROW
    return str(args.mode)


def _unconsumed_options(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> list[str]:
    """Each option given a value other than its default that its row does not consume.

    Compared with the default rather than tracked as "given", so an explicit value equal to
    the default -- which changes nothing -- passes.
    """
    consumed = CONSUMED_OPTIONS[_consumer_row(args)]
    return [
        action.option_strings[0]
        for action in _checkable_actions(parser)
        if action.dest not in consumed
        and getattr(args, action.dest) != parser.get_default(action.dest)
    ]


def _checkable_actions(parser: argparse.ArgumentParser) -> list[argparse.Action]:
    """Every option *parser* defines but the unchecked ones, read from the parser itself.

    Read rather than listed, so an option added later is checked too, and refused by every
    row until one names it. argparse offers no public list of a parser's actions;
    ``_actions`` is where every ``add_argument`` has put them since argparse joined the
    standard library.
    """
    return [action for action in parser._actions if action.dest not in _UNCHECKED_DESTS]


def _run_mode(args: argparse.Namespace, host: Host) -> int:
    """One snapshot, or its plan under --dry-run; run_backup's exit code, unchanged."""
    _guard_state_base(host, create=not args.dry_run)
    roots, config = _configured(args, host)
    request = RunRequest(
        roots=roots,
        config=config,
        base=_state_base(host),
        tools_dir=memory_tools_dir(host.script),
        python=host.python,
        if_due=args.if_due,
        dry_run=args.dry_run,
        platform=host.platform,
    )
    deps = RunDeps(
        run=host.run,
        now=host.now,
        which=host.which,
        notify=lambda title, message: backup_notify.notify(
            title, message, run=host.run
        ),
        hostname=host.hostname,
        out=host.out,
        err=host.err,
    )
    return run_backup(request, deps)


def _install_mode(args: argparse.Namespace, host: Host) -> int:
    """Install the daily agent for *host*; backup_launchd.install's exit code, unchanged.

    Every refusal comes before anything is written: a --time that is not HH:MM, a checkout or
    configuration the agent's runs could not use, git missing from PATH, a script inside a
    linked worktree, which a worktree cleanup can later delete from under the agent, and a run
    in progress. A dry run refuses as a real install does. With --repo, the worktree check is
    of the script's own folder, which then need not be in any checkout, and the agent is given
    the resolved primary checkout. The destination, --destination's or the configured one, is
    pinned into the agent either way.
    """
    schedule = schedule_artifacts.parse_daily_time(args.time)
    if schedule is None:
        raise _Refused(f"--time must be a 24-hour HH:MM clock time, got {args.time!r}")
    _guard_state_base(host, create=not args.dry_run)
    roots, config = _configured(args, host)
    git_path = host.which(GIT)
    if git_path is None:
        raise _Refused("git is not on PATH")
    named = args.repo is not None
    if in_linked_worktree(host.start, run=host.run, outside_ok=named):
        raise _Refused(
            "install from a script outside a linked worktree: the script's folder "
            f"{host.start} is in one, which a worktree cleanup can later delete from "
            "under the agent"
        )
    refuse_during_a_run(_state_base(host), host.err)
    spec = backup_launchd.AgentSpec(
        home=host.home,
        state_base=_state_base(host),
        python=host.python,
        script=host.script,
        schedule=schedule,
        git_path=git_path,
        uid=host.uid,
        destination=config.destination,
        repo=roots.workspace if named else None,
    )
    code = backup_launchd.install(
        spec,
        run=host.run,
        waiter=host.waiter,
        dry_run=args.dry_run,
        out=host.out,
        err=host.err,
    )
    if code == EXIT_OK:
        print(BACKS_UP.format(repo=roots.workspace), file=host.out)
    return code


def _uninstall_mode(args: argparse.Namespace, host: Host) -> int:
    """Remove the agent unless a backup run is in progress; uninstall's exit code, unchanged.

    --repo is checked only for being absolute, never against a checkout: it is compared with
    the repository the removed agent names, which is all uninstall reports about it.
    """
    named = named_repo(args.repo, home=host.home)
    refuse_during_a_run(_state_base(host), host.err, uninstalling=True)
    return backup_launchd.uninstall(
        host.home,
        host.uid,
        run=host.run,
        waiter=host.waiter,
        out=host.out,
        err=host.err,
        named_repo=named,
    )


def _status_mode(args: argparse.Namespace, host: Host) -> int:
    """Judge the destination; the verdict's code, its lines on stdout only when healthy."""
    _guard_state_base(host, create=False)
    _, config = _configured(args, host)
    try:
        facts = backup_status.gather_facts(
            destination=config.destination,
            state=state_dir_for(_state_base(host), config.destination),
            keep=config.keep,
            agent=_agent_state(host),
            now=host.now(),
            home=host.home,
            run=host.run,
            machine=read_identity(_state_base(host), host.hostname),
        )
    except (LedgerError, MachineError) as exc:
        # StateReadError included: a recorded state file that exists but cannot be read.
        raise _Refused(str(exc)) from exc
    pinned = pinned_destination_problems(
        backup_launchd.plist_path(host.home), config.destination, host.err
    )
    verdict = backup_status.judge(replace(facts, agent_problems=pinned))
    stream = host.out if verdict.code == backup_status.EXIT_HEALTHY else host.err
    for line in verdict.lines:
        print(line, file=stream)
    return verdict.code


def _configured(args: argparse.Namespace, host: Host) -> tuple[Roots, BackupConfig]:
    """The primary checkout and its configuration, with --keep and --destination applied.

    Refuses a --keep below one and a relative --destination before git runs, then whatever
    :func:`_checkout_and_config` refuses.
    """
    if args.keep is not None and args.keep < 1:
        raise _Refused(f"--keep must be a positive integer, got {args.keep}")
    destination = destination_override(args.destination, host.home)
    roots, config = _checkout_and_config(args, host)
    keep = config.keep if args.keep is None else args.keep
    if destination is None:
        destination = config.destination
    return roots, replace(config, keep=keep, destination=destination)


def _checkout_and_config(
    args: argparse.Namespace, host: Host
) -> tuple[Roots, BackupConfig]:
    """The primary checkout --repo names, else the one *host* starts in, and its configuration.

    Refuses an unusable --repo, a start outside any git checkout, and a missing or invalid
    configuration.
    """
    start = repo_start(args.repo, default=host.start, home=host.home)
    named = args.repo is not None
    return checkout_and_config(start, home=host.home, run=host.run, named=named)


def _agent_state(host: Host) -> str:
    """The agent's state as launchd reports it once its plist exists; absent while it does not.

    Without its plist the agent does not load at the next login, whatever launchd holds now.
    """
    if not backup_launchd.plist_path(host.home).exists():
        return backup_status.AGENT_ABSENT
    return backup_launchd.agent_state(host.uid, run=host.run)


def _guard_state_base(host: Host, *, create: bool) -> None:
    """Refuse an unsafe state base; with *create*, then make it, or keep it, owner-only."""
    if create:
        secure_state_base(_state_base(host), host.euid)
    else:
        refuse_unsafe_state_base(_state_base(host), host.euid)


def _state_base(host: Host) -> Path:
    """The tool's own directory under *host*'s home."""
    return host.home / STATE_BASE_NAME


def _refuse(host: Host, problem: str) -> int:
    """Report *problem* as one line on *host*'s stderr; the could-not-run exit code."""
    print(f"ERROR: {problem}", file=host.err)
    return EXIT_UNRUNNABLE


_RUN_MODE = "run"
_Handler = Callable[[argparse.Namespace, Host], int]
_HANDLERS: "dict[str, _Handler]" = {
    _RUN_MODE: _run_mode,
    "install": _install_mode,
    "uninstall": _uninstall_mode,
    "status": _status_mode,
}
#: The modes --mode accepts: the handler table's own keys, so neither can name one alone.
MODES = tuple(_HANDLERS)
#: The row a dry run of --mode run is checked against. The plan reads no --if-due, so on a
#: day already covered it would still print "would write"; refused, it says nothing false.
RUN_PLAN_ROW = f"{_RUN_MODE} --dry-run"
#: The option dests each row consumes; any other option given a non-default value is refused.
CONSUMED_OPTIONS: "dict[str, frozenset[str]]" = {
    # dry_run is named only because giving it selects RUN_PLAN_ROW instead of this row.
    _RUN_MODE: frozenset({"if_due", "keep", "destination", "dry_run", "repo"}),
    RUN_PLAN_ROW: frozenset({"dry_run", "keep", "destination", "repo"}),
    # The agent reads the configuration every morning, so install overrides none of it but
    # the destination, which it pins; --repo says where that configuration is, and is baked
    # into the agent too.
    "install": frozenset({"time", "dry_run", "repo", "destination"}),
    # The label is fixed, so the one agent goes whichever repository it backs up; --repo is
    # only compared with the one it was backing up.
    "uninstall": frozenset({"repo"}),
    "status": frozenset({"keep", "destination", "repo"}),
}
#: Parser dests no row names: --help exits while parsing, and --mode selects the row.
_UNCHECKED_DESTS = frozenset({"help", "mode"})


if __name__ == "__main__":  # pragma: no cover - entry point; tests drive main()
    sys.exit(main(None, real_host()))
