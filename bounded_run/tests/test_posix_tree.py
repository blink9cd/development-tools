"""The POSIX tree kill in parts: the ps parser, the group walk, the frozen check and the loop.

All but the live ``ps`` test run on every host. The loop takes its signal call, its signal
numbers and its process table as seams and treats the numbers as opaque, so a recorder and
scripted tables pin its ORDER -- freeze before walking, every stop before any kill -- with no
process at risk. The live nested-session test pins the real signals end to end.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from collections.abc import Iterator, Sequence
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import boundedrun_posix_tree
from boundedrun_posix_tree import ProcessRow, TreeKillSeams

# The determinism convention: a profile registered at import, never an `@settings`
# decorator, which a fuzzing run applying its own settings could not override.
# deadline=None: a per-example wall-clock deadline measures machine load, not correctness.
# too_slow is suppressed for the same
# reason: measured failing here at 4 draws in 1.02 s, one draw alone 0.98 s, on a loaded machine.
settings.register_profile(
    "fuzz_gate_deterministic",
    derandomize=True,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("fuzz_gate_deterministic")

#: Opaque stand-ins for SIGSTOP and SIGKILL: the loop only forwards them.
_STOP = 1001
_KILL = 1002

#: The root the loop is asked to kill, and a caller group none of the tables below contain.
_ROOT = 100
_CALLER_GROUP = 9000

#: A bound no scripted table outlasts, for the tests that are not about the deadline.
_AMPLE_SECONDS = 60.0

#: The log entry a scripted snapshot leaves, so a test reads signals and reads in one order.
_SNAPSHOT = ("snapshot",)


def _row(pid: int, ppid: int, pgid: int, state: str = "S") -> ProcessRow:
    """A table row; running (``S``) unless a test says otherwise."""
    return ProcessRow(pid, ppid, pgid, state)


class _Script:
    """Seams that log each signal and each snapshot, in call order, to one list."""

    def __init__(
        self,
        tables: Sequence[tuple[ProcessRow, ...] | None],
        *,
        refuse: dict[int, type[OSError]] | None = None,
    ) -> None:
        self.log: list[tuple[object, ...]] = []
        self.signals: list[tuple[int, int]] = []
        self.snapshot_timeouts: list[float] = []
        self._tables: Iterator[tuple[ProcessRow, ...] | None] = iter(tables)
        self._refuse = refuse or {}

    def send(self, group: int, sig: int) -> None:
        self.log.append((group, sig))
        self.signals.append((group, sig))
        if group in self._refuse:
            raise self._refuse[group]()

    def snapshot(self, timeout: float) -> tuple[ProcessRow, ...] | None:
        self.log.append(_SNAPSHOT)
        self.snapshot_timeouts.append(timeout)
        return next(self._tables)

    def seams(
        self, *, own_group: int = _CALLER_GROUP, clock: _Clock | None = None
    ) -> TreeKillSeams:
        return TreeKillSeams(
            send=self.send,
            stop_signal=_STOP,
            kill_signal=_KILL,
            snapshot=self.snapshot,
            clock=clock or _Clock(step=0.0),
            own_group=own_group,
        )

    def sent(self, sig: int) -> list[int]:
        """The groups sent *sig*, in order."""
        return [group for group, sent in self.signals if sent == sig]


class _Clock:
    """A clock that advances *step* seconds on every read."""

    def __init__(self, *, step: float) -> None:
        self._now = 0.0
        self._step = step

    def __call__(self) -> float:
        self._now += self._step
        return self._now


#: The nested-session tree: the root, its child leading another session, and that child's own.
_NESTED_RUNNING = (
    _row(_ROOT, 1, _ROOT),
    _row(200, _ROOT, 200),
    _row(300, 200, 300),
    _row(301, 300, 300),
)
_NESTED_FROZEN = tuple(row._replace(state="T") for row in _NESTED_RUNNING)


class ParseProcessTableTests(unittest.TestCase):
    def test_reads_each_well_formed_row(self) -> None:
        text = "    1     0     1 Ss  \n  220     1   220 S+\n"
        self.assertEqual(
            boundedrun_posix_tree.parse_process_table(text),
            (_row(1, 0, 1, "Ss"), _row(220, 1, 220, "S+")),
        )

    def test_skips_every_line_it_cannot_read(self) -> None:
        unreadable = ["", "1 0 1", "1 0 1 S extra", "x 0 1 S", "1 -1 1 S", "1 0 1.5 S"]
        text = "\n".join([*unreadable, "7 1 7 T"])
        self.assertEqual(
            boundedrun_posix_tree.parse_process_table(text), (_row(7, 1, 7, "T"),)
        )

    def test_the_parser_property_holds(self) -> None:
        test_fuzz_parse_process_table()

    def test_a_formatted_table_reads_back_unchanged(self) -> None:
        test_round_trip_parse_process_table()


@given(st.text())
def test_fuzz_parse_process_table(text: str) -> None:
    """Parsing never raises, and every row it keeps carries non-negative ids.

    The property for the ``# fuzz-target`` on ``parse_process_table``: the
    kill runs on whatever ``ps`` printed, so no output may stop it before it signals anything.
    """
    for row in boundedrun_posix_tree.parse_process_table(text):
        assert min(row.pid, row.ppid, row.pgid) >= 0


_IDS = st.integers(min_value=0, max_value=2**31 - 1)
_STATES = st.text(alphabet="DIRSTUZXt+<>Nslx", min_size=1, max_size=4)


@given(st.lists(st.builds(ProcessRow, _IDS, _IDS, _IDS, _STATES), max_size=20))
def test_round_trip_parse_process_table(rows: list[ProcessRow]) -> None:
    """A table printed the way ``ps`` prints it parses back to exactly its rows."""
    text = "".join(f"{r.pid:>6} {r.ppid:>6} {r.pgid:>6} {r.state}\n" for r in rows)
    assert boundedrun_posix_tree.parse_process_table(text) == tuple(rows)


class TreeGroupsTests(unittest.TestCase):
    def test_follows_nested_sessions_down_the_tree(self) -> None:
        self.assertEqual(
            boundedrun_posix_tree.tree_groups(_ROOT, _NESTED_RUNNING),
            {_ROOT, 200, 300},
        )

    def test_reaches_past_an_exited_parent_through_its_group(self) -> None:
        # The root exited unreaped; its child was re-parented to init but kept the root's
        # group, and that child's own nested run leads group 400.
        rows = (_row(_ROOT, 1, _ROOT, "Z"), _row(150, 1, _ROOT), _row(400, 150, 400))
        self.assertEqual(boundedrun_posix_tree.tree_groups(_ROOT, rows), {_ROOT, 400})

    def test_leaves_every_unrelated_process_out(self) -> None:
        rows = (_row(50, 1, 50), _row(_ROOT, 50, _ROOT), _row(500, 50, 500))
        self.assertEqual(boundedrun_posix_tree.tree_groups(_ROOT, rows), {_ROOT})

    def test_never_collects_group_zero_or_one(self) -> None:
        # killpg(0) would address the caller's own group; neither row can be a descendant's.
        rows = (_row(700, _ROOT, 0), _row(701, _ROOT, 1), _row(800, 700, 800))
        self.assertEqual(boundedrun_posix_tree.tree_groups(_ROOT, rows), {_ROOT})

    def test_a_root_missing_from_the_table_is_still_collected(self) -> None:
        self.assertEqual(boundedrun_posix_tree.tree_groups(_ROOT, ()), {_ROOT})


class IsFrozenTests(unittest.TestCase):
    def test_stopped_zombie_and_dead_members_are_frozen(self) -> None:
        for state in ("T", "T+", "t", "Z", "X"):
            with self.subTest(state=state):
                rows = (_row(1, 0, 5, state),)
                self.assertTrue(boundedrun_posix_tree.is_frozen(rows, frozenset({5})))

    def test_a_member_that_can_still_run_is_not(self) -> None:
        for state in ("R", "S", "Ss", "U", "D", "I"):
            with self.subTest(state=state):
                rows = (_row(1, 0, 5, "T"), _row(2, 1, 5, state))
                self.assertFalse(boundedrun_posix_tree.is_frozen(rows, frozenset({5})))

    def test_rows_outside_the_groups_are_ignored(self) -> None:
        rows = (_row(1, 0, 5, "T"), _row(2, 0, 6, "R"))
        self.assertTrue(boundedrun_posix_tree.is_frozen(rows, frozenset({5})))


class FreezeAndKillTests(unittest.TestCase):
    def _run(self, script: _Script, *, own_group: int = _CALLER_GROUP) -> None:
        boundedrun_posix_tree.freeze_and_kill(
            _ROOT, _AMPLE_SECONDS, script.seams(own_group=own_group)
        )

    def test_reads_the_table_first_and_kills_only_after_every_stop(self) -> None:
        # The first snapshot comes before any signal, so the root is checked first.
        script = _Script([_NESTED_RUNNING, _NESTED_FROZEN])
        self._run(script)
        self.assertEqual(
            script.log,
            [
                _SNAPSHOT,
                (_ROOT, _STOP),
                (200, _STOP),
                (300, _STOP),
                _SNAPSHOT,
                (_ROOT, _KILL),
                (200, _KILL),
                (300, _KILL),
            ],
        )

    def test_a_member_still_running_costs_another_round(self) -> None:
        # Every group is already known, but one member forked as its group was stopped.
        straggling = (*_NESTED_FROZEN, _row(302, 301, 300, "R"))
        script = _Script([_NESTED_RUNNING, straggling, _NESTED_FROZEN])
        self._run(script)
        self.assertEqual(script.log.count(_SNAPSHOT), 3)
        self.assertEqual(script.sent(_KILL), [_ROOT, 200, 300])

    def test_an_unreadable_snapshot_kills_what_was_collected(self) -> None:
        script = _Script([_NESTED_RUNNING, None])
        self._run(script)
        self.assertEqual(script.sent(_KILL), [_ROOT, 200, 300])

    def test_an_unreadable_first_snapshot_kills_the_root_group_alone(self) -> None:
        script = _Script([None])
        self._run(script)
        self.assertEqual(script.log, [_SNAPSHOT, (_ROOT, _KILL)])

    def test_the_deadline_ends_a_tree_that_never_settles(self) -> None:
        # Each clock read advances 4s against a 10s bound: the snapshots get 6s and then 2s,
        # after which no time is left for a third, and everything collected is killed.
        script = _Script([_NESTED_RUNNING] * 5)
        boundedrun_posix_tree.freeze_and_kill(
            _ROOT, 10.0, script.seams(clock=_Clock(step=4.0))
        )
        self.assertEqual(script.snapshot_timeouts, [6.0, 2.0])
        self.assertEqual(script.sent(_KILL), [_ROOT, 200, 300])

    def test_the_callers_own_group_is_never_stopped(self) -> None:
        # A tree row in the caller's group is left alone entirely; had it been stopped, the
        # caller would have stopped itself and could never have gone on to the kill.
        rows = (*_NESTED_FROZEN, _row(210, _ROOT, _CALLER_GROUP, "S"))
        script = _Script([rows, rows])
        self._run(script)
        self.assertNotIn(_CALLER_GROUP, script.sent(_STOP) + script.sent(_KILL))

    def test_a_root_below_two_is_refused_before_any_signal(self) -> None:
        """Regression: root 0 was sent STOP then KILL, and killpg(0) is the caller's own group.

        Group 1 is init/launchd's, and a negative number is no group at all.
        """
        for root in (0, 1, -1):
            with self.subTest(root=root):
                script = _Script([_NESTED_RUNNING, _NESTED_FROZEN])
                with self.assertRaises(ValueError):
                    boundedrun_posix_tree.freeze_and_kill(
                        root, _AMPLE_SECONDS, script.seams()
                    )
                self.assertEqual(script.log, [])

    def test_a_root_that_is_the_callers_own_group_is_refused(self) -> None:
        """Regression: it was killed, and killing the caller's group kills the caller."""
        script = _Script([(_row(_ROOT, 1, _ROOT, "S"),)])
        with self.assertRaises(ValueError):
            self._run(script, own_group=_ROOT)
        self.assertEqual(script.log, [])

    def test_a_root_that_leads_no_group_of_its_own_is_refused(self) -> None:
        """Regression: a root in another's group had that whole group collected and killed.

        Probed: ``tree_groups(600, rows)`` with 600 in group 400 returned ``{400, 600}``.
        """
        rows = (_row(400, 1, 400), _row(_ROOT, 400, 400), _row(500, _ROOT, 500))
        script = _Script([rows, rows])
        with self.assertRaises(ValueError):
            self._run(script)
        self.assertEqual(script.log, [_SNAPSHOT])

    def test_a_root_missing_from_the_table_is_still_killed(self) -> None:
        # An exited, reaped leader whose group lives on: nothing to check it against.
        orphan = (_row(150, 1, _ROOT, "T"),)
        script = _Script([orphan, orphan])
        self._run(script)
        self.assertEqual(script.sent(_KILL), [_ROOT])

    def test_a_group_gone_or_not_ours_does_not_stop_the_kill(self) -> None:
        refuse = {200: ProcessLookupError, 300: PermissionError}
        script = _Script([_NESTED_RUNNING, _NESTED_FROZEN], refuse=refuse)
        self._run(script)
        self.assertEqual(script.sent(_KILL), [_ROOT, 200, 300])


