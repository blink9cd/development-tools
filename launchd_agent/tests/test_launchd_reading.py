"""Grouped suite: launchd_agent's reads, names and plist, against a scripted launchctl.

The names launchctl is handed, the upward library search, one ``launchctl print``'s three
answers, the wait that polls it, and the keep-alive plist. Nothing here loads or unloads an
agent, which is why it is apart from the lifecycle suite.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from collections.abc import Mapping
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import launchd_agent
from launchd_agent import AgentState, LaunchdError
from _launchd_fixtures import (
    ABSENT,
    Answer,
    FakeLaunchctl,
    LABEL,
    LOADED,
    PRINT,
    TARGET,
    UID,
)


class ServiceTargetTest(unittest.TestCase):
    """The names launchctl is handed."""

    def test_target_is_the_gui_domain_and_label(self) -> None:
        self.assertEqual(TARGET, launchd_agent.service_target(UID, LABEL))

    def test_kickstart_argv_names_the_target(self) -> None:
        self.assertEqual(
            [launchd_agent.LAUNCHCTL, "kickstart", TARGET],
            launchd_agent.kickstart_argv(UID, LABEL),
        )

    def test_a_label_that_could_name_another_target_is_refused(self) -> None:
        # A slash would reach past the label into another path; a leading dash reads as an
        # option; spaces and controls are no label launchd writes.
        for label in ("", "../other", "local.a/b", "-local.a", "local a", "local\na"):
            with self.subTest(label=label), self.assertRaises(ValueError):
                launchd_agent.service_target(UID, label)

    def test_every_verb_refuses_a_bad_label_before_launchctl_runs(self) -> None:
        bad = "local.a/../b"
        with tempfile.TemporaryDirectory() as tmp:
            plist_path = Path(tmp) / "agent.plist"
            fake = FakeLaunchctl(self)
            with self.assertRaises(ValueError):
                launchd_agent.install_agent(
                    fake.ctl(), label=bad, plist_path=plist_path, plist=b"<plist/>"
                )
            self.assertFalse(plist_path.exists())
            plist_path.write_bytes(b"<plist/>")
            with self.assertRaises(ValueError):
                launchd_agent.uninstall_agent(
                    fake.ctl(), label=bad, plist_path=plist_path
                )
            self.assertTrue(plist_path.exists())
        with self.assertRaises(ValueError):
            launchd_agent.agent_state(fake.ctl(), bad)
        with self.assertRaises(ValueError):
            launchd_agent.kickstart_argv(UID, bad)
        self.assertEqual([], fake.calls)


class LibrarySearchTest(unittest.TestCase):
    """The upward search for the shared libraries."""

    def test_a_tree_without_the_library_finds_none(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            start = Path(folder) / "a" / "b.py"
            self.assertIsNone(launchd_agent._nearest_library("atomic_write", start))

    def test_finds_the_nearest_holding_folder(self) -> None:
        start = Path(launchd_agent.__file__).resolve()
        found = launchd_agent._nearest_library("atomic_write", start)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertTrue((found / "atomic_write.py").is_file())

    @staticmethod
    def _plant(folder: Path, name: str) -> Path:
        """Create ``<folder>/<name>/<name>.py`` and return that library folder."""
        library = folder / name
        library.mkdir(parents=True)
        (library / f"{name}.py").write_text("", encoding="utf-8", newline="\n")
        return library

    def test_finds_a_library_beside_its_folder_in_a_flattened_export(self) -> None:
        # An export ships each vendored library in a folder of its own name directly
        # under the export root, with no utilities/ folder above it.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve() / "export"
            library = self._plant(root, "atomic_write")
            start = root / "launchd_agent" / "launchd_agent.py"
            self.assertEqual(
                library, launchd_agent._nearest_library("atomic_write", start)
            )

    def test_finds_a_library_in_the_monorepo_layout(self) -> None:
        # Records every file the search probes, so the test pins where it looks and not
        # only what it returns: a library is the module folder's sibling, so the module's
        # own folder and its parent are the only places read, one candidate each.
        with tempfile.TemporaryDirectory() as folder:
            utilities = Path(folder).resolve() / "repo" / "utilities"
            library = self._plant(utilities, "atomic_write")
            own = utilities / "launchd_agent"
            probed: list[Path] = []
            real_is_file = Path.is_file

            def recording_is_file(path: Path) -> bool:
                probed.append(path)
                return real_is_file(path)

            with mock.patch.object(Path, "is_file", recording_is_file):
                found = launchd_agent._nearest_library(
                    "atomic_write", own / "launchd_agent.py"
                )
            self.assertEqual(library, found)
            self.assertEqual(
                [
                    own / "atomic_write" / "atomic_write.py",
                    utilities / "atomic_write" / "atomic_write.py",
                ],
                probed,
            )

    def test_never_searches_above_the_folder_holding_its_own(self) -> None:
        # The library is absent where this module's layout puts it, and a same-named
        # folder sits higher up, as it could in a folder above a clone. A search that
        # climbed there would put a stranger's folder on sys.path.
        with tempfile.TemporaryDirectory() as folder:
            above = Path(folder).resolve()
            self._plant(above / "utilities", "atomic_write")
            self._plant(above, "atomic_write")
            start = above / "clone" / "launchd_agent" / "launchd_agent.py"
            self.assertIsNone(launchd_agent._nearest_library("atomic_write", start))


class AgentStateTest(unittest.TestCase):
    """One ``launchctl print`` has three answers, not two."""

    def _state(self, answer: Answer) -> AgentState:
        fake = FakeLaunchctl(self).expect(PRINT, answer)
        state = launchd_agent.agent_state(fake.ctl(), LABEL)
        self.assertEqual([launchd_agent.LAUNCHCTL_TIMEOUT_S], fake.timeouts)
        return state

    def test_exit_zero_is_loaded(self) -> None:
        self.assertIs(AgentState.LOADED, self._state(LOADED))

    def test_exit_113_is_absent(self) -> None:
        self.assertIs(AgentState.ABSENT, self._state(ABSENT))

    def test_any_other_exit_is_unknown(self) -> None:
        self.assertIs(AgentState.UNKNOWN, self._state((5, "", "Input/output error\n")))

    def test_a_timeout_is_unknown(self) -> None:
        timeout = subprocess.TimeoutExpired(PRINT, launchd_agent.LAUNCHCTL_TIMEOUT_S)
        self.assertIs(AgentState.UNKNOWN, self._state(timeout))

    def test_a_launchctl_that_cannot_run_is_unknown(self) -> None:
        missing = FileNotFoundError(2, "No such file or directory")
        self.assertIs(AgentState.UNKNOWN, self._state(missing))

    # print_service (agent_state is its .state): stdout when loaded, reason when unknown.
    def _print(self, answer: Answer) -> launchd_agent.ServicePrint:
        fake = FakeLaunchctl(self).expect(PRINT, answer)
        return launchd_agent.print_service(fake.ctl(), LABEL)

    def test_loaded_keeps_stdout(self) -> None:
        got = self._print((0, "\tstate = running\n\tpid = 42\n", ""))
        self.assertIn("pid = 42", got.stdout)

    def test_absent_has_no_stdout(self) -> None:
        got = self._print((launchd_agent.PRINT_ABSENT_RC, "noise", ""))
        self.assertEqual((got.state, got.stdout), (AgentState.ABSENT, ""))

    def test_odd_exit_is_unknown_with_reason(self) -> None:
        got = self._print((5, "", "boom\nmore"))
        self.assertEqual((got.stdout, "boom" in got.why), ("", True))


class WaitForTest(unittest.TestCase):
    """Polling until launchd reports the wanted state."""

    def test_succeeds_after_two_polls(self) -> None:
        fake = FakeLaunchctl(self).expect(PRINT, LOADED).expect(PRINT, ABSENT)
        launchd_agent.wait_for(fake.ctl(), LABEL, AgentState.ABSENT)
        self.assertEqual([launchd_agent.POLL_S], fake.sleeps)
        self.assertEqual([], fake.script)

    def test_unknown_fails_at_once_naming_the_exit_and_stderr(self) -> None:
        fake = FakeLaunchctl(self).expect(PRINT, (5, "", "Input/output error\nmore\n"))
        with self.assertRaises(LaunchdError) as caught:
            launchd_agent.wait_for(fake.ctl(), LABEL, AgentState.LOADED)
        message = str(caught.exception)
        self.assertIn("exit 5", message)
        self.assertIn("Input/output error", message)
        self.assertNotIn("more", message)
        self.assertEqual([], fake.sleeps)

    def test_a_timeout_fails_at_once_and_says_so(self) -> None:
        timeout = subprocess.TimeoutExpired(PRINT, launchd_agent.LAUNCHCTL_TIMEOUT_S)
        fake = FakeLaunchctl(self).expect(PRINT, timeout)
        with self.assertRaises(LaunchdError) as caught:
            launchd_agent.wait_for(fake.ctl(), LABEL, AgentState.LOADED)
        self.assertIn("timed out", str(caught.exception))

    def test_unknown_with_empty_stderr_still_names_the_exit(self) -> None:
        fake = FakeLaunchctl(self).expect(PRINT, (5, "", ""))
        with self.assertRaises(LaunchdError) as caught:
            launchd_agent.wait_for(fake.ctl(), LABEL, AgentState.LOADED)
        self.assertIn("exit 5", str(caught.exception))

    def test_the_deadline_fails_with_the_state_still_read(self) -> None:
        fake = FakeLaunchctl(self)
        for _ in range(3):
            fake.expect(PRINT, LOADED)
        with self.assertRaises(LaunchdError) as caught:
            launchd_agent.wait_for(fake.ctl(), LABEL, AgentState.ABSENT, deadline_s=1.0)
        self.assertEqual(f"{LABEL}: still loaded after 1 s", str(caught.exception))
        self.assertEqual([], fake.script)

    def test_unknown_is_not_a_state_to_wait_for(self) -> None:
        fake = FakeLaunchctl(self)
        with self.assertRaises(ValueError):
            launchd_agent.wait_for(fake.ctl(), LABEL, AgentState.UNKNOWN)
        self.assertEqual([], fake.calls)


class BuildKeepalivePlistTest(unittest.TestCase):
    """The always-running agent's plist."""

    def _build(
        self,
        label: str,
        keep_alive: bool | Mapping[str, bool] = True,
    ) -> bytes:
        return launchd_agent.build_keepalive_plist(
            label=label,
            program_arguments=[
                "/usr/bin/python3",
                "/opt/tool/serve.py",
                "--mode",
                "serve",
            ],
            working_directory="/opt/tool",
            log_paths=("/opt/tool/logs/out.log", "/opt/tool/logs/err.log"),
            path_value="/usr/bin:/bin",
            keep_alive=keep_alive,
        )

    def test_round_trips_to_the_exact_dictionary(self) -> None:
        expected = {
            "Label": LABEL,
            "ProgramArguments": [
                "/usr/bin/python3",
                "/opt/tool/serve.py",
                "--mode",
                "serve",
            ],
            "WorkingDirectory": "/opt/tool",
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "ProcessType": "Background",
            "StandardOutPath": "/opt/tool/logs/out.log",
            "StandardErrorPath": "/opt/tool/logs/err.log",
            "EnvironmentVariables": {"PATH": "/usr/bin:/bin"},
        }
        self.assertEqual(expected, plistlib.loads(self._build(LABEL)))

    def test_restarts_on_every_exit_unless_told_otherwise(self) -> None:
        plist = plistlib.loads(self._build(LABEL))
        self.assertIs(plist["KeepAlive"], True)

    def test_restarts_only_after_a_failed_exit_when_asked(self) -> None:
        # launchd reads SuccessfulExit false as: restart after a nonzero exit or a crash,
        # and leave the agent stopped after exit 0, so a permanent refusal can end a loop.
        built = self._build(LABEL, keep_alive=launchd_agent.RESTART_AFTER_FAILURE)
        self.assertEqual({"SuccessfulExit": False}, plistlib.loads(built)["KeepAlive"])

    def test_the_restart_policy_constant_cannot_be_changed_in_place(self) -> None:
        # Typed as a Mapping, so only a cast lets the test try the write it must refuse.
        policy = cast("dict[str, bool]", launchd_agent.RESTART_AFTER_FAILURE)
        with self.assertRaises(TypeError):
            policy["SuccessfulExit"] = True

    def test_refuses_a_label_with_a_slash(self) -> None:
        with self.assertRaises(ValueError):
            self._build("local/escape")
