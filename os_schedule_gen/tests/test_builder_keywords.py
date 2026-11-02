#!/usr/bin/env python3
"""The keyword parameters that let a caller other than the sweep author its own job.

Every keyword defaults to the sweep's value, and the byte pins in the other suites hold
those defaults; this suite holds what each keyword changes when it is given. A *script* of
``None`` drops the script word, for a program run with its arguments alone, and with it the
working directory that would have been derived from that script's folder.

The Task XML is parsed with the standard-library parser, which is safe: its only input is
the string the code under test just produced.
"""

from __future__ import annotations

import plistlib
import unittest
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import os_schedule_gen as sa
from _fixtures import FAKE_PATH, FAKE_PYTHON, FAKE_SCRIPT

_NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
_WEEKLY = sa.WeeklySchedule(weekday="Monday", hour=4, minute=45)
_START = date(2026, 1, 5)
_PROGRAM = "/opt/example/bin/report"


def _exec_fields(xml: str) -> "dict[str, str | None]":
    """The task's Exec action, field by field; a missing field reads as None."""
    root = ET.fromstring(xml)
    return {
        tag: root.findtext(f"{_NS}Actions/{_NS}Exec/{_NS}{tag}")
        for tag in ("Command", "Arguments", "WorkingDirectory")
    }


class TaskXmlKeywordTests(unittest.TestCase):
    """``build_task_xml``'s keywords reach the task they name."""

    def test_every_keyword_reaches_its_element(self) -> None:
        xml = sa.build_task_xml(
            _WEEKLY,
            _PROGRAM,
            None,
            _START,
            task_name=r"\nightly-report",
            description="Nightly report",
            args=("--mode", "run"),
            working_directory=r"C:\work",
            execution_limit="PT30M",
            logon_type="InteractiveToken",
        )
        root = ET.fromstring(xml)
        self.assertEqual(
            root.findtext(f"{_NS}RegistrationInfo/{_NS}URI"), r"\nightly-report"
        )
        self.assertEqual(
            root.findtext(f"{_NS}RegistrationInfo/{_NS}Description"), "Nightly report"
        )
        self.assertEqual(
            root.findtext(f"{_NS}Settings/{_NS}ExecutionTimeLimit"), "PT30M"
        )
        self.assertEqual(
            root.findtext(f"{_NS}Principals/{_NS}Principal/{_NS}LogonType"),
            "InteractiveToken",
        )
        self.assertEqual(
            _exec_fields(xml),
            {
                "Command": _PROGRAM,
                "Arguments": "--mode run",
                "WorkingDirectory": r"C:\work",
            },
        )

    def test_no_script_and_no_directory_omits_the_working_directory(self) -> None:
        xml = sa.build_task_xml(_WEEKLY, _PROGRAM, None, _START, args=())
        self.assertEqual(
            _exec_fields(xml),
            {"Command": _PROGRAM, "Arguments": None, "WorkingDirectory": None},
        )

    def test_a_line_break_in_any_value_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            sa.build_task_xml(_WEEKLY, _PROGRAM, None, _START, description="a\nb")
        with self.assertRaises(ValueError):
            sa.build_task_xml(_WEEKLY, _PROGRAM, None, _START, working_directory="a\nb")
        for python, script, args in (
            ("a\rb", None, ()),
            (_PROGRAM, "a\0b", ()),
            (_PROGRAM, None, ("a\nb",)),
        ):
            with self.subTest(python=python, script=script, args=args):
                with self.assertRaises(ValueError):
                    sa.build_task_xml(_WEEKLY, python, script, _START, args=args)

    def test_a_task_name_outside_the_safe_shape_is_refused(self) -> None:
        # A leading backslash, then backslash-separated segments each in the job-name
        # set: a ".." segment, markup or an empty segment is not a task path.
        for name in (
            "",
            "\\",
            "nightly",
            r"\a\..\b",
            r"\a\\b",
            "\\a\\",
            "<Evil/>",
            r"\<Evil/>",
            r"\a b",
            r"\-x",
            r"\.x",
            r"\a/b",
        ):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    sa.build_task_xml(_WEEKLY, _PROGRAM, None, _START, task_name=name)

    def test_a_task_name_in_the_safe_shape_is_kept(self) -> None:
        for name in (sa.TASK_NAME, r"\Maintenance\NightlyReport", r"\nightly-report"):
            with self.subTest(name=name):
                xml = sa.build_task_xml(_WEEKLY, _PROGRAM, None, _START, task_name=name)
                uri = ET.fromstring(xml).findtext(f"{_NS}RegistrationInfo/{_NS}URI")
                self.assertEqual(uri, name)


