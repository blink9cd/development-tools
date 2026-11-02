#!/usr/bin/env python3
"""The command-line front end: one schedule in, the chosen artifacts written as files.

Each test runs ``main`` against a fresh temporary output directory, so nothing outside it
is written and nothing is ever installed. The exit-code contract is the one the epilog
states: 0 written, 1 a destination already exists (and then nothing is written), 2 bad
input, with a message on stderr naming the option at fault.
"""

from __future__ import annotations

import contextlib
import io
import os
import plistlib
import tempfile
import unittest
from datetime import date
from unittest import mock
from pathlib import Path

import os_schedule_gen as sa

# sched_cli puts help_formatter's folder on the path, so it is imported first.
import sched_cli
import help_formatter

_PROGRAM = "/opt/example/bin/report"
_ALL_FILES = (
    "job.xml",
    "job.service",
    "job.timer",
    "job.plist",
    "job.crontab",
)


class _Run:
    """One ``main`` call: its exit code, its two streams, and the files it left behind."""

    def __init__(self, code: int, out: str, err: str, folder: Path) -> None:
        self.code, self.out, self.err = code, out, err
        self.files = sorted(path.name for path in folder.iterdir())


class CliTestCase(unittest.TestCase):
    """A temporary output directory per test, and a helper that runs the CLI into it."""

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.folder = Path(holder.name)

    def run_cli(self, *extra: str, base: bool = True) -> _Run:
        """Run ``main`` with the required options (unless *base* is false) plus *extra*."""
        argv = (
            ["--name", "job", "--time", "06:30", "--program", _PROGRAM]
            + ["--output-dir", str(self.folder), "--start-date", "2026-01-05"]
            if base
            else []
        ) + list(extra)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = sa.main(argv)
            except SystemExit as exc:  # argparse's own refusals exit here
                code = int(exc.code or 0)
        return _Run(code, out.getvalue(), err.getvalue(), self.folder)


