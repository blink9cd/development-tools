#!/usr/bin/env python3
"""The guards one run holds, each driven end to end against real git and a temp destination.

- A manifest is ledgered only once it reads back from the destination as written.
- Staging is fresh: a stale folder that cannot be swept, a symlinked staging root, and a
  folder the sweep left behind each fail the run before anything is built.
- A RESTORE.md that refuses to render, a staged file that cannot be hashed, and verification
  scratch that cannot be made or removed are failed items on a promoted snapshot.
- A record that cannot be written, a lock that cannot be taken, and a notification that is
  not posted are each said somewhere a person will see.
"""

from __future__ import annotations

import errno
import io
import os
import tempfile
import unittest
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest import mock

import _fixtures as fx
from _run_case import FIRST_NAME, FIXTURE_GIT, FIXTURE_HOST, MACHINE, START, RunCase

import backup_notify
import backup_proc
import backup_run as brun
import backup_seal as seal
import backup_staging as bstage
from backup_build import BuildResult
from backup_fs import RunLock, sha256_bytes
from backup_ledger import LAST_RUN_NAME, STATE_DIR_NAME, load_ledger
from backup_manifest import MANIFEST_NAME, manifest_to_json, parse_manifest
from backup_model import (
    OUTCOME_FAILED,
    OUTCOME_INCOMPLETE,
    FailedItem,
    Ledger,
)
from backup_restore_doc import RESTORE_DOC_NAME


class _UnwritableStream(io.StringIO):
    """An output stream every write to which fails, as a broken pipe's does."""

    def write(self, text: str) -> int:
        raise BrokenPipeError(errno.EPIPE, "fixture broken pipe")


