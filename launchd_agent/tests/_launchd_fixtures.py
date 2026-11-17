"""The fixtures launchd_agent's grouped suites are built on.

Shared across the grouped suites, by import or by inheritance alike. They live here
rather than inside one suite because a fixture reached ACROSS suites is what makes
the decomposition honest: the alternative is near-copies that drift, and a drifted
copy fails in whichever suite touches it first, pointing at the wrong subject.

Every helper is PUBLIC. A leading underscore would claim these are private to one
module, which stopped being true the moment a second suite imported them.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from collections.abc import Callable, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import launchd_agent
from launchd_agent import Launchctl


#: The uid every fake domain is in: any fixed value, since the fake never asks the host.
UID = 501


LABEL = "local.launchd-agent.test"


TARGET = f"gui/{UID}/{LABEL}"


PRINT = [launchd_agent.LAUNCHCTL, "print", TARGET]


BOOTOUT = [launchd_agent.LAUNCHCTL, "bootout", TARGET]


#: One scripted answer: an exit code with stdout and stderr, or an exception run raises.
Answer = tuple[int, str, str] | BaseException


class FakeLaunchctl:
    """A scripted launchctl: each call pops the next expected argv and its answer.

    ``before_each``, when set, runs before each answer, so a test can observe the disk then.
    """

    def __init__(self, test: unittest.TestCase) -> None:
        self.test = test
        self.script: list[tuple[list[str], Answer]] = []
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []
        self.now = 0.0
        self.sleeps: list[float] = []
        self.before_each: Callable[[], None] | None = None

    def expect(self, argv: list[str], answer: Answer) -> FakeLaunchctl:
        """Queue *answer* for the next call, which must be *argv*."""
        self.script.append((argv, answer))
        return self

    def run(
        self, argv: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        """Answer *argv* from the script, failing the test on an unexpected one."""
        got = list(argv)
        self.calls.append(got)
        self.timeouts.append(timeout)
        if self.before_each is not None:
            self.before_each()
        if not self.script:
            self.test.fail(f"unexpected launchctl call: {got}")
        wanted, answer = self.script.pop(0)
        self.test.assertEqual(wanted, got)
        if isinstance(answer, BaseException):
            raise answer
        code, out, err = answer
        return subprocess.CompletedProcess(got, code, out, err)

    def clock(self) -> float:
        """The fake time, which only sleeping advances."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Advance the fake time by *seconds*."""
        self.sleeps.append(seconds)
        self.now += seconds

    def ctl(self) -> Launchctl:
        """The Launchctl the library is handed."""
        return Launchctl(run=self.run, uid=UID, clock=self.clock, sleep=self.sleep)


LOADED = (0, "state = running\n", "")


ABSENT = (113, "", "Could not find service in domain\n")


OK = (0, "", "")