class TakeSnapshotTests(unittest.TestCase):
    def test_lists_this_process_with_its_parent_and_group(self) -> None:
        if sys.platform == "win32":
            # skipTest never returns, so mypy's win32 pass stops here and the POSIX-only
            # os.getpgrp below still type-checks on that platform.
            self.skipTest("the ps snapshot is POSIX; win32 kills through a job")
        rows = boundedrun_posix_tree.take_snapshot(_AMPLE_SECONDS)
        if rows is None:
            self.fail("ps could not be read")
        mine = [row for row in rows if row.pid == os.getpid()]
        self.assertEqual(
            [(row.ppid, row.pgid) for row in mine], [(os.getppid(), os.getpgrp())]
        )

    def test_a_table_the_command_prints_is_parsed(self) -> None:
        # Platform-neutral, unlike the live ps test above: a stand-in command prints the
        # table, so the read-and-parse path is exercised on win32 as well.
        table = "  7  1  7 S\n  8  7  7 R+\n"
        argv = (sys.executable, "-c", f"print({table!r}, end='')")
        self.assertEqual(
            boundedrun_posix_tree.take_snapshot(_AMPLE_SECONDS, argv),
            (ProcessRow(7, 1, 7, "S"), ProcessRow(8, 7, 7, "R+")),
        )

    def test_a_command_that_cannot_start_reads_as_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "no-such-ps")
            self.assertIsNone(
                boundedrun_posix_tree.take_snapshot(_AMPLE_SECONDS, (missing,))
            )

    def test_a_failing_command_reads_as_none(self) -> None:
        argv = (sys.executable, "-c", "raise SystemExit(3)")
        self.assertIsNone(boundedrun_posix_tree.take_snapshot(_AMPLE_SECONDS, argv))

    def test_a_command_past_its_timeout_reads_as_none(self) -> None:
        argv = (sys.executable, "-c", "import time; time.sleep(60)")
        self.assertIsNone(boundedrun_posix_tree.take_snapshot(0.5, argv))


if __name__ == "__main__":
    unittest.main()
