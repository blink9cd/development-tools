#!/usr/bin/env python3
"""The status verdict: what each fact comes to, and where each threshold turns.

Pure: every case is a ``replace`` of the healthy facts in ``_status_case``, judged with no
I/O. Gathering the facts from a destination is test_status_gather.py.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from _status_case import (
    COMPLETE_RUN,
    HEALTHY,
    LATER,
    MACHINE,
    MANIFEST,
    MINUTE,
    NEWEST,
    NOW,
    OLDER,
    SKIPPED_RUN,
    TAKEN,
    entry,
    text_of,
)

import backup_icloud as bi
import backup_status as bs
from backup_model import (
    OUTCOME_FAILED,
    OUTCOME_INCOMPLETE,
    STATE_INCOMING,
    Ledger,
    LedgerEntry,
    RunRecord,
)


class JudgeTests(unittest.TestCase):
    def test_the_thresholds_are_the_specs(self) -> None:
        self.assertEqual(bs.STALE_AFTER, timedelta(hours=36))
        self.assertEqual(bs.UPLOAD_GRACE, timedelta(hours=24))

    def test_not_installed_or_never_recorded_cannot_be_judged(self) -> None:
        stale = replace(HEALTHY, now=TAKEN + bs.STALE_AFTER)
        absent = bs.judge(replace(stale, agent=bs.AGENT_ABSENT))
        self.assertEqual((absent.code, len(absent.lines)), (2, 1))
        self.assertIn("not installed", absent.lines[0])
        unrecorded = bs.judge(replace(stale, last_run=None))
        self.assertEqual((unrecorded.code, len(unrecorded.lines)), (2, 1))
        self.assertIn("run record", unrecorded.lines[0])

    def test_a_healthy_destination_exits_0_naming_its_newest_snapshot(self) -> None:
        verdict = bs.judge(HEALTHY)
        self.assertEqual((verdict.code, len(verdict.lines)), (0, 1))
        self.assertIn("healthy", verdict.lines[0])
        self.assertIn(NEWEST, verdict.lines[0])

    def test_each_problem_exits_1_and_says_what_it_is(self) -> None:
        many = Ledger((entry(OLDER), entry("2026-09-13T070000"), entry(NEWEST)))
        failed = RunRecord("t", OUTCOME_FAILED, "boom", None, ())
        cases = {
            "agent unknown": (replace(HEALTHY, agent=bs.AGENT_UNKNOWN), "agent"),
            "agent unrecognized": (replace(HEALTHY, agent="bogus"), "agent"),
            "last failed": (replace(HEALTHY, last_run=failed), "failed: boom"),
            "last incomplete": (
                replace(
                    HEALTHY, last_run=replace(COMPLETE_RUN, outcome=OUTCOME_INCOMPLETE)
                ),
                "incomplete",
            ),
            "last outcome unrecognized": (
                replace(HEALTHY, last_run=replace(COMPLETE_RUN, outcome="bogus")),
                "bogus",
            ),
            "no complete snapshot": (
                replace(
                    HEALTHY,
                    newest_complete=None,
                    newest_manifest=None,
                    upload=bi.UPLOAD_NONE,
                ),
                "no complete snapshot",
            ),
            "name records no time": (
                replace(HEALTHY, newest_complete="not-a-snapshot"),
                "not-a-snapshot",
            ),
            "stale": (replace(HEALTHY, now=datetime(2026, 9, 15, 18, 1)), "h old"),
            "manifest unreadable": (replace(HEALTHY, newest_manifest=None), "manifest"),
            "unclassified": (
                replace(
                    HEALTHY,
                    newest_manifest=replace(
                        MANIFEST, unclassified_ignored=("new_folder",)
                    ),
                ),
                "new_folder",
            ),
            "upload overdue": (
                replace(
                    HEALTHY, upload=bi.UPLOAD_PENDING, now=datetime(2026, 9, 15, 6, 1)
                ),
                "not uploaded",
            ),
            "upload unreadable": (
                replace(HEALTHY, upload=bi.UPLOAD_UNREADABLE),
                "upload state",
            ),
            "upload unasked beside a snapshot": (
                replace(HEALTHY, upload=bi.UPLOAD_NONE),
                "upload state",
            ),
            "upload unrecognized": (replace(HEALTHY, upload="bogus"), "upload state"),
            "too many snapshots": (
                replace(HEALTHY, keep=1, ledger=many),
                "3 snapshots",
            ),
        }
        for label, (facts, fragment) in cases.items():
            with self.subTest(label):
                verdict = bs.judge(facts)
                self.assertEqual(verdict.code, 1)
                self.assertIn(fragment, text_of(verdict))

    def test_a_skip_masks_neither_staleness_nor_an_incomplete_snapshot(self) -> None:
        skipped = replace(HEALTHY, last_run=SKIPPED_RUN)
        self.assertEqual(bs.judge(skipped).code, 0)
        stale = bs.judge(replace(skipped, now=TAKEN + bs.STALE_AFTER))
        self.assertEqual(stale.code, 1)
        self.assertIn("h old", text_of(stale))
        # A run that left an incomplete snapshot, then a skip that overwrote its record: the
        # ledger still shows the incomplete snapshot, and that stays a problem.
        over_incomplete = replace(
            skipped, ledger=Ledger((entry(NEWEST), entry(LATER, complete=False)))
        )
        verdict = bs.judge(over_incomplete)
        self.assertEqual(verdict.code, 1)
        self.assertIn(LATER, text_of(verdict))
        # An incoming leftover is not a snapshot, and says nothing about the last run.
        incoming = LedgerEntry(f".incoming-{LATER}", STATE_INCOMING, False, "")
        beside_incoming = replace(skipped, ledger=Ledger((entry(NEWEST), incoming)))
        self.assertEqual(bs.judge(beside_incoming).code, 0)

    def test_every_problem_is_listed_and_notes_follow_them(self) -> None:
        facts = replace(
            HEALTHY,
            agent=bs.AGENT_UNKNOWN,
            newest_manifest=None,
            upload=bi.UPLOAD_NOT_ICLOUD,
            in_icloud_drive=False,
        )
        verdict = bs.judge(facts)
        self.assertEqual((verdict.code, len(verdict.lines)), (1, 3))
        self.assertIn("agent", verdict.lines[0])
        self.assertIn("manifest", verdict.lines[1])
        self.assertIn("not in iCloud", verdict.lines[2])

    def test_untracked_snapshot_folders_are_a_note_that_alone_leaves_exit_0(
        self,
    ) -> None:
        # After a machine restore the ledger starts empty: every old snapshot is untracked,
        # and prune never removes one, so they are said, though nothing is wrong.
        verdict = bs.judge(replace(HEALTHY, untracked=(OLDER,)))
        self.assertEqual((verdict.code, len(verdict.lines)), (0, 2))
        self.assertIn(OLDER, verdict.lines[1])
        self.assertIn("never prunes", verdict.lines[1])
        many = tuple(f"2026-09-0{day}T060000" for day in range(1, 10))
        text = text_of(bs.judge(replace(HEALTHY, untracked=many)))
        self.assertIn(f"and {len(many) - bs.UNTRACKED_NAMED} more", text)
        for name in many[bs.UNTRACKED_NAMED :]:
            self.assertNotIn(name, text)

    def test_another_machines_snapshots_are_one_count_that_alone_leaves_exit_0(
        self,
    ) -> None:
        verdict = bs.judge(replace(HEALTHY, other_machines=2))
        self.assertEqual(verdict.code, bs.EXIT_HEALTHY)
        self.assertEqual(len(verdict.lines), 2)
        self.assertIn("2 snapshots from other machines", verdict.lines[1])
        one = text_of(bs.judge(replace(HEALTHY, other_machines=1)))
        self.assertIn("1 snapshot from another machine", one)
        self.assertNotIn("other machines", text_of(bs.judge(HEALTHY)))

    def test_an_untracked_folder_named_for_this_machine_needs_attention(self) -> None:
        own = f"{OLDER}-{MACHINE}"
        verdict = bs.judge(replace(HEALTHY, untracked=(own,)))
        self.assertEqual(verdict.code, bs.EXIT_ATTENTION)
        problem = " ".join(verdict.lines[0].split())
        # This machine's own snapshot that a run could not ledger is the likely cause, and
        # a second machine sharing the label the other one; each gets its own fix.
        for phrase in (
            own,
            "this machine's own snapshot that a run could not ledger",
            "keep or delete it by hand",
            "Only if a second machine uses the same label",
            "edit ~/.workspace_backup/machine on one of them",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, problem)
        self.assertNotIn("may share this identity", problem)
        # Still listed as untracked, as before identity: prune never touches it.
        self.assertIn(own, verdict.lines[1])
        self.assertIn("never prunes", verdict.lines[1])

    def test_a_bare_untracked_folder_is_only_a_note_whatever_the_machine(self) -> None:
        for machine in (MACHINE, None):
            with self.subTest(machine=machine):
                facts = replace(HEALTHY, untracked=(OLDER,), machine=machine)
                self.assertEqual(bs.judge(facts).code, bs.EXIT_HEALTHY)
        unnamed = replace(HEALTHY, untracked=(f"{OLDER}-{MACHINE}",), machine=None)
        self.assertEqual(bs.judge(unnamed).code, bs.EXIT_HEALTHY)

    def test_an_aware_now_is_refused_when_the_facts_are_built(self) -> None:
        # Snapshot names record naive local time; an aware now would otherwise escape judge
        # as a TypeError from the first subtraction.
        with self.assertRaisesRegex(ValueError, "now"):
            replace(HEALTHY, now=NOW.replace(tzinfo=timezone.utc))


class BoundaryTests(unittest.TestCase):
    """Each threshold at exactly its value, and one minute either side."""

    def test_staleness_turns_at_exactly_its_threshold(self) -> None:
        for offset, code in ((-MINUTE, 0), (timedelta(0), 1), (MINUTE, 1)):
            with self.subTest(offset=offset):
                now = TAKEN + bs.STALE_AFTER + offset
                self.assertEqual(bs.judge(replace(HEALTHY, now=now)).code, code)

    def test_a_pending_upload_is_a_note_inside_its_grace_and_a_problem_from_its_end(
        self,
    ) -> None:
        pending = replace(HEALTHY, upload=bi.UPLOAD_PENDING)
        inside = bs.judge(replace(pending, now=TAKEN + bs.UPLOAD_GRACE - MINUTE))
        self.assertEqual((inside.code, len(inside.lines)), (0, 2))
        self.assertIn("pending", inside.lines[1])
        for offset in (timedelta(0), MINUTE):
            with self.subTest(offset=offset):
                now = TAKEN + bs.UPLOAD_GRACE + offset
                verdict = bs.judge(replace(pending, now=now))
                self.assertEqual(verdict.code, 1)
                self.assertIn("not uploaded", text_of(verdict))
                self.assertNotIn("pending", text_of(verdict))

    def test_a_snapshot_dated_after_now_needs_attention_and_one_dated_now_does_not(
        self,
    ) -> None:
        # A negative age is never stale, and a pending upload would sit inside its grace for
        # ever: a clock set back, or a snapshot from the future, must be said.
        pending = replace(HEALTHY, upload=bi.UPLOAD_PENDING)
        future = bs.judge(replace(pending, now=TAKEN - MINUTE))
        self.assertEqual(future.code, 1)
        self.assertIn("after now", text_of(future))
        self.assertNotIn("pending", text_of(future))
        at_now = bs.judge(replace(pending, now=TAKEN))
        self.assertEqual((at_now.code, len(at_now.lines)), (0, 2))
        self.assertIn("pending", at_now.lines[1])

    def test_the_snapshot_count_turns_past_twice_keep(self) -> None:
        two = Ledger((entry(OLDER), entry(NEWEST)))
        self.assertEqual(bs.judge(replace(HEALTHY, keep=1, ledger=two)).code, 0)
        three = Ledger((entry("2026-09-12T060000"), *two.entries))
        self.assertEqual(bs.judge(replace(HEALTHY, keep=1, ledger=three)).code, 1)
        incoming = LedgerEntry(f".incoming-{LATER}", STATE_INCOMING, False, "")
        two_and_leftover = Ledger((*two.entries, incoming))
        self.assertEqual(
            bs.judge(replace(HEALTHY, keep=1, ledger=two_and_leftover)).code, 0
        )


class IcloudDriveTests(unittest.TestCase):
    """Not in iCloud is a note for a destination chosen outside iCloud Drive, a problem inside."""

    def test_a_destination_outside_icloud_drive_is_healthy_with_a_note(self) -> None:
        outside = replace(HEALTHY, upload=bi.UPLOAD_NOT_ICLOUD, in_icloud_drive=False)
        verdict = bs.judge(outside)
        self.assertEqual((verdict.code, len(verdict.lines)), (0, 2))
        self.assertIn("not in iCloud", verdict.lines[1])

    def test_a_destination_under_icloud_drive_whose_files_are_not_in_icloud_is_a_problem(
        self,
    ) -> None:
        # iCloud Drive turned off or signed out, and the folder recreated as a plain local
        # directory: every run succeeds and nothing leaves the machine.
        verdict = bs.judge(replace(HEALTHY, upload=bi.UPLOAD_NOT_ICLOUD))
        self.assertEqual((verdict.code, len(verdict.lines)), (1, 1))
        self.assertIn("signed in", verdict.lines[0])


if __name__ == "__main__":
    unittest.main()