class SystemdKeywordTests(unittest.TestCase):
    """``build_systemd_units``' keywords reach the units they name."""

    def setUp(self) -> None:
        self.service, self.timer = sa.build_systemd_units(
            _WEEKLY,
            _PROGRAM,
            None,
            "",
            unit_basename="nightly-report",
            description="Nightly report",
            args=("--mode", "run"),
            working_directory="/opt/example",
            time_limit_seconds=1800,
        )

    def test_the_service_runs_the_program_with_its_arguments(self) -> None:
        self.assertIn(f'ExecStart="{_PROGRAM}" --mode run\n', self.service)
        self.assertIn("WorkingDirectory=/opt/example\n", self.service)
        self.assertIn("TimeoutStartSec=1800\n", self.service)

    def test_both_units_carry_the_description_and_the_timer_names_the_service(
        self,
    ) -> None:
        self.assertIn("Description=Nightly report\n", self.service)
        self.assertIn("Description=Nightly report\n", self.timer)
        self.assertIn("Unit=nightly-report.service\n", self.timer)

    def test_no_script_and_no_directory_omits_the_working_directory(self) -> None:
        service, _timer = sa.build_systemd_units(_WEEKLY, _PROGRAM, None, "", args=())
        self.assertIn(f'ExecStart="{_PROGRAM}"\n', service)
        self.assertNotIn("WorkingDirectory", service)

    def test_a_percent_in_the_description_is_doubled(self) -> None:
        service, _timer = sa.build_systemd_units(
            _WEEKLY, _PROGRAM, None, "", description="50% of %n"
        )
        self.assertIn("Description=50%% of %%n\n", service)

    def test_a_description_systemd_would_misread_is_refused(self) -> None:
        for description in ("ends in \\", " leading", "trailing ", "a\nb"):
            with self.subTest(description=description):
                with self.assertRaises(ValueError):
                    sa.build_systemd_units(
                        _WEEKLY, _PROGRAM, None, "", description=description
                    )

    def test_a_working_directory_systemd_would_misread_is_refused(self) -> None:
        for directory in ("relative", "/opt/a\\", "/opt/a ", "~/x", "-/opt"):
            with self.subTest(directory=directory):
                with self.assertRaises(ValueError):
                    sa.build_systemd_units(
                        _WEEKLY, _PROGRAM, None, "", working_directory=directory
                    )

    def test_a_unit_name_outside_the_safe_set_is_refused(self) -> None:
        for name in ("", "a b", "a/b", "a@b", "a\nb", "%n", "-x", ".x", ".", ".."):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    sa.build_systemd_units(
                        _WEEKLY, _PROGRAM, None, "", unit_basename=name
                    )

    def test_a_time_limit_that_is_not_a_positive_int_is_refused(self) -> None:
        for limit in (0, -1, True):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    sa.build_systemd_units(
                        _WEEKLY, _PROGRAM, None, "", time_limit_seconds=limit
                    )


