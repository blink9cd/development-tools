#!/usr/bin/env python3
"""The CLI suites' shared case: a configured fixture tree, and a Host built only of fakes.

Deliberately not named ``test_*``, so the aggregator's discover skips it.

Every Host a test hands ``main()`` comes from :meth:`CliCase.host`: home, start and the
destination are temporary directories, and the uid, platform, clock, PATH lookup, host name
and launchd waiter are fakes. launchctl and osascript are always routed, so neither can reach
the host; memory_snapshot is answered by a route; and verification's scratch directory is
kept in the case's own temporary directory.
"""

from __future__ import annotations

import io
import itertools
import plistlib
import tempfile
import unittest
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest import mock

import _fixtures as fx

import backup_launchd
import backup_proc
import git_workspace_backup as wb
from backup_ledger import state_dir_for
from backup_preflight import effective_uid

#: The fixed clock every run and status reads: naive local time, as snapshot names are.
START = datetime(2026, 9, 14, 6, 0, 0)
#: The host name the fake host reports, and the machine label it derives: obvious fixtures.
FIXTURE_HOST = "Fixture-Mac.local"
MACHINE = "fixture-mac"
#: The snapshot name a run at START writes.
FIRST_NAME = f"2026-09-14T060000-{MACHINE}"
FIXTURE_UID = 501
FIXTURE_PYTHON = "/opt/fixture-python"
#: What the injected PATH lookup reports; a run itself executes whichever git is on PATH.
FIXTURE_GIT = "/opt/fixture-bin/git"
#: The fake waiter's deadline, in ticks of its clock: each read of the clock is one tick.
WAIT_DEADLINE_S = 3.0
#: What a fake memory_snapshot prints when told to fail.
MEMORY_FAILURE = "fixture memory failure"


class FakeLaunchctl:
    """Answers ``print`` from a queue, then with *settled*; every other verb succeeds."""

    def __init__(self) -> None:
        self.prints: list[int] = []
        self.settled = 0
        self.argvs: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> "tuple[int, str, str]":
        self.argvs.append(list(argv))
        if argv[1] != "print":
            return 0, "", ""
        code = self.prints.pop(0) if self.prints else self.settled
        return code, "", f"fixture print {code}" if code else ""


class FakeOsascript:
    """Answers the upload query with one fixed state per path, and records notifications."""

    def __init__(self) -> None:
        #: (ubiquitous, uploaded) for every path asked about.
        self.state = (False, False)
        self.notifications: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> "tuple[int, str, str]":
        items = fx.osascript_paths(argv)
        # The upload query names its script language; a notification is plain AppleScript.
        if "-l" in argv[: list(argv).index(backup_proc.END_OF_OPTIONS)]:
            return 0, fx.upload_states_json(*[self.state] * len(items)), ""
        self.notifications.append(items)
        return 0, "", ""


def _failing_memory(argv: Sequence[str]) -> "tuple[int, str, str]":
    """A memory_snapshot route that exits 1."""
    return 1, "", MEMORY_FAILURE


class CliCase(unittest.TestCase):
    """A configured fixture tree and a Host of fakes over it, all under one temp directory."""

    def setUp(self) -> None:
        self.enterContext(fx.isolated_git_env())
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.tree = fx.make_tree(self.tmp)
        self.destination = self.tmp / "dest"
        self.write_config(self.destination)
        scratch = self.tmp / "system-temp"
        scratch.mkdir()
        self.enterContext(mock.patch.object(tempfile, "tempdir", str(scratch)))
        self.launchctl = FakeLaunchctl()
        self.osascript = FakeOsascript()
        self.runner = self.fake_runner()
        self.out = io.StringIO()
        self.err = io.StringIO()

    def write_config(self, destination: Path) -> None:
        """Point the workspace's ``[tool.workspace_backup]`` at *destination*."""
        (self.tree.workspace / "pyproject.toml").write_text(
            "[tool.workspace_backup]\n"
            f'destination = "{destination}"\n'
            'extras = [".notes"]\n'
            'regenerable = ["cache", "new_folder"]\n'
            'claude_files = ["settings.json"]\n'
            'home_files = [".zshrc"]\n'
            "memory_snapshot = true\n",
            encoding="utf-8",
            newline="\n",
        )

    def fake_runner(
        self,
        *,
        memory_fails: bool = False,
        real: backup_proc.RunCmd = backup_proc.run_cmd,
    ) -> fx.FakeRunner:
        """A runner with launchctl, osascript and memory_snapshot always routed."""
        memory = _failing_memory if memory_fails else fx.MEMORY_OK["memory_snapshot.py"]
        routes: "dict[str, fx.Route]" = {
            "memory_snapshot.py": memory,
            "launchctl": self.launchctl,
            "osascript": self.osascript,
        }
        return fx.FakeRunner(routes, real=real)

    def host(self, runner: "fx.FakeRunner | None" = None) -> wb.Host:
        """A Host of fakes over the fixture tree; *runner* becomes ``self.runner``."""
        self.runner = self.fake_runner() if runner is None else runner
        ticks = itertools.count()
        waiter = backup_launchd.Waiter(
            clock=lambda: float(next(ticks)),
            sleep=lambda seconds: None,
            deadline_s=WAIT_DEADLINE_S,
        )
        return wb.Host(
            home=self.tree.home,
            start=self.tree.workspace,
            script=self.script,
            python=FIXTURE_PYTHON,
            platform="darwin",
            uid=FIXTURE_UID,
            # The real one: the temporary home is this process's, and is checked as such.
            euid=effective_uid(),
            run=self.runner,
            now=lambda: START,
            which=lambda name: FIXTURE_GIT,
            hostname=lambda: FIXTURE_HOST,
            waiter=waiter,
            out=io.StringIO(),
            err=io.StringIO(),
        )

    @property
    def script(self) -> Path:
        """Where the fake host says the CLI lives: never this checkout's own script."""
        return self.tmp / "tools" / "git_workspace_backup" / "git_workspace_backup.py"

    def main(self, *argv: str, host: "wb.Host | None" = None) -> int:
        """Run ``main()`` once on *host* (default: a fresh one) with fresh output streams."""
        self.out, self.err = io.StringIO(), io.StringIO()
        chosen = self.host() if host is None else host
        return wb.main(list(argv), replace(chosen, out=self.out, err=self.err))

    def state(self, destination: "Path | None" = None) -> Path:
        """The state directory the CLI keeps for *destination* (default: the configured one)."""
        chosen = self.destination if destination is None else destination
        return state_dir_for(self.tree.home / wb.STATE_BASE_NAME, chosen)

    def write_plist(self, args: "Sequence[str] | None" = None) -> Path:
        """Put a plist where install writes one, so status asks launchd about the agent.

        Its ProgramArguments are the script's, then *args* (default: the agent's own), which
        is all uninstall reads back from it.
        """
        chosen = backup_launchd.AGENT_ARGS if args is None else args
        program = [FIXTURE_PYTHON, str(self.script), *chosen]
        plist = backup_launchd.plist_path(self.tree.home)
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(plistlib.dumps({"ProgramArguments": program}))
        return plist

    def assert_one_error_line(self, fragment: str) -> None:
        """The run printed exactly one line, an ERROR naming *fragment*, and no traceback."""
        lines = self.err.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith("ERROR: "), lines)
        self.assertIn(fragment, lines[0])
