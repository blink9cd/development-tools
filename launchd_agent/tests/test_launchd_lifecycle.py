"""Grouped suite: install, uninstall and stop, each step confirmed by the read-back.

Every launchctl call is answered by the scripted fake, so a test pins the order of the calls
and that nothing else ran; temporary folders stand in for ``~/Library/LaunchAgents``. The one
live test runs only under the owner's ``LAUNCHD_AGENT_LIVE=1`` opt-in.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import launchd_agent
from launchd_agent import AgentState, LaunchdError
from _launchd_fixtures import (
    ABSENT,
    BOOTOUT,
    FakeLaunchctl,
    LABEL,
    LOADED,
    OK,
    PRINT,
    UID,
)


#: The owner's opt-in for the one test that mutates the real launchd.
_LIVE = os.environ.get("LAUNCHD_AGENT_LIVE") == "1"


_LIVE_LABEL = "local.launchd-agent.selftest"


class InstallAgentTest(unittest.TestCase):
    """Write the plist, boot out, wait absent, bootstrap, wait loaded."""

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        # A missing LaunchAgents folder, as on an account that never had an agent.
        self.plist_path = Path(folder.name) / "LaunchAgents" / f"{LABEL}.plist"
        self.bootstrap = [
            launchd_agent.LAUNCHCTL,
            "bootstrap",
            f"gui/{UID}",
            str(self.plist_path),
        ]

    def _install(self, fake: FakeLaunchctl) -> None:
        launchd_agent.install_agent(
            fake.ctl(), label=LABEL, plist_path=self.plist_path, plist=b"<plist/>"
        )

    def test_runs_the_four_steps_in_order_with_the_plist_written_first(self) -> None:
        seen: list[bool] = []
        fake = FakeLaunchctl(self)
        fake.before_each = lambda: seen.append(self.plist_path.is_file())
        fake.expect(BOOTOUT, (3, "", "Boot-out failed: 3: No such process\n"))
        fake.expect(PRINT, ABSENT).expect(self.bootstrap, OK).expect(PRINT, LOADED)
        self._install(fake)
        self.assertEqual([BOOTOUT, PRINT, self.bootstrap, PRINT], fake.calls)
        self.assertEqual([True] * 4, seen)
        self.assertEqual(b"<plist/>", self.plist_path.read_bytes())
        if sys.platform != "win32":
            mode = stat.S_IMODE(self.plist_path.stat().st_mode)
            self.assertEqual(0o644, mode)

    def test_a_failed_bootstrap_still_succeeds_when_print_reads_loaded(self) -> None:
        fake = FakeLaunchctl(self).expect(BOOTOUT, OK).expect(PRINT, ABSENT)
        fake.expect(
            self.bootstrap, (5, "", "Bootstrap failed: 5: Input/output error\n")
        )
        fake.expect(PRINT, LOADED)
        self._install(fake)
        self.assertEqual([], fake.script)

    def test_a_bootout_that_times_out_is_not_the_check(self) -> None:
        timeout = subprocess.TimeoutExpired(BOOTOUT, launchd_agent.LAUNCHCTL_TIMEOUT_S)
        fake = FakeLaunchctl(self).expect(BOOTOUT, timeout).expect(PRINT, ABSENT)
        fake.expect(self.bootstrap, OK).expect(PRINT, LOADED)
        self._install(fake)
        self.assertEqual([], fake.script)

    def test_an_agent_that_never_loads_fails(self) -> None:
        fake = FakeLaunchctl(self).expect(BOOTOUT, OK).expect(PRINT, ABSENT)
        fake.expect(self.bootstrap, OK).expect(PRINT, (5, "", "boom\n"))
        with self.assertRaises(LaunchdError) as caught:
            self._install(fake)
        self.assertNotIn("bootstrap failed", str(caught.exception))
        self.assertTrue(self.plist_path.is_file())

    def test_a_failed_bootstrap_that_never_loads_names_its_exit_and_stderr(
        self,
    ) -> None:
        fake = FakeLaunchctl(self).expect(BOOTOUT, OK).expect(PRINT, ABSENT)
        fake.expect(
            self.bootstrap, (5, "", "Bootstrap failed: 5: Input/output error\n")
        )
        for _ in range(3):
            fake.expect(PRINT, ABSENT)
        with self.assertRaises(LaunchdError) as caught:
            launchd_agent.install_agent(
                fake.ctl(),
                label=LABEL,
                plist_path=self.plist_path,
                plist=b"<plist/>",
                deadline_s=1.0,
            )
        self.assertEqual(
            f"{LABEL}: still absent after 1 s; launchctl bootstrap failed (exit 5): "
            "Bootstrap failed: 5: Input/output error",
            str(caught.exception),
        )
        self.assertIsInstance(caught.exception.__cause__, LaunchdError)

    def test_a_failed_bootstrap_with_only_stdout_names_stdout(self) -> None:
        fake = FakeLaunchctl(self).expect(BOOTOUT, OK).expect(PRINT, ABSENT)
        fake.expect(self.bootstrap, (5, "the reason\nmore\n", ""))
        fake.expect(PRINT, (7, "", "boom\n"))
        with self.assertRaises(LaunchdError) as caught:
            self._install(fake)
        self.assertTrue(str(caught.exception).endswith("(exit 5): the reason"))

    def test_a_bootstrap_that_times_out_and_never_loads_keeps_the_wait_message(
        self,
    ) -> None:
        timeout = subprocess.TimeoutExpired(self.bootstrap, 10.0)
        fake = FakeLaunchctl(self).expect(BOOTOUT, OK).expect(PRINT, ABSENT)
        fake.expect(self.bootstrap, timeout).expect(PRINT, (7, "", "boom\n"))
        with self.assertRaises(LaunchdError) as caught:
            self._install(fake)
        self.assertNotIn("bootstrap failed", str(caught.exception))


class UninstallAgentTest(unittest.TestCase):
    """Boot out, wait absent, and only then remove the plist."""

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.plist_path = Path(folder.name) / f"{LABEL}.plist"
        self.plist_path.write_bytes(b"<plist/>")

    def _uninstall(self, fake: FakeLaunchctl) -> None:
        launchd_agent.uninstall_agent(
            fake.ctl(), label=LABEL, plist_path=self.plist_path, deadline_s=1.0
        )

    def test_removes_the_plist_once_launchd_confirms_absent(self) -> None:
        fake = FakeLaunchctl(self).expect(BOOTOUT, OK).expect(PRINT, ABSENT)
        self._uninstall(fake)
        self.assertFalse(self.plist_path.exists())
        self.assertEqual([BOOTOUT, PRINT], fake.calls)

    def test_keeps_the_plist_when_the_wait_fails(self) -> None:
        fake = FakeLaunchctl(self).expect(BOOTOUT, OK)
        for _ in range(3):
            fake.expect(PRINT, LOADED)
        with self.assertRaises(LaunchdError):
            self._uninstall(fake)
        self.assertTrue(self.plist_path.is_file())

    def test_an_absent_plist_is_not_an_error(self) -> None:
        self.plist_path.unlink()
        fake = FakeLaunchctl(self).expect(BOOTOUT, ABSENT).expect(PRINT, ABSENT)
        self._uninstall(fake)
        self.assertFalse(self.plist_path.exists())


class StopAgentTest(unittest.TestCase):
    """Boot out and wait absent, keeping the plist for the next load."""

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.plist_path = Path(folder.name) / f"{LABEL}.plist"
        self.plist_path.write_bytes(b"<plist/>")

    def test_stops_the_agent_and_keeps_its_plist(self) -> None:
        fake = FakeLaunchctl(self).expect(BOOTOUT, OK).expect(PRINT, ABSENT)
        launchd_agent.stop_agent(fake.ctl(), label=LABEL, deadline_s=1.0)
        self.assertEqual([BOOTOUT, PRINT], fake.calls)
        self.assertEqual(self.plist_path.read_bytes(), b"<plist/>")

    def test_an_agent_that_stays_loaded_raises(self) -> None:
        fake = FakeLaunchctl(self).expect(BOOTOUT, OK)
        for _ in range(3):
            fake.expect(PRINT, LOADED)
        with self.assertRaises(LaunchdError):
            launchd_agent.stop_agent(fake.ctl(), label=LABEL, deadline_s=1.0)
        self.assertTrue(self.plist_path.is_file())


class RunSeamTest(unittest.TestCase):
    """A launchctl that cannot be launched leaves bootout and bootstrap as a LaunchdError.

    A timeout there is ignored, since the read-back after it reads the real state, but an
    OSError means no read-back can follow, so the seam raises LaunchdError chained to it.
    """

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.plist_path = Path(folder.name) / f"{LABEL}.plist"
        self.missing = FileNotFoundError(2, "No such file or directory", "launchctl")

    def assert_mapped(self, raised: LaunchdError, verb: str) -> None:
        """The error names the label, the verb and its cause, and chains the OSError.

        The label leads, as in every other LaunchdError, so a caller installing several
        agents can tell which one failed.
        """
        self.assertIs(raised.__cause__, self.missing)
        self.assertTrue(
            str(raised).startswith(f"{LABEL}: could not run launchctl {verb}: "),
            str(raised),
        )
        self.assertIn("No such file or directory", str(raised))

    def test_the_seam_maps_an_oserror_to_a_launchd_error(self) -> None:
        fake = FakeLaunchctl(self).expect(BOOTOUT, self.missing)
        with self.assertRaises(LaunchdError) as caught:
            launchd_agent._run_unchecked(fake.ctl(), BOOTOUT, label=LABEL)
        self.assert_mapped(caught.exception, "bootout")

    def test_stop_agent_raises_a_launchd_error(self) -> None:
        fake = FakeLaunchctl(self).expect(BOOTOUT, self.missing)
        with self.assertRaises(LaunchdError) as caught:
            launchd_agent.stop_agent(fake.ctl(), label=LABEL, deadline_s=1.0)
        self.assert_mapped(caught.exception, "bootout")

    def test_uninstall_raises_a_launchd_error_and_keeps_the_plist(self) -> None:
        self.plist_path.write_bytes(b"<plist/>")
        fake = FakeLaunchctl(self).expect(BOOTOUT, self.missing)
        with self.assertRaises(LaunchdError) as caught:
            launchd_agent.uninstall_agent(
                fake.ctl(), label=LABEL, plist_path=self.plist_path, deadline_s=1.0
            )
        self.assert_mapped(caught.exception, "bootout")
        self.assertEqual(self.plist_path.read_bytes(), b"<plist/>")

    def test_install_raises_a_launchd_error_and_keeps_the_plist(self) -> None:
        fake = FakeLaunchctl(self).expect(BOOTOUT, self.missing)
        with self.assertRaises(LaunchdError) as caught:
            launchd_agent.install_agent(
                fake.ctl(), label=LABEL, plist_path=self.plist_path, plist=b"<plist/>"
            )
        self.assert_mapped(caught.exception, "bootout")
        self.assertEqual(self.plist_path.read_bytes(), b"<plist/>")

    def test_an_unrunnable_bootstrap_raises_a_launchd_error(self) -> None:
        bootstrap = [
            launchd_agent.LAUNCHCTL,
            "bootstrap",
            f"gui/{UID}",
            str(self.plist_path),
        ]
        fake = FakeLaunchctl(self).expect(BOOTOUT, OK).expect(PRINT, ABSENT)
        fake.expect(bootstrap, self.missing)
        with self.assertRaises(LaunchdError) as caught:
            launchd_agent.install_agent(
                fake.ctl(), label=LABEL, plist_path=self.plist_path, plist=b"<plist/>"
            )
        self.assert_mapped(caught.exception, "bootstrap")
        self.assertEqual([BOOTOUT, PRINT, bootstrap], fake.calls)


@unittest.skipUnless(
    _LIVE and sys.platform == "darwin",
    "set LAUNCHD_AGENT_LIVE=1 on macOS: it installs a real LaunchAgent",
)
class LiveLaunchdTest(unittest.TestCase):  # pragma: no cover - the owner's opt-in only
    """Install, read, uninstall and read a real agent in this account's GUI domain."""

    def test_install_then_uninstall_reads_back(self) -> None:
        ctl = launchd_agent.real_launchctl()
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            plist = launchd_agent.build_keepalive_plist(
                label=_LIVE_LABEL,
                program_arguments=["/usr/bin/true"],
                working_directory=str(base),
                log_paths=(str(base / "out.log"), str(base / "err.log")),
                path_value="/usr/bin:/bin",
            )
            plist_path = base / f"{_LIVE_LABEL}.plist"
            try:
                launchd_agent.install_agent(
                    ctl, label=_LIVE_LABEL, plist_path=plist_path, plist=plist
                )
                self.assertIs(
                    AgentState.LOADED, launchd_agent.agent_state(ctl, _LIVE_LABEL)
                )
            finally:
                launchd_agent.uninstall_agent(
                    ctl, label=_LIVE_LABEL, plist_path=plist_path
                )
            self.assertIs(
                AgentState.ABSENT, launchd_agent.agent_state(ctl, _LIVE_LABEL)
            )
            self.assertFalse(plist_path.exists())