class WritesTests(CliTestCase):
    """What a successful run writes, and what it prints."""

    def test_every_target_by_default_one_stdout_line_per_file(self) -> None:
        result = self.run_cli()
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.files, sorted(_ALL_FILES))
        self.assertEqual(
            result.out.splitlines(), [str(self.folder / name) for name in _ALL_FILES]
        )

    def test_a_chosen_target_writes_only_its_files(self) -> None:
        result = self.run_cli("--target", "systemd", "--target", "systemd")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.files, ["job.service", "job.timer"])

    def test_the_task_xml_is_utf16_as_schtasks_expects(self) -> None:
        self.run_cli("--target", "task-xml")
        raw = (self.folder / "job.xml").read_bytes()
        self.assertTrue(raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"))
        text = raw.decode("utf-16")
        self.assertIn(r"<URI>\job</URI>", text)
        self.assertIn("<Description>Scheduled job job</Description>", text)
        self.assertIn("<StartBoundary>2026-01-05T06:30:00</StartBoundary>", text)
        self.assertIn("<ExecutionTimeLimit>PT120M</ExecutionTimeLimit>", text)
        self.assertIn("<LogonType>S4U</LogonType>", text)
        self.assertIn("<ScheduleByDay>", text)
        self.assertNotIn("WorkingDirectory", text)

    def test_no_day_is_daily_and_a_day_is_weekly(self) -> None:
        self.run_cli("--target", "cron")
        self.assertEqual(
            (self.folder / "job.crontab").read_text(encoding="utf-8"),
            f'30 6 * * * "{_PROGRAM}"\n',
        )
        self.run_cli("--target", "cron", "--day", "Tuesday", "--force")
        self.assertEqual(
            (self.folder / "job.crontab").read_text(encoding="utf-8"),
            f'30 6 * * 2 "{_PROGRAM}"\n',
        )

    def test_every_option_reaches_its_artifact(self) -> None:
        result = self.run_cli(
            "--arg=--mode",
            "--arg",
            "run",
            "--description",
            "Nightly report",
            "--working-directory",
            "/opt/example",
            "--path-env",
            "/usr/bin:/bin",
            "--log-dir",
            "/var/tmp/logs",
            "--time-limit-minutes",
            "30",
            "--logon-type",
            "InteractiveToken",
        )
        self.assertEqual(result.code, 0, result.err)
        service = (self.folder / "job.service").read_text(encoding="utf-8")
        self.assertIn(f'ExecStart="{_PROGRAM}" --mode run\n', service)
        self.assertIn("Description=Nightly report\n", service)
        self.assertIn("WorkingDirectory=/opt/example\n", service)
        self.assertIn('Environment="PATH=/usr/bin:/bin"\n', service)
        self.assertIn("TimeoutStartSec=1800\n", service)
        plist = plistlib.loads((self.folder / "job.plist").read_bytes())
        self.assertEqual(plist["Label"], "job")
        self.assertEqual(plist["StandardOutPath"], "/var/tmp/logs/job.out.log")
        self.assertEqual(plist["StandardErrorPath"], "/var/tmp/logs/job.err.log")
        self.assertEqual(plist["EnvironmentVariables"], {"PATH": "/usr/bin:/bin"})
        xml = (self.folder / "job.xml").read_bytes().decode("utf-16")
        self.assertIn("<LogonType>InteractiveToken</LogonType>", xml)
        self.assertIn("<ExecutionTimeLimit>PT30M</ExecutionTimeLimit>", xml)

    def test_force_overwrites_an_existing_file(self) -> None:
        (self.folder / "job.crontab").write_bytes(b"old\n")
        result = self.run_cli("--target", "cron", "--force")
        self.assertEqual(result.code, 0, result.err)
        self.assertNotIn(
            "old", (self.folder / "job.crontab").read_text(encoding="utf-8")
        )

    def test_the_start_date_defaults_to_today(self) -> None:
        args = sa.build_parser().parse_args(
            ["--name", "job", "--time", "06:30", "--program", _PROGRAM]
            + ["--output-dir", str(self.folder), "--target", "task-xml"]
        )
        with contextlib.redirect_stdout(io.StringIO()):
            code = sched_cli.run(args, today=lambda: date(2027, 3, 4))
        self.assertEqual(code, 0)
        xml = (self.folder / "job.xml").read_bytes().decode("utf-16")
        self.assertIn("<StartBoundary>2027-03-04T06:30:00</StartBoundary>", xml)


@unittest.skipIf(os.name == "nt", "POSIX permission bits and umask")
class FileModeTests(CliTestCase):
    """A written file's mode is 0644 less the process umask, never wider than it allows."""

    def run_under_umask(self, mask: int) -> int:
        """Write the cron target under *mask*; return the file's permission bits."""
        previous = os.umask(mask)
        try:
            result = self.run_cli("--target", "cron", "--force")
        finally:
            os.umask(previous)
        self.assertEqual(result.code, 0, result.err)
        return (self.folder / "job.crontab").stat().st_mode & 0o777

    def test_a_restrictive_umask_is_honored(self) -> None:
        self.assertEqual(self.run_under_umask(0o077), 0o600)

    def test_the_usual_umask_gives_0644(self) -> None:
        self.assertEqual(self.run_under_umask(0o022), 0o644)

    def test_a_group_umask_keeps_group_read(self) -> None:
        self.assertEqual(self.run_under_umask(0o027), 0o640)


class ExistingDestinationTests(CliTestCase):
    """Exit 1: a destination exists and --force was not given, so nothing is written."""

    def test_one_existing_file_stops_every_write(self) -> None:
        (self.folder / "job.timer").write_bytes(b"keep\n")
        result = self.run_cli()
        self.assertEqual(result.code, 1)
        self.assertEqual(result.files, ["job.timer"])
        self.assertEqual(result.out, "")
        self.assertIn("job.timer", result.err)
        self.assertIn("--force", result.err)
        self.assertEqual(
            (self.folder / "job.timer").read_text(encoding="utf-8"), "keep\n"
        )


class WriteFailureTests(CliTestCase):
    """Exit 2 when a file cannot be written, with no temporary file left behind."""

    def test_a_failed_write_is_reported_and_cleaned_up(self) -> None:
        with mock.patch.object(
            sched_cli.os, "replace", side_effect=OSError("disk full")
        ):
            result = self.run_cli("--target", "cron")
        self.assertEqual(result.code, 2)
        self.assertIn("could not write", result.err)
        self.assertIn("disk full", result.err)
        self.assertEqual(result.files, [])
        self.assertEqual(result.out, "")

    def test_a_temporary_already_gone_does_not_mask_the_error(self) -> None:
        # The swap fails after something removed the temporary: the cleanup must not
        # raise FileNotFoundError over the error that actually stopped the write.
        def vanish_then_fail(source: str, _target: object) -> None:
            os.unlink(source)
            raise OSError("disk full")

        with mock.patch.object(sched_cli.os, "replace", side_effect=vanish_then_fail):
            result = self.run_cli("--target", "cron")
        self.assertEqual(result.code, 2)
        self.assertIn("disk full", result.err)
        self.assertEqual(result.files, [])

    def test_a_taken_temporary_name_is_retried(self) -> None:
        (self.folder / ".job.crontab.taken").write_bytes(b"")
        with mock.patch.object(
            sched_cli.secrets, "token_hex", side_effect=["taken", "free"]
        ):
            result = self.run_cli("--target", "cron")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.files, [".job.crontab.taken", "job.crontab"])

    def test_no_free_temporary_name_is_a_write_failure(self) -> None:
        (self.folder / ".job.crontab.taken").write_bytes(b"")
        with mock.patch.object(sched_cli.secrets, "token_hex", return_value="taken"):
            result = self.run_cli("--target", "cron")
        self.assertEqual(result.code, 2)
        self.assertIn("could not write", result.err)
        self.assertEqual(result.files, [".job.crontab.taken"])


