#!/usr/bin/env python3
"""A built snapshot restores; a corrupted or incomplete one is caught before it is trusted.

Memory corpora are never snapshotted for real here: every build routes memory_snapshot
through a canned FakeRunner answer, the tools directory it would run from does not exist,
and the interpreter is an obvious fake. Every repository is a fixture under a temp dir.
"""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import _fixtures as fx

import backup_build as bld
import backup_proc
import backup_verify as bv
from backup_model import FileEntry, WorktreeHead


class _Tree(unittest.TestCase):
    """A fixture workspace, Claude home and home, plus a request to build them."""

    def setUp(self) -> None:
        self.enterContext(fx.isolated_git_env())
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        tree = fx.make_tree(self.tmp)
        self.workspace, self.home, self.staging = (
            tree.workspace,
            tree.home,
            tree.staging,
        )
        self.request = tree.request


class BuildTests(_Tree):
    def test_a_full_build_produces_every_item(self) -> None:
        runner = fx.FakeRunner(fx.MEMORY_OK)
        result = bld.build_snapshot(self.request, run=runner)
        self.assertEqual(result.failed, ())
        self.assertEqual(
            [b.name for b in result.bundles],
            ["workspace.bundle", "memory/-fixture-proj.bundle"],
        )
        self.assertEqual(
            [m.name for m in result.archive_members],
            ["workspace/.notes/f.md", "claude/settings.json", "home/.zshrc"],
        )
        self.assertEqual(result.unclassified_ignored, ("new_folder",))
        self.assertIn(
            [
                "/opt/fixture-python",
                str(self.tmp / "tools" / "memory_snapshot" / "memory_snapshot.py"),
                "--mode",
                "snapshot",
                "--home",
                str(self.home),
            ],
            runner.calls,
        )

    def test_a_failed_memory_snapshot_is_recorded_and_bundling_continues(self) -> None:
        for routes, reason in (
            (
                {"memory_snapshot.py": lambda argv: (1, "", "line one\nboom\n")},
                "exit 1: boom",
            ),
            ({"memory_snapshot.py": lambda argv: (2, "", "")}, "exit 2: no output"),
        ):
            with self.subTest(reason=reason):
                request = replace(
                    self.request, staging=self.tmp / f"staging-{reason[5]}"
                )
                result = bld.build_snapshot(request, run=fx.FakeRunner(routes))
                self.assertEqual(
                    [(f.item, f.reason) for f in result.failed],
                    [("memory_snapshot", reason)],
                )
                self.assertEqual(len(result.bundles), 2)

    def test_with_memory_snapshot_off_the_step_never_runs_and_corpora_still_bundle(
        self,
    ) -> None:
        """Off, memory_snapshot is never called: no call, no failed item.

        Its route here fails, so a call would show as a failed item as well as a call. The
        corpora are bundled all the same: the key gates the pre-step, not the bundling.
        """
        request = replace(
            self.request, config=replace(self.request.config, memory_snapshot=False)
        )
        runner = fx.FakeRunner({"memory_snapshot.py": lambda argv: (1, "", "boom")})
        result = bld.build_snapshot(request, run=runner)
        self.assertEqual(result.failed, ())
        self.assertEqual(
            [b.name for b in result.bundles],
            ["workspace.bundle", "memory/-fixture-proj.bundle"],
        )
        self.assertFalse(
            [call for call in runner.calls if "memory_snapshot" in " ".join(call)]
        )

    def test_dirty_and_drift_failures_are_items_not_crashes(self) -> None:
        for fragment, item in (
            ("worktree list", "dirty_worktrees"),
            ("--ignored=matching", "drift"),
        ):
            with self.subTest(item=item):
                request = replace(self.request, staging=self.tmp / f"staging-{item}")
                runner = fx.FakeRunner(fx.MEMORY_OK, real=fx.failing_when(fragment))
                result = bld.build_snapshot(request, run=runner)
                self.assertIn(item, [f.item for f in result.failed])

    def test_a_workspace_bundle_failure_is_an_item(self) -> None:
        runner = fx.FakeRunner(fx.MEMORY_OK, real=fx.failing_when("bundle create"))
        result = bld.build_snapshot(self.request, run=runner)
        self.assertIn("workspace", [f.item for f in result.failed])

    def test_an_archive_entry_that_fails_is_a_build_failure(self) -> None:
        config = replace(self.request.config, home_files=(".zshrc", "absent-rc"))
        request = replace(self.request, config=config)
        result = bld.build_snapshot(request, run=fx.FakeRunner(fx.MEMORY_OK))
        self.assertEqual(
            [(f.item, f.reason) for f in result.failed],
            [("home:absent-rc", "matched no files")],
        )
        self.assertIn("home/.zshrc", [m.name for m in result.archive_members])

    def test_no_projects_directory_means_no_memory_repos(self) -> None:
        self.assertEqual(bld.memory_repos(self.tmp / "no-claude"), ())

    def test_a_project_provably_holding_no_repository_is_passed_over(self) -> None:
        claude_home = self.request.roots.claude_home
        (claude_home / "projects" / "-no-memory").mkdir()
        (claude_home / "projects" / "-memory-file").mkdir()
        (claude_home / "projects" / "-memory-file" / "memory").write_text(
            "not a repository", encoding="utf-8", newline="\n"
        )
        self.assertEqual(
            [slug for slug, _ in bld.memory_repos(claude_home)],
            ["-empty", "-fixture-proj"],
        )

    def test_every_command_runs_under_its_named_bound(self) -> None:
        bounds: set[tuple[str, float]] = set()
        inner = fx.FakeRunner(fx.MEMORY_OK)

        def recording(
            argv: Sequence[str], cwd: "Path | None", timeout: float
        ) -> "tuple[int, str, str]":
            bounds.add((argv[0], timeout))
            return inner(argv, cwd, timeout)

        self.assertEqual(bld.build_snapshot(self.request, run=recording).failed, ())
        # Every call is one of exactly these two: the interpreter under memory_snapshot's
        # bound, or git under git's.
        self.assertEqual(
            bounds,
            {
                ("/opt/fixture-python", backup_proc.MEMORY_SNAPSHOT_TIMEOUT_S),
                ("git", backup_proc.GIT_TIMEOUT_S),
            },
        )

    def test_a_failed_bundle_is_never_listed_even_when_its_file_is_on_disk(
        self,
    ) -> None:
        # list-heads runs after the bundle is written, so its failure leaves a whole file.
        runner = fx.FakeRunner(fx.MEMORY_OK, real=fx.failing_when("list-heads"))
        result = bld.build_snapshot(self.request, run=runner)
        self.assertTrue((self.staging / "workspace.bundle").is_file())
        self.assertEqual(result.bundles, ())
        reasons = {f.item: f.reason for f in result.failed}
        self.assertEqual(sorted(reasons), ["memory:-fixture-proj", "workspace"])
        for item, name in (
            ("workspace", "workspace.bundle"),
            ("memory:-fixture-proj", "memory/-fixture-proj.bundle"),
        ):
            self.assertTrue(
                reasons[item].endswith(f"; unverified bundle kept at {name}"),
                reasons[item],
            )

    def test_a_staging_path_that_cannot_hold_files_is_recorded_not_raised(
        self,
    ) -> None:
        self.staging.write_text(
            "a file, not a directory", encoding="utf-8", newline="\n"
        )
        result = bld.build_snapshot(self.request, run=fx.FakeRunner(fx.MEMORY_OK))
        self.assertEqual(result.bundles, ())
        self.assertEqual(result.archive_members, ())
        self.assertEqual(
            sorted(f.item for f in result.failed),
            ["extras.zip", "memory:-fixture-proj", "workspace"],
        )
        # No bundle file could be written, so none is named as kept.
        self.assertFalse(any("kept" in f.reason for f in result.failed))

    def test_unreadable_memory_projects_are_failures_never_absences(self) -> None:
        projects = self.request.roots.claude_home / "projects"
        locked = fx.make_repo(projects / "-locked", name="memory").parent
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        result = bld.build_snapshot(self.request, run=fx.FakeRunner(fx.MEMORY_OK))
        self.assertIn("memory:-locked", [f.item for f in result.failed])
        self.assertEqual(
            [b.name for b in result.bundles],
            ["workspace.bundle", "memory/-fixture-proj.bundle"],
        )
        projects.chmod(0)
        self.addCleanup(projects.chmod, 0o700)
        request = replace(self.request, staging=self.tmp / "staging-unlisted")
        result = bld.build_snapshot(request, run=fx.FakeRunner(fx.MEMORY_OK))
        self.assertIn("memory", [f.item for f in result.failed])
        self.assertEqual([b.name for b in result.bundles], ["workspace.bundle"])


