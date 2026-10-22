#!/usr/bin/env python3
"""One run end to end, against real git and a temp destination: the pipeline's exit paths.

The guards the pipeline holds -- a manifest that must read back before it is ledgered, fresh
staging, a document that refuses to render, verification scratch, a record that cannot be
written -- are pinned in test_run_guards.py.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from _run_case import FIRST_NAME, MACHINE, START, RunCase

import backup_rearm as brr
import backup_run as brun
from backup_fs import RunLock, sha256_file
from backup_ledger import LEDGER_NAME, load_ledger, read_last_run, state_dir_for
from backup_manifest import MANIFEST_NAME, parse_manifest
from backup_model import (
    OUTCOME_COMPLETE,
    OUTCOME_FAILED,
    OUTCOME_INCOMPLETE,
    OUTCOME_SKIPPED,
    STATE_PROMOTED,
    FailedItem,
    Ledger,
    LedgerEntry,
)
from backup_promote import PruneResult
from backup_restore_doc import RESTORE_DOC_NAME

_MEMORY_FAILS = {"memory_snapshot.py": lambda argv: (1, "", "boom")}


class RunTests(RunCase):
    def test_a_complete_run_promotes_records_and_stays_quiet(self) -> None:
        self.assertEqual(self.run_backup(self.request()), brun.EXIT_OK)
        folder = self.destination / FIRST_NAME
        manifest = parse_manifest((folder / MANIFEST_NAME).read_bytes())
        assert manifest is not None
        self.assertTrue(manifest.complete)
        self.assertEqual(manifest.failed, ())
        self.assertEqual(manifest.unclassified_ignored, ("new_folder",))
        self.assertIn(RESTORE_DOC_NAME, [entry.name for entry in manifest.files])
        for entry in manifest.files:
            with self.subTest(file=entry.name):
                self.assertEqual(sha256_file(folder / entry.name), entry.sha256)
        self.assertIn("complete: yes", (folder / RESTORE_DOC_NAME).read_text("utf-8"))
        self.assertEqual(
            load_ledger(self.state).entries,
            (
                LedgerEntry(
                    FIRST_NAME,
                    STATE_PROMOTED,
                    True,
                    sha256_file(folder / MANIFEST_NAME),
                ),
            ),
        )
        record = self.last_run()
        self.assertEqual(
            (record.outcome, record.snapshot), (OUTCOME_COMPLETE, FIRST_NAME)
        )
        self.assertEqual(self.notes, [])
        self.assertEqual(list((self.base / brun.STAGING_DIR).iterdir()), [])
        self.assertEqual(list(self.scratch_root.iterdir()), [])
        self.assertEqual(self.out.getvalue(), f"complete: snapshot {FIRST_NAME}\n")
        self.assertEqual(self.err.getvalue(), "")

    def test_the_restore_document_carries_the_configured_or_default_rearm_step(
        self,
    ) -> None:
        """The run hands the config's commands, or the derived default, to RESTORE.md.

        The configured setup document travels with them, into the step's prose.
        """
        workspace = self.tree.request.roots.workspace
        configured = ("python tools/fixture_hooks.py --install",)
        cases = {
            "configured": (configured, brr.ReArm(configured, configured=True)),
            "absent": (None, brr.default_rearm(workspace)),
        }
        for label, (commands, expected) in cases.items():
            with self.subTest(label):
                config = replace(
                    self.tree.request.config,
                    rearm_commands=commands,
                    setup_doc="docs/FIXTURE_SETUP.md",
                    destination=self.tmp / f"dest-{label}",
                )
                request = replace(self.request(), config=config)
                self.assertEqual(self.run_backup(request), brun.EXIT_OK)
                doc = (config.destination / FIRST_NAME / RESTORE_DOC_NAME).read_text(
                    "utf-8"
                )
                for command in expected.commands:
                    self.assertIn(f"\n{command}\n", doc)
                self.assertIn("`docs/FIXTURE_SETUP.md`", doc)

    def test_an_incomplete_snapshot_is_promoted_flagged_and_notified(self) -> None:
        code = self.run_backup(self.request(), routes=_MEMORY_FAILS)
        self.assertEqual(code, brun.EXIT_ATTENTION)
        [entry] = load_ledger(self.state).entries
        self.assertEqual((entry.name, entry.complete), (FIRST_NAME, False))
        record = self.last_run()
        self.assertEqual(record.outcome, OUTCOME_INCOMPLETE)
        self.assertEqual(
            record.failed, (FailedItem("memory_snapshot", "exit 1: boom"),)
        )
        self.assertIn("1 failed item(s): memory_snapshot: exit 1: boom", record.detail)
        self.assertEqual(
            self.notes, [(brun.NOTIFY_TITLE, f"{OUTCOME_INCOMPLETE}: {record.detail}")]
        )
        doc = (self.destination / FIRST_NAME / RESTORE_DOC_NAME).read_text("utf-8")
        self.assertIn("complete: no", doc)

    def test_validation_failures_exit_2_record_and_notify_without_touching_the_destination(
        self,
    ) -> None:
        a_file = self.tmp / "a-file"
        a_file.write_text("not a directory", encoding="utf-8", newline="\n")
        config = self.tree.request.config
        cases = (
            ("platform", replace(self.request(), platform="linux"), "macOS only"),
            ("parent absent", self._to(self.tmp / "absent" / "dest"), "does not exist"),
            ("parent a file", self._to(a_file / "dest"), "is not a directory"),
            (
                "parent unexaminable",
                self._to(a_file / "sub" / "dest"),
                "cannot be examined",
            ),
        )
        for label, request, reason in cases:
            with self.subTest(label):
                self.assertEqual(self.run_backup(request), brun.EXIT_UNRUNNABLE)
                state = state_dir_for(self.base, request.config.destination)
                record = self.last_run(state)
                self.assertEqual(record.outcome, OUTCOME_FAILED)
                self.assertIn(reason, record.detail)
                self.assertFalse(request.config.destination.exists())
        with self.subTest("git"):
            self.assertEqual(
                self.run_backup(self.request(), git=None), brun.EXIT_UNRUNNABLE
            )
            self.assertEqual(self.last_run().detail, "git is not on PATH")
        self.assertFalse(config.destination.exists())
        self.assertEqual(len(self.notes), len(cases) + 1)
        self.assertEqual(self.memory_snapshot_calls(), [])

    def test_a_ledger_that_is_not_ours_or_cannot_be_read_exits_2(self) -> None:
        self.state.mkdir(parents=True)
        ledger = self.state / LEDGER_NAME
        ledger.write_text("{", encoding="utf-8", newline="\n")
        self.assertEqual(self.run_backup(self.request()), brun.EXIT_UNRUNNABLE)
        self.assertIn("not a ledger this tool wrote", self.last_run().detail)
        ledger.unlink()
        ledger.mkdir()
        self.assertEqual(self.run_backup(self.request()), brun.EXIT_UNRUNNABLE)
        self.assertIn("could not read the ledger", self.last_run().detail)
        self.assertFalse(self.destination.exists())
        self.assertEqual(len(self.notes), 2)

    def test_a_held_lock_skip_prints_only_and_a_failed_record_survives_it(self) -> None:
        holder = RunLock(self.base / brun.LOCK_NAME)
        self.assertTrue(holder.acquire())
        self.addCleanup(holder.release)
        self.assertEqual(self.run_backup(self.request()), brun.EXIT_ATTENTION)
        self.assertIsNone(read_last_run(self.state))
        self.assertEqual(self.notes, [])
        self.assertEqual(self.err.getvalue(), "skipped: another run holds the lock\n")
        self.assertFalse(self.destination.exists())
        # The holder's own failure, recorded: a skip racing its notification must not
        # overwrite it, or status reads the skip and says nothing.
        holder.release()
        linux = replace(self.request(), platform="linux")
        self.assertEqual(self.run_backup(linux), brun.EXIT_UNRUNNABLE)
        failed = self.last_run()
        self.assertEqual(failed.outcome, OUTCOME_FAILED)
        self.assertTrue(holder.acquire())
        self.notes.clear()
        self.assertEqual(self.run_backup(self.request()), brun.EXIT_ATTENTION)
        self.assertEqual(self.last_run(), failed)
        self.assertEqual(self.notes, [])

    def test_a_clock_set_back_never_prunes_the_snapshot_its_own_run_promoted(
        self,
    ) -> None:
        request = replace(
            self.request(), config=replace(self.tree.request.config, keep=2)
        )
        for days in (2, 1):
            self.clock = START + timedelta(days=days)
            self.assertEqual(self.run_backup(request), brun.EXIT_OK)
        # Set back past both, so keep complete snapshots are named after the new one and it
        # alone falls outside the newest keep.
        self.clock = START
        self.assertEqual(self.run_backup(request), brun.EXIT_OK)
        self.assertEqual(self.last_run().snapshot, FIRST_NAME)
        folders = [path.name for path in self.destination.iterdir()]
        self.assertIn(FIRST_NAME, folders)
        self.assertEqual(len(folders), 3)
        ledgered = [entry.name for entry in load_ledger(self.state).entries]
        self.assertIn(FIRST_NAME, ledgered)

    def test_if_due_skips_a_covered_day_and_runs_the_next(self) -> None:
        self.run_backup(self.request())
        due = replace(self.request(), if_due=True)
        self.clock = START + timedelta(hours=1)
        self.assertEqual(self.run_backup(due), brun.EXIT_OK)
        self.assertEqual(self.last_run().outcome, OUTCOME_SKIPPED)
        self.assertEqual(self.memory_snapshot_calls(), [])
        self.clock = START + timedelta(days=1)
        self.assertEqual(self.run_backup(due), brun.EXIT_OK)
        self.assertEqual(len(list(self.destination.iterdir())), 2)

    def test_if_due_runs_when_today_holds_only_an_incomplete_snapshot(self) -> None:
        self.run_backup(self.request(), routes=_MEMORY_FAILS)
        self.clock = START + timedelta(hours=1)
        self.assertEqual(
            self.run_backup(replace(self.request(), if_due=True)), brun.EXIT_OK
        )
        self.assertEqual(self.last_run().outcome, OUTCOME_COMPLETE)
        self.assertEqual(len(list(self.destination.iterdir())), 2)

    def test_a_same_day_rerun_adds_a_folder_and_prune_honors_keep(self) -> None:
        config = replace(self.tree.request.config, keep=1)
        request = replace(self.request(), config=config)
        for second in range(3):
            self.clock = START + timedelta(seconds=second)
            self.assertEqual(self.run_backup(request), brun.EXIT_OK)
        self.assertEqual(
            [path.name for path in self.destination.iterdir()],
            [f"2026-09-14T060002-{MACHINE}"],
        )
        self.assertEqual(
            [entry.name for entry in load_ledger(self.state).entries],
            [f"2026-09-14T060002-{MACHINE}"],
        )
        self.assertIn("pruned 1", self.out.getvalue())

    def test_a_promote_failure_is_recorded_as_failed(self) -> None:
        occupied = self.destination / FIRST_NAME
        occupied.mkdir(parents=True)
        self.assertEqual(self.run_backup(self.request()), brun.EXIT_ATTENTION)
        record = self.last_run()
        self.assertEqual(record.outcome, OUTCOME_FAILED)
        self.assertIn("promote failed", record.detail)
        self.assertEqual(load_ledger(self.state).entries, ())
        self.assertEqual(list(occupied.iterdir()), [])
        self.assertEqual(len(self.notes), 1)

    def test_an_unexpected_filesystem_error_is_recorded_not_raised(self) -> None:
        staging_root = self.base / brun.STAGING_DIR
        staging_root.mkdir(parents=True)
        staging_root.chmod(0o500)
        self.restore_permissions(staging_root)
        self.assertEqual(self.run_backup(self.request()), brun.EXIT_ATTENTION)
        self.assertIn("unexpected filesystem error", self.last_run().detail)
        self.assertEqual(len(self.notes), 1)

    def test_a_dry_run_prints_the_plan_and_writes_nothing(self) -> None:
        config = replace(self.tree.request.config, home_files=())
        request = replace(self.request(), dry_run=True, config=config)
        self.assertEqual(self.run_backup(request), brun.EXIT_OK)
        self.assertIn(
            f"would write: {self.destination / FIRST_NAME}", self.out.getvalue()
        )
        # The fixture's two memory repositories: one with history, one with none yet.
        self.assertIn("memory repositories: 2", self.out.getvalue())
        self.assertIn("home files: (none)", self.out.getvalue())
        self.assertFalse(self.base.exists())
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.notes, [])

    def test_summaries_name_failures_prunes_and_deferrals(self) -> None:
        text = brun.summarize_run(
            "s", (FailedItem("a", "b"),), PruneResult(Ledger(), ("old",), ("stuck",))
        )
        self.assertEqual(
            text, "snapshot s; 1 failed item(s): a: b; pruned 1; prune deferred: stuck"
        )
        self.assertEqual(
            brun.summarize_run("s", (), PruneResult(Ledger(), (), ())), "snapshot s"
        )

    def _to(self, destination: Path) -> brun.RunRequest:
        """The plain request, pointed at *destination*."""
        config = replace(self.tree.request.config, destination=destination)
        return replace(self.request(), config=config)


if __name__ == "__main__":
    unittest.main()