class ConfirmationTests(RunCase):
    def test_a_manifest_that_does_not_read_back_is_never_ledgered_and_nothing_is_pruned(
        self,
    ) -> None:
        keep_two = replace(
            self.request(), config=replace(self.tree.request.config, keep=2)
        )
        for second in range(2):
            self.clock = START + timedelta(seconds=second)
            self.assertEqual(self.run_backup(keep_two), brun.EXIT_OK)
        before = load_ledger(self.state).entries
        keep_one = replace(
            self.request(), config=replace(self.tree.request.config, keep=1)
        )
        for second, read_back in ((2, None), (3, b"not the manifest written")):
            name = f"2026-09-14T06000{second}-{MACHINE}"
            with self.subTest(read_back=read_back):
                self.clock = START + timedelta(seconds=second)
                with mock.patch.object(
                    brun, "read_bytes_bounded", return_value=read_back
                ):
                    self.assertEqual(self.run_backup(keep_one), brun.EXIT_ATTENTION)
                record = self.last_run()
                self.assertEqual(
                    (record.outcome, record.snapshot), (OUTCOME_FAILED, name)
                )
                self.assertIn("promoted but not confirmed", record.detail)
                self.assertTrue((self.destination / name / MANIFEST_NAME).is_file())
                # Neither ledgered on an unproven hash nor pruned past: keep=1 would
                # otherwise delete the older of the two confirmed snapshots.
                self.assertEqual(load_ledger(self.state).entries, before)
                for entry in before:
                    self.assertTrue((self.destination / entry.name).is_dir())
        self.assertEqual(len(self.notes), 2)

    def test_the_read_back_is_of_the_promoted_manifest(self) -> None:
        with mock.patch.object(
            brun, "read_bytes_bounded", wraps=brun.read_bytes_bounded
        ) as read:
            self.assertEqual(self.run_backup(self.request()), brun.EXIT_OK)
        [(path, timeout)] = [call.args for call in read.call_args_list]
        self.assertEqual(path, self.destination / FIRST_NAME / MANIFEST_NAME)
        self.assertEqual(timeout, brun.MANIFEST_READ_TIMEOUT_S)

    def test_a_ledger_that_cannot_be_saved_after_promote_still_names_the_snapshot(
        self,
    ) -> None:
        refused = OSError(errno.EACCES, "fixture ledger refused")
        with self.subTest("unconfirmed"):
            with (
                mock.patch.object(brun, "read_bytes_bounded", return_value=None),
                mock.patch.object(brun, "save_ledger", side_effect=refused),
            ):
                self.assertEqual(self.run_backup(self.request()), brun.EXIT_ATTENTION)
            record = self.last_run()
            self.assertEqual(
                (record.outcome, record.snapshot), (OUTCOME_FAILED, FIRST_NAME)
            )
            self.assertIn("promoted but not confirmed", record.detail)
            self.assertIn("fixture ledger refused", record.detail)
            self.assertNotIn("unexpected filesystem error", record.detail)
        with self.subTest("confirmed"):
            # A good snapshot the ledger does not hold is never pruned, so it must be named,
            # and prune must not run while the newest snapshot is not durably ledgered.
            self.clock = START + timedelta(seconds=1)
            name = f"2026-09-14T060001-{MACHINE}"
            with (
                mock.patch.object(brun, "save_ledger", side_effect=refused),
                mock.patch.object(brun, "prune") as prune,
            ):
                self.assertEqual(self.run_backup(self.request()), brun.EXIT_ATTENTION)
            record = self.last_run()
            self.assertEqual((record.outcome, record.snapshot), (OUTCOME_FAILED, name))
            self.assertIn("fixture ledger refused", record.detail)
            self.assertNotIn("unexpected filesystem error", record.detail)
            prune.assert_not_called()
            self.assertTrue((self.destination / name / MANIFEST_NAME).is_file())

    def test_a_cross_volume_promote_that_does_not_confirm_is_never_ledgered(
        self,
    ) -> None:
        real_promote = brun.promote

        def across_volumes(
            staging: Path,
            destination: Path,
            name: str,
            ledger: Ledger,
            *,
            save_ledger: Callable[[Ledger], None],
        ) -> "tuple[Path, Ledger]":
            def rename(source: Path, target: Path) -> None:
                if source == staging:
                    raise OSError(errno.EXDEV, "fixture cross-device link")
                os.rename(source, target)

            return real_promote(
                staging,
                destination,
                name,
                ledger,
                save_ledger=save_ledger,
                rename=rename,
            )

        with (
            mock.patch.object(brun, "promote", across_volumes),
            mock.patch.object(brun, "read_bytes_bounded", return_value=None),
        ):
            self.assertEqual(self.run_backup(self.request()), brun.EXIT_ATTENTION)
        record = self.last_run()
        self.assertEqual(
            (record.outcome, record.snapshot), (OUTCOME_FAILED, FIRST_NAME)
        )
        self.assertIn("promoted but not confirmed", record.detail)
        self.assertEqual(
            [path.name for path in self.destination.iterdir()], [FIRST_NAME]
        )
        self.assertTrue((self.destination / FIRST_NAME / MANIFEST_NAME).is_file())
        # Neither the incoming record nor the promoted entry: nothing unproven is ledgered.
        self.assertEqual(load_ledger(self.state).entries, ())
        self.assertEqual(list((self.base / brun.STAGING_DIR).iterdir()), [])

    def test_a_confirmed_snapshot_is_ledgered_before_prune_runs(self) -> None:
        # prune is the step with no undo; if the run dies inside it, the newest snapshot
        # must already be in the ledger on disk rather than known only in memory.
        with mock.patch.object(brun, "prune", side_effect=OSError("died in prune")):
            self.assertEqual(self.run_backup(self.request()), brun.EXIT_ATTENTION)
        self.assertIn("died in prune", self.last_run().detail)
        [entry] = load_ledger(self.state).entries
        self.assertEqual((entry.name, entry.complete), (FIRST_NAME, True))


