#!/usr/bin/env python3
"""The run suites' shared case: a fixture tree, a temp tool base, and one run's seams.

Deliberately NOT named ``test_*``, so the aggregator's discover skips it.

Nothing a run touches belongs to the host. The tool base, its state, the destination and the
system temp dir that verification restores into all sit in the case's own temp directory;
memory_snapshot is answered by a route; notifications are collected, never posted; and the
clock and the host name are fixed, the host name an obvious fixture.
"""

from __future__ import annotations

import io
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import _fixtures as fx

import backup_proc
import backup_run as brun
from backup_ledger import read_last_run, state_dir_for
from backup_model import RunRecord

START = datetime(2026, 9, 14, 6, 0, 0)
#: The host name the injected seam reports, and the machine label it derives.
FIXTURE_HOST = "Fixture-Mac.local"
MACHINE = "fixture-mac"
#: The snapshot name a run at START writes.
FIRST_NAME = f"2026-09-14T060000-{MACHINE}"
#: What the injected PATH lookup reports; the run itself executes whichever git is on PATH.
FIXTURE_GIT = "/opt/fixture-bin/git"


class RunCase(unittest.TestCase):
    """A fixture tree and everything one run of it needs, all under one temp directory."""

    def setUp(self) -> None:
        self.enterContext(fx.isolated_git_env())
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.tree = fx.make_tree(self.tmp)
        self.base = self.tmp / "home" / ".workspace_backup"
        self.destination = self.tree.request.config.destination
        self.state = state_dir_for(self.base, self.destination)
        # Verification makes its scratch directory in the system temp dir; pointing that here
        # keeps a leaked scratch directory visible to the test instead of lost on the host.
        self.scratch_root = self.tmp / "system-temp"
        self.scratch_root.mkdir()
        self.enterContext(
            mock.patch.object(tempfile, "tempdir", str(self.scratch_root))
        )
        self.notes: list[tuple[str, str]] = []
        self.notify_accepts = True
        self.clock = START
        self.hostname = FIXTURE_HOST
        self.out = io.StringIO()
        self.err = io.StringIO()
        self.runner = fx.FakeRunner(fx.MEMORY_OK)

    def request(self) -> brun.RunRequest:
        """The request for a plain run of the fixture tree."""
        return brun.RunRequest(
            roots=self.tree.request.roots,
            config=self.tree.request.config,
            base=self.base,
            tools_dir=self.tmp / "tools",
            python="/opt/fixture-python",
            if_due=False,
            dry_run=False,
            platform=brun.DARWIN,
        )

    def run_backup(
        self,
        request: brun.RunRequest,
        *,
        routes: "dict[str, fx.Route] | None" = None,
        real: backup_proc.RunCmd = backup_proc.run_cmd,
        git: "str | None" = FIXTURE_GIT,
    ) -> int:
        """Run *request* once with fresh output streams; the exit code."""
        self.out, self.err = io.StringIO(), io.StringIO()
        self.runner = fx.FakeRunner(
            fx.MEMORY_OK if routes is None else routes, real=real
        )
        deps = brun.RunDeps(
            run=self.runner,
            now=lambda: self.clock,
            which=lambda name: git,
            notify=self._collect,
            hostname=lambda: self.hostname,
            out=self.out,
            err=self.err,
        )
        return brun.run_backup(request, deps)

    def last_run(self, state: "Path | None" = None) -> RunRecord:
        """The recorded last run under *state* (default: the fixture destination's)."""
        record = read_last_run(self.state if state is None else state)
        assert record is not None, "no last run was recorded"
        return record

    def memory_snapshot_calls(self) -> list[list[str]]:
        """Every memory_snapshot invocation the last run made: none means nothing was built."""
        return [
            argv
            for argv in self.runner.calls
            if argv[1:2] and "memory_snapshot" in argv[1]
        ]

    def restore_permissions(self, path: Path) -> None:
        """Give *path* back its owner's permissions at cleanup, so the temp tree can go."""
        self.addCleanup(path.chmod, 0o700)

    def _collect(self, title: str, message: str) -> bool:
        self.notes.append((title, message))
        return self.notify_accepts