class VerifyTests(_Tree):
    def setUp(self) -> None:
        super().setUp()
        self.result = bld.build_snapshot(self.request, run=fx.FakeRunner(fx.MEMORY_OK))
        self.scratch = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.workspace_entry = self.result.bundles[0]

    def test_a_good_snapshot_verifies_clean(self) -> None:
        self.assertEqual(
            bv.verify_snapshot(
                self.staging,
                self.result.bundles,
                self.result.archive_members,
                self.scratch,
                run=backup_proc.run_cmd,
            ),
            (),
        )

    def test_corrupted_pack_data_is_caught(self) -> None:
        bundle = self.staging / "workspace.bundle"
        data = bytearray(bundle.read_bytes())
        middle = len(data) // 2
        data[middle : middle + 64] = b"\xff" * 64
        bundle.write_bytes(bytes(data))
        failure = bv.verify_bundle(
            self.staging, self.workspace_entry, self.scratch, run=backup_proc.run_cmd
        )
        assert failure is not None
        self.assertRegex(failure.reason, "clone failed|fsck failed")

    def test_each_restore_check_has_its_own_failure(self) -> None:
        entry = self.workspace_entry
        ghost_head = (WorktreeHead("ghost", "0" * 40, False),)
        cases = (
            ("fsck", replace(entry), fx.failing_when("fsck"), "fsck failed"),
            (
                "refs query",
                replace(entry),
                fx.failing_when("for-each-ref"),
                "for-each-ref failed",
            ),
            (
                "missing ref",
                replace(entry, refs=entry.refs + ("refs/heads/ghost",)),
                backup_proc.run_cmd,
                "lacks refs: refs/heads/ghost",
            ),
            (
                "worktree head",
                replace(entry, worktree_heads=ghost_head),
                backup_proc.run_cmd,
                "worktree head ghost",
            ),
        )
        for index, (label, case_entry, run, expected) in enumerate(cases):
            with self.subTest(label):
                # Named by position, not label: a scratch path holding "fsck" would put the
                # fragment in the clone's own argv, failing the clone instead of the fsck.
                scratch = self.scratch / f"case-{index}"
                scratch.mkdir()
                failure = bv.verify_bundle(self.staging, case_entry, scratch, run=run)
                assert failure is not None
                self.assertIn(expected, failure.reason)

    def test_the_archive_checks(self) -> None:
        archive = self.staging / "extras.zip"
        members = self.result.archive_members
        self.assertIsNone(bv.verify_archive(archive, members))
        wrong = (replace(members[0], sha256="0" * 64),)
        self.assertIn(
            "differs", (bv.verify_archive(archive, wrong) or self.fail()).reason
        )
        ghost = (FileEntry("workspace/ghost", "0" * 64, 1),)
        self.assertIn(
            "unreadable", (bv.verify_archive(archive, ghost) or self.fail()).reason
        )
        garbage = self.tmp / "garbage.zip"
        garbage.write_bytes(b"not a zip")
        self.assertIn(
            "unreadable", (bv.verify_archive(garbage, ()) or self.fail()).reason
        )

    def test_a_crc_mismatch_is_reported_by_member(self) -> None:
        stored = self.tmp / "stored.zip"
        with zipfile.ZipFile(stored, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("home/x", b"A" * 100)
        data = bytearray(stored.read_bytes())
        offset = data.index(b"A" * 100)
        data[offset] = ord("B")
        stored.write_bytes(bytes(data))
        failure = bv.verify_archive(stored, ())
        assert failure is not None
        self.assertIn("corrupt member: home/x", failure.reason)

    def test_file_hashes_exclude_the_manifest_and_use_posix_names(self) -> None:
        (self.staging / "MANIFEST.json").write_text(
            "{}", encoding="utf-8", newline="\n"
        )
        hashed = bv.hash_snapshot_files(self.staging, exclude="MANIFEST.json")
        self.assertEqual(hashed.failed, ())
        self.assertEqual(
            [entry.name for entry in hashed.files],
            ["extras.zip", "memory/-fixture-proj.bundle", "workspace.bundle"],
        )

    def test_verify_snapshot_collects_every_failure(self) -> None:
        (self.staging / "extras.zip").write_bytes(b"not a zip")
        failures = bv.verify_snapshot(
            self.staging,
            (replace(self.workspace_entry, refs=("refs/heads/ghost",)),),
            self.result.archive_members,
            self.scratch,
            run=backup_proc.run_cmd,
        )
        self.assertEqual(
            [f.item for f in failures], ["verify:workspace.bundle", "verify:extras.zip"]
        )


class MemoryToolsDirTests(unittest.TestCase):
    """Where memory_snapshot is looked for: the folder the tool's own folder sits in."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def script_at(self, *parts: str) -> Path:
        """A stand-in entry script at ``tmp/<parts>/git_workspace_backup.py``."""
        script = self.tmp.joinpath(*parts, "git_workspace_backup.py")
        script.parent.mkdir(parents=True)
        script.write_text("", encoding="utf-8", newline="\n")
        return script

    def test_an_ancestors_utilities_is_never_searched(self) -> None:
        memory = self.tmp / "utilities" / "memory_snapshot" / "memory_snapshot.py"
        memory.parent.mkdir(parents=True)
        memory.write_text("", encoding="utf-8", newline="\n")
        script = self.script_at("projects", "development_tools", "git_workspace_backup")
        self.assertEqual(
            bld.memory_tools_dir(script), self.tmp / "projects" / "development_tools"
        )

    def test_the_folder_above_the_tools_own_is_the_only_place_looked(self) -> None:
        # A utilities/ folder further up, here one holding a memory_snapshot folder, is not.
        (self.tmp / "utilities" / "memory_snapshot").mkdir(parents=True)
        script = self.script_at("tools", "git_workspace_backup")
        self.assertEqual(bld.memory_tools_dir(script), self.tmp / "tools")


if __name__ == "__main__":
    unittest.main()