class StagingTests(RunCase):
    def test_a_stale_staging_folder_that_cannot_be_swept_fails_before_building(
        self,
    ) -> None:
        stale = self.base / brun.STAGING_DIR / "stale"
        stale.mkdir(parents=True)
        (stale / "f").write_text(
            "left by a crashed run", encoding="utf-8", newline="\n"
        )
        stale.chmod(0o500)
        self.restore_permissions(stale)
        self.assertEqual(self.run_backup(self.request()), brun.EXIT_ATTENTION)
        record = self.last_run()
        self.assertEqual(record.outcome, OUTCOME_FAILED)
        self.assertIn("staging sweep failed", record.detail)
        self.assertEqual(self.memory_snapshot_calls(), [])
        self.assertFalse(self.destination.exists())
        self.assertEqual(len(self.notes), 1)

    def test_a_symlinked_staging_root_is_refused_and_its_target_untouched(self) -> None:
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "keep.txt").write_text(
            "not staging's", encoding="utf-8", newline="\n"
        )
        self.base.mkdir(parents=True)
        (self.base / brun.STAGING_DIR).symlink_to(elsewhere, target_is_directory=True)
        self.assertEqual(self.run_backup(self.request()), brun.EXIT_ATTENTION)
        self.assertIn("not a plain directory", self.last_run().detail)
        self.assertIn("refuses to sweep", self.last_run().detail)
        self.assertEqual([path.name for path in elsewhere.iterdir()], ["keep.txt"])
        self.assertEqual(self.memory_snapshot_calls(), [])

    def test_a_staging_root_that_cannot_be_examined_is_refused_by_name(self) -> None:
        staging_root = self.base / brun.STAGING_DIR
        real_lstat = Path.lstat

        def lstat(path: Path) -> os.stat_result:
            if path == staging_root:
                raise PermissionError(errno.EACCES, "fixture denied", str(path))
            return real_lstat(path)

        with mock.patch.object(Path, "lstat", lstat):
            self.assertEqual(self.run_backup(self.request()), brun.EXIT_ATTENTION)
        record = self.last_run()
        self.assertEqual(record.outcome, OUTCOME_FAILED)
        self.assertIn("refuses to sweep", record.detail)
        self.assertIn("fixture denied", record.detail)
        self.assertNotIn("unexpected filesystem error", record.detail)
        self.assertEqual(self.memory_snapshot_calls(), [])

    def test_a_staging_folder_the_sweep_left_is_never_built_into(self) -> None:
        leftover = self.base / brun.STAGING_DIR / FIRST_NAME
        leftover.mkdir(parents=True)
        (leftover / "planted").write_text("from before", encoding="utf-8", newline="\n")
        with mock.patch.object(bstage, "sweep_staging", return_value=()):
            self.assertEqual(self.run_backup(self.request()), brun.EXIT_ATTENTION)
        self.assertIn("already exists", self.last_run().detail)
        self.assertEqual([path.name for path in leftover.iterdir()], ["planted"])
        self.assertEqual(self.memory_snapshot_calls(), [])
        self.assertFalse(self.destination.exists())