class BadInputTests(CliTestCase):
    """Exit 2: refused input, named on stderr, with nothing written."""

    def assert_refused(self, result: _Run, option: str) -> None:
        """Exit 2, *option* named on stderr, and the output directory left empty."""
        self.assertEqual(result.code, 2, result.err)
        self.assertIn(option, result.err)
        self.assertEqual(result.files, [])
        self.assertEqual(result.out, "")

    def test_a_name_outside_the_safe_set(self) -> None:
        for name in ("a b", "a/b", "", "x\\y", "-x", ".x", ".", "..", "_x"):
            with self.subTest(name=name):
                self.assert_refused(self.run_cli("--name", name), "--name")

    def test_a_malformed_time(self) -> None:
        for value in ("25:00", "6pm", "06:60", ""):
            with self.subTest(value=value):
                self.assert_refused(self.run_cli("--time", value), "--time")

    def test_a_missing_output_directory(self) -> None:
        missing = str(self.folder / "absent")
        self.assert_refused(self.run_cli("--output-dir", missing), "--output-dir")

    def test_a_bad_start_date_or_time_limit(self) -> None:
        for value in ("2026-13-01", "2026-1-5", "20260105", "soon"):
            with self.subTest(value=value):
                self.assert_refused(self.run_cli("--start-date", value), "--start-date")
        for value in ("0", "-5", "ten", "1.5", "525601", "99999999999999999999"):
            with self.subTest(value=value):
                self.assert_refused(
                    self.run_cli("--time-limit-minutes", value), "--time-limit-minutes"
                )

    def test_the_time_limit_maximum_is_one_year(self) -> None:
        result = self.run_cli("--target", "systemd", "--time-limit-minutes", "525600")
        self.assertEqual(result.code, 0, result.err)
        service = (self.folder / "job.service").read_text(encoding="utf-8")
        self.assertIn("TimeoutStartSec=31536000\n", service)
        refused = self.run_cli("--time-limit-minutes", "525601", "--force")
        self.assertIn("525600", refused.err)

    def test_a_choice_outside_its_set(self) -> None:
        self.assert_refused(self.run_cli("--day", "Someday"), "--day")
        self.assert_refused(self.run_cli("--target", "at"), "--target")
        self.assert_refused(self.run_cli("--logon-type", "Password"), "--logon-type")

    def test_a_missing_required_option(self) -> None:
        result = self.run_cli("--name", "job", base=False)
        self.assertEqual(result.code, 2)
        self.assertIn("--time", result.err)

    def test_a_line_break_names_the_option_that_carried_it(self) -> None:
        for option in (
            "--program",
            "--arg",
            "--description",
            "--working-directory",
            "--path-env",
            "--log-dir",
        ):
            with self.subTest(option=option):
                self.assert_refused(self.run_cli(f"{option}=/opt/a\nb"), option)

    def test_a_value_only_one_format_refuses_names_that_target(self) -> None:
        # plistlib refuses a control character; no builder keyword is named, so the
        # message is the library's own.
        result = self.run_cli("--target", "launchd", "--arg=a\x01b")
        self.assert_refused(result, "launchd")
        self.assertIn("control characters", result.err)

    def test_a_value_a_builder_refuses_names_its_target(self) -> None:
        # systemd cannot hold a WorkingDirectory= ending in a backslash; the builder's
        # ValueError is reported, not raised, and no other target is written instead.
        result = self.run_cli("--working-directory", "/opt/x\\")
        self.assert_refused(result, "systemd")
        self.assertIn("--working-directory", result.err)

    def test_a_relative_directory_is_refused_for_every_target(self) -> None:
        for option in ("--working-directory", "--log-dir"):
            for target in sched_cli.TARGETS:
                for value in ("relative", "./x", "~/x", "-x"):
                    with self.subTest(option=option, target=target, value=value):
                        result = self.run_cli("--target", target, f"{option}={value}")
                        self.assert_refused(result, option)
                        self.assertIn("absolute", result.err)

    def test_a_windows_working_directory_is_absolute_for_the_task(self) -> None:
        for value in ("C:\\work", "\\\\server\\share\\work"):
            with self.subTest(value=value):
                result = self.run_cli(
                    "--target", "task-xml", "--working-directory", value, "--force"
                )
                self.assertEqual(result.code, 0, result.err)

    def test_a_control_character_in_a_task_value_names_its_option(self) -> None:
        result = self.run_cli("--target", "task-xml", "--arg=a\x01b")
        self.assert_refused(result, "--arg")
        self.assertIn("task-xml", result.err)