class LaunchdKeywordTests(unittest.TestCase):
    """``build_launchd_plist``'s new keywords, and its optional script and state dir."""

    def test_explicit_logs_and_directory_replace_the_derived_ones(self) -> None:
        plist = plistlib.loads(
            sa.build_launchd_plist(
                _WEEKLY,
                _PROGRAM,
                None,
                None,
                "",
                label="nightly-report",
                args=("--mode", "run"),
                working_directory="/opt/example",
                log_paths=("/var/tmp/r.out.log", "/var/tmp/r.err.log"),
            )
        )
        self.assertEqual(plist["ProgramArguments"], [_PROGRAM, "--mode", "run"])
        self.assertEqual(plist["WorkingDirectory"], "/opt/example")
        self.assertEqual(plist["StandardOutPath"], "/var/tmp/r.out.log")
        self.assertEqual(plist["StandardErrorPath"], "/var/tmp/r.err.log")

    def test_no_script_no_state_dir_and_no_logs_omit_those_keys(self) -> None:
        plist = plistlib.loads(
            sa.build_launchd_plist(_WEEKLY, _PROGRAM, None, None, "", args=())
        )
        self.assertEqual(plist["ProgramArguments"], [_PROGRAM])
        for key in ("WorkingDirectory", "StandardOutPath", "StandardErrorPath"):
            self.assertNotIn(key, plist)

    def test_the_state_dir_still_derives_the_logs_when_no_paths_are_given(self) -> None:
        plist = plistlib.loads(
            sa.build_launchd_plist(
                _WEEKLY, FAKE_PYTHON, FAKE_SCRIPT, Path("/var/tmp/state"), FAKE_PATH
            )
        )
        self.assertEqual(
            plist["StandardOutPath"], "/var/tmp/state/logs/launchd.out.log"
        )

    def test_a_launchd_label_outside_the_safe_set_is_refused(self) -> None:
        # The label names the plist file, so a separator in it would place the file
        # outside LaunchAgents: the same set a unit name is held to.
        for label in ("", "a b", "a/b", "../../etc/x", "a\nb", "-x", ".x", ".", ".."):
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    sa.build_launchd_plist(
                        _WEEKLY, _PROGRAM, None, None, "", label=label
                    )

    def test_a_plist_path_for_a_label_outside_the_safe_set_is_refused(self) -> None:
        for label in ("", "a/b", "../../etc/x", "-x", ".x", ".", ".."):
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    sa.launchd_plist_path(Path("/opt/example/user"), label)

    def test_a_plist_path_for_a_safe_label_stays_in_launch_agents(self) -> None:
        path = sa.launchd_plist_path(Path("/opt/example/user"), "local.nightly-report")
        self.assertEqual(
            path,
            Path("/opt/example/user/Library/LaunchAgents/local.nightly-report.plist"),
        )


class CronKeywordTests(unittest.TestCase):
    """``build_cron_line``'s keywords, and its optional script."""

    def test_a_program_alone_with_its_arguments(self) -> None:
        line = sa.build_cron_line(_WEEKLY, _PROGRAM, None, "", args=("--mode", "run"))
        self.assertEqual(line, f'45 4 * * 1 "{_PROGRAM}" --mode run')

    def test_a_working_directory_is_entered_before_the_command(self) -> None:
        line = sa.build_cron_line(
            _WEEKLY, _PROGRAM, None, FAKE_PATH, args=(), working_directory="/opt/ex"
        )
        self.assertEqual(
            line, f'45 4 * * 1 cd "/opt/ex" && PATH="{FAKE_PATH}" "{_PROGRAM}"'
        )

    def test_a_relative_working_directory_is_refused(self) -> None:
        # cd reads a leading "-" as an option; an absolute path cannot start with one.
        for directory in ("relative", "-P", ""):
            with self.subTest(directory=directory):
                with self.assertRaises(ValueError):
                    sa.build_cron_line(
                        _WEEKLY, _PROGRAM, None, "", working_directory=directory
                    )

    def test_a_line_break_in_an_argument_or_directory_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            sa.build_cron_line(_WEEKLY, _PROGRAM, None, "", args=("a\nb",))
        with self.assertRaises(ValueError):
            sa.build_cron_line(
                _WEEKLY, _PROGRAM, None, "", working_directory="/opt/a\nb"
            )


if __name__ == "__main__":
    unittest.main()