class SnapshotRecordTests(RunCase):
    def test_a_restore_doc_refusal_is_a_failed_item_on_a_promoted_snapshot(
        self,
    ) -> None:
        projects = self.tree.request.roots.claude_home / "projects"
        fx.make_repo(projects / "-fence```slug", name="memory")
        self.assertEqual(self.run_backup(self.request()), brun.EXIT_ATTENTION)
        record = self.last_run()
        self.assertEqual(record.outcome, OUTCOME_INCOMPLETE)
        [failure] = record.failed
        self.assertEqual(failure.item, RESTORE_DOC_NAME)
        self.assertIn("could end a Markdown fence", failure.reason)
        folder = self.destination / FIRST_NAME
        self.assertFalse((folder / RESTORE_DOC_NAME).exists())
        manifest = parse_manifest((folder / MANIFEST_NAME).read_bytes())
        assert manifest is not None
        self.assertEqual((manifest.complete, manifest.failed), (False, (failure,)))
        self.assertFalse(load_ledger(self.state).entries[0].complete)

    def test_a_file_that_cannot_be_hashed_makes_the_records_incomplete(self) -> None:
        staging = self.tmp / "records"
        staging.mkdir()
        (staging / "a.txt").write_text("a", encoding="utf-8", newline="\n")
        (staging / "link").symlink_to(staging / "a.txt")
        build = BuildResult((), (), (), (), (), ())
        manifest = seal.write_snapshot_records(staging, START, build, ())
        self.assertFalse(manifest.complete)
        self.assertEqual(
            manifest.failed, (FailedItem("hash:link", "not a regular file"),)
        )
        self.assertEqual(
            [entry.name for entry in manifest.files], [RESTORE_DOC_NAME, "a.txt"]
        )
        self.assertIn("complete: no", (staging / RESTORE_DOC_NAME).read_text("utf-8"))
        written = (staging / MANIFEST_NAME).read_bytes()
        self.assertEqual(written, manifest_to_json(manifest).encode("utf-8"))
        doc = (staging / RESTORE_DOC_NAME).read_bytes()
        self.assertEqual(manifest.files[0].sha256, sha256_bytes(doc))
        self.assertEqual(manifest.files[0].size, len(doc))

    def test_a_verification_scratch_that_cannot_be_removed_is_a_failed_item(
        self,
    ) -> None:
        planted: list[Path] = []

        def plant_then_run(
            argv: Sequence[str], cwd: "Path | None", timeout: float
        ) -> "tuple[int, str, str]":
            # A restore runs in its own directory inside the run's scratch directory, so an
            # unremovable folder planted beside it outlives only the scratch removal.
            if cwd is not None and "clone" in argv and not planted:
                stuck = cwd.parent / "stuck"
                stuck.mkdir()
                (stuck / "f").write_text("unremovable", encoding="utf-8", newline="\n")
                stuck.chmod(0o500)
                self.restore_permissions(stuck)
                planted.append(stuck)
            return backup_proc.run_cmd(argv, cwd, timeout)

        code = self.run_backup(self.request(), real=plant_then_run)
        self.assertEqual(code, brun.EXIT_ATTENTION)
        record = self.last_run()
        self.assertEqual(
            (record.outcome, record.snapshot), (OUTCOME_INCOMPLETE, FIRST_NAME)
        )
        [failure] = record.failed
        self.assertEqual(failure.item, seal.VERIFY_SCRATCH_ITEM)
        self.assertIn("could not be removed", failure.reason)
        self.assertEqual(len(load_ledger(self.state).entries), 1)

    def test_a_verification_that_raises_and_scratch_that_stays_are_both_recorded(
        self,
    ) -> None:
        defect = ValueError("fixture defect")
        stuck = "fixture scratch could not be removed"
        with (
            mock.patch.object(seal, "verify_snapshot", side_effect=defect),
            mock.patch.object(seal, "_removal_problem", return_value=stuck),
        ):
            self.assertEqual(self.run_backup(self.request()), brun.EXIT_ATTENTION)
        detail = self.last_run().detail
        self.assertIn("internal error: ValueError: fixture defect", detail)
        self.assertIn(stuck, detail)

    def test_a_verification_that_raises_still_removes_its_scratch(self) -> None:
        defect = ValueError("fixture defect")
        with mock.patch.object(seal, "verify_snapshot", side_effect=defect):
            self.assertEqual(self.run_backup(self.request()), brun.EXIT_ATTENTION)
        # Removed, so the record carries the defect alone and no scratch is left behind.
        self.assertEqual(
            self.last_run().detail, "internal error: ValueError: fixture defect"
        )
        self.assertEqual(list(self.scratch_root.iterdir()), [])

    def test_no_scratch_directory_leaves_the_snapshot_promoted_but_unverified(
        self,
    ) -> None:
        with mock.patch.object(tempfile, "tempdir", str(self.tmp / "absent")):
            self.assertEqual(self.run_backup(self.request()), brun.EXIT_ATTENTION)
        record = self.last_run()
        self.assertEqual(record.outcome, OUTCOME_INCOMPLETE)
        [failure] = record.failed
        self.assertEqual(failure.item, seal.VERIFY_SCRATCH_ITEM)
        self.assertIn("nothing was verified", failure.reason)
        self.assertEqual([argv for argv in self.runner.calls if "clone" in argv], [])
        self.assertTrue((self.destination / FIRST_NAME / MANIFEST_NAME).is_file())