class ParserTests(unittest.TestCase):
    """The parser's own contract: house formatter, named options, an honest epilog."""

    def test_the_parser_uses_the_house_formatter(self) -> None:
        parser = sa.build_parser()
        self.assertTrue(
            issubclass(parser.formatter_class, help_formatter.ReqAwareDefaultsFormatter)  # type: ignore[arg-type]
        )

    def test_the_epilog_states_prerequisites_exit_codes_and_an_example(self) -> None:
        epilog = sa.build_parser().epilog or ""
        for needle in ("Prerequisites", "Exit codes", "0 ", "1 ", "2 ", "--arg=--mode"):
            with self.subTest(needle=needle):
                self.assertIn(needle, epilog)

    def test_help_warns_that_task_scheduler_expands_percent_names(self) -> None:
        text = " ".join(sa.build_parser().format_help().split())
        self.assertIn("%NAME%", text)
        self.assertIn("no escape", text)

    def test_help_shows_the_time_limit_default(self) -> None:
        self.assertIn("120", sa.build_parser().format_help())

    def test_help_shows_no_default_a_person_could_not_type(self) -> None:
        # The other options say what leaving them out means; a bare False or None
        # beside --force would be the one stock note left among them.
        text = sa.build_parser().format_help()
        for note in ("(default: False)", "(default: None)"):
            with self.subTest(note=note):
                self.assertNotIn(note, text)

    def test_force_is_off_when_omitted_and_on_when_given(self) -> None:
        parser = sa.build_parser()
        required = (
            "--name", "n", "--time", "01:00", "--program", "/p", "--output-dir", ".",
        )  # fmt: skip
        self.assertFalse(parser.parse_args(required).force)
        self.assertIs(parser.parse_args([*required, "--force"]).force, True)

    def test_help_states_the_time_limit_maximum(self) -> None:
        self.assertIn("525600", sa.build_parser().format_help())

    def test_the_choices_come_from_the_library(self) -> None:
        parser = sa.build_parser()
        choices = {
            action.dest: tuple(action.choices or ())
            for action in parser._actions
            if action.choices
        }
        self.assertEqual(choices["day"], sa.DAYS)
        self.assertEqual(choices["logon_type"], sa.LOGON_TYPES)
        self.assertEqual(choices["target"], sched_cli.TARGETS)


if __name__ == "__main__":
    unittest.main()