class ReportingTests(RunCase):
    def test_a_skip_that_cannot_be_recorded_needs_attention_and_is_posted(self) -> None:
        self.assertEqual(self.run_backup(self.request()), brun.EXIT_OK)
        (self.state / LAST_RUN_NAME).unlink()
        (self.state / LAST_RUN_NAME).mkdir()
        self.clock = START + timedelta(hours=1)
        due = replace(self.request(), if_due=True)
        self.assertEqual(self.run_backup(due), brun.EXIT_ATTENTION)
        [(title, message)] = self.notes
        self.assertEqual(title, brun.NOTIFY_TITLE)
        self.assertIn("last run not recorded", message)
        self.assertIn("last run not recorded", self.err.getvalue())

    def test_a_held_lock_writes_nothing_so_an_unwritable_state_posts_nothing(
        self,
    ) -> None:
        holder = RunLock(self.base / brun.LOCK_NAME)
        self.assertTrue(holder.acquire())
        self.addCleanup(holder.release)
        (self.base / STATE_DIR_NAME).write_text(
            "a file, not a directory", encoding="utf-8", newline="\n"
        )
        self.assertEqual(self.run_backup(self.request()), brun.EXIT_ATTENTION)
        self.assertEqual(self.notes, [])
        self.assertEqual(self.err.getvalue(), "skipped: another run holds the lock\n")

    def test_a_lock_that_cannot_be_taken_is_unrunnable_and_posted(self) -> None:
        self.base.write_text(
            "a file where the tool's directory belongs", encoding="utf-8", newline="\n"
        )
        self.assertEqual(self.run_backup(self.request()), brun.EXIT_UNRUNNABLE)
        [(_, message)] = self.notes
        self.assertIn("could not take the run lock", message)
        self.assertEqual(self.memory_snapshot_calls(), [])
        self.assertFalse(self.destination.exists())

    def test_an_exception_no_step_expects_is_recorded_printed_and_posted(self) -> None:
        defect = ValueError("fixture defect")
        with mock.patch.object(brun, "build_snapshot", side_effect=defect):
            self.assertEqual(self.run_backup(self.request()), brun.EXIT_ATTENTION)
        record = self.last_run()
        detail = "internal error: ValueError: fixture defect"
        self.assertEqual(
            (record.outcome, record.detail, record.snapshot),
            (OUTCOME_FAILED, detail, None),
        )
        self.assertEqual(
            self.notes, [(brun.NOTIFY_TITLE, f"{OUTCOME_FAILED}: {detail}")]
        )
        # Recorded and posted first, then said: the outcome line, then the traceback.
        err = self.err.getvalue()
        self.assertTrue(err.startswith(f"{OUTCOME_FAILED}: {detail}\n"), err)
        self.assertIn("Traceback (most recent call last)", err)
        # Only Exception is caught: an interrupt still stops the run, and still frees the lock.
        with mock.patch.object(brun, "build_snapshot", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_backup(self.request())
        after = RunLock(self.base / brun.LOCK_NAME)
        self.assertTrue(after.acquire())
        after.release()

    def test_a_defect_is_recorded_and_posted_even_when_stderr_cannot_be_written(
        self,
    ) -> None:
        # A manual run piped into a pager that quit, or a log that is gone: stderr is best
        # effort, and the record and the notification are the signals that must survive it.
        posted: list[list[str]] = []

        def osascript(argv: Sequence[str]) -> "tuple[int, str, str]":
            posted.append(fx.osascript_paths(argv))
            return 0, "", ""

        runner = fx.FakeRunner({**fx.MEMORY_OK, "osascript": osascript})
        deps = brun.RunDeps(
            run=runner,
            now=lambda: self.clock,
            which=lambda name: FIXTURE_GIT,
            notify=lambda title, text: backup_notify.notify(title, text, run=runner),
            hostname=lambda: FIXTURE_HOST,
            out=io.StringIO(),
            err=_UnwritableStream(),
        )
        defect = ValueError("fixture defect")
        with mock.patch.object(brun, "build_snapshot", side_effect=defect):
            self.assertEqual(brun.run_backup(self.request(), deps), brun.EXIT_ATTENTION)
        detail = "internal error: ValueError: fixture defect"
        self.assertEqual(self.last_run().detail, detail)
        self.assertEqual(posted, [[brun.NOTIFY_TITLE, f"{OUTCOME_FAILED}: {detail}"]])

    def test_a_notification_that_is_not_posted_is_said_on_stderr(self) -> None:
        self.notify_accepts = False
        request = replace(self.request(), platform="linux")
        self.assertEqual(self.run_backup(request), brun.EXIT_UNRUNNABLE)
        self.assertEqual(len(self.notes), 1)
        self.assertIn("notification could not be posted", self.err.getvalue())

    def test_a_dry_run_that_cannot_run_or_list_memory_says_so_and_writes_nothing(
        self,
    ) -> None:
        linux = replace(self.request(), dry_run=True, platform="linux")
        self.assertEqual(self.run_backup(linux), brun.EXIT_UNRUNNABLE)
        self.assertIn("cannot run: macOS only", self.err.getvalue())
        projects = self.tree.request.roots.claude_home / "projects"
        projects.chmod(0o000)
        self.restore_permissions(projects)
        dry = replace(self.request(), dry_run=True)
        self.assertEqual(self.run_backup(dry), brun.EXIT_ATTENTION)
        self.assertIn("memory repositories: cannot list:", self.out.getvalue())
        self.assertFalse(self.base.exists())
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.notes, [])


if __name__ == "__main__":
    unittest.main()
