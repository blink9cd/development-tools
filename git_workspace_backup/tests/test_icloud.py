#!/usr/bin/env python3
"""Per-file iCloud upload state: the argv osascript receives, the answer's parse, the summary.

Every call but the live boundary checks is answered by a FakeRunner route, which refuses an
unrouted osascript outright. The live checks call ``backup_proc.run_cmd`` directly, and only
with paths inside a fresh temporary directory: the query is a read-only resource-value lookup,
and its inputs never name the real destination or anything under a home. They run osascript by
its absolute path, which no stand-in first on PATH can answer instead, so
``WORKSPACE_BACKUP_OFFLINE=1`` skips them for a run that must not reach the real one, as in an
export's proof.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path

import _fixtures as fx

import backup_icloud as bi
import backup_proc
from backup_proc import NOT_STARTED_RC, OSASCRIPT_TIMEOUT_S, TIMEOUT_RC


class ReadUploadStatesTests(unittest.TestCase):
    def test_paths_travel_after_the_options_end_and_the_answer_is_parsed(self) -> None:
        answer = fx.upload_states_json((True, True), (True, False)) + "\n"
        runner = fx.FakeRunner({"osascript": fx.osascript_answering(0, answer)})
        states = bi.read_upload_states([Path("/opt/a"), Path("/opt/b")], run=runner)
        self.assertEqual(
            states, (bi.UploadState(True, True), bi.UploadState(True, False))
        )
        [argv] = runner.calls
        self.assertEqual(argv[:4], [backup_proc.OSASCRIPT, "-l", "JavaScript", "-e"])
        # "--" ends osascript's own options; without it a dash-leading argument is refused
        # as an illegal option and nothing runs. Both osascript call sites share this shape.
        self.assertEqual(argv[5:], ["--", "/opt/a", "/opt/b"])

    def test_the_script_is_one_constant_that_never_holds_a_path(self) -> None:
        def local(argv: Sequence[str]) -> "tuple[int, str, str]":
            pairs = [(False, False)] * len(fx.osascript_paths(argv))
            return 0, fx.upload_states_json(*pairs), ""

        runner = fx.FakeRunner({"osascript": local})
        bi.read_upload_states([Path("/opt/fixture-one")], run=runner)
        bi.read_upload_states([Path("/opt/fixture-two"), Path("/opt/b")], run=runner)
        scripts = {call[4] for call in runner.calls}
        self.assertEqual(len(scripts), 1)
        self.assertNotIn("fixture", scripts.pop())

    def test_the_bound_is_osascripts_timeout(self) -> None:
        bounds: list[float] = []

        def run(
            argv: Sequence[str], cwd: "Path | None", timeout: float
        ) -> "tuple[int, str, str]":
            bounds.append(timeout)
            return 0, fx.upload_states_json((False, False)), ""

        bi.read_upload_states([Path("/opt/a")], run=run)
        self.assertEqual(bounds, [OSASCRIPT_TIMEOUT_S])

    def test_no_paths_asks_nothing(self) -> None:
        runner = fx.FakeRunner({"osascript": fx.osascript_answering(0, "[]")})
        self.assertEqual(bi.read_upload_states([], run=runner), ())
        self.assertEqual(runner.calls, [])

    def test_a_failed_timed_out_or_unstarted_osascript_is_unreadable(self) -> None:
        # Whatever it printed: an answer from a run that did not exit 0 is never believed.
        uploaded = fx.upload_states_json((True, True))
        for code in (1, TIMEOUT_RC, NOT_STARTED_RC):
            with self.subTest(code=code):
                route = fx.osascript_answering(code, uploaded)
                runner = fx.FakeRunner({"osascript": route})
                self.assertIsNone(bi.read_upload_states([Path("/opt/a")], run=runner))


class ParseUploadStatesTests(unittest.TestCase):
    def test_a_well_formed_answer_of_the_expected_length_is_accepted(self) -> None:
        text = fx.upload_states_json((False, False), (True, True))
        self.assertEqual(
            bi.parse_upload_states(text, 2),
            (bi.UploadState(False, False), bi.UploadState(True, True)),
        )
        self.assertEqual(bi.parse_upload_states("[]\n", 0), ())

    def test_every_malformed_answer_is_refused(self) -> None:
        one = fx.upload_states_json((True, True))
        cases = (
            ("not json", 1),
            ("", 1),
            ("{}", 1),
            (one, 2),
            (one, 0),
            ("[1]", 1),
            # The script answers null for a path it cannot reach or read.
            ("[null]", 1),
            ('[{"ubiquitous": true}]', 1),
            ('[{"ubiquitous": true, "uploaded": true, "extra": true}]', 1),
            ('[{"ubiquitous": 1, "uploaded": true}]', 1),
            ('[{"ubiquitous": true, "uploaded": null}]', 1),
            ('[{"ubiquitous": true, "uploaded": "true"}]', 1),
            # Nesting deep enough to exhaust the decoder's recursion.
            ("[" * 100_000, 1),
        )
        for text, expected in cases:
            with self.subTest(text=text[:40], expected=expected):
                self.assertIsNone(bi.parse_upload_states(text, expected))


class SummaryTests(unittest.TestCase):
    def test_summaries(self) -> None:
        up = bi.UploadState(True, True)
        pending = bi.UploadState(True, False)
        local = bi.UploadState(False, False)
        cases = (
            (None, bi.UPLOAD_UNREADABLE),
            ((), bi.UPLOAD_NOT_ICLOUD),
            ((local, local), bi.UPLOAD_NOT_ICLOUD),
            ((up, local), bi.UPLOAD_UNREADABLE),
            ((up, up), bi.UPLOAD_UPLOADED),
            ((up, pending), bi.UPLOAD_PENDING),
        )
        for states, expected in cases:
            with self.subTest(states=states):
                self.assertEqual(bi.summarize_upload(states), expected)


#: Set to 1 to skip the live checks on a run that must not reach the real osascript: a run
#: that puts a logging stand-in first on PATH sets it, as that stand-in would answer them.
_OFFLINE_ENV = "WORKSPACE_BACKUP_OFFLINE"


def _live_skip_reason(
    environ: Mapping[str, str], osascript: "str | None"
) -> "str | None":
    """Why the live Foundation checks must not run, else None.

    *osascript* is what a PATH lookup of osascript finds, or None when it finds nothing.
    """
    if environ.get(_OFFLINE_ENV) == "1":
        return f"{_OFFLINE_ENV}=1 - live osascript checks skipped"
    if osascript is None:
        return "osascript is macOS-only"
    return None


_LIVE_SKIP = _live_skip_reason(os.environ, shutil.which(backup_proc.OSASCRIPT))


class LiveSkipTests(unittest.TestCase):
    """When the live checks below run, and why they are skipped otherwise."""

    def test_the_offline_switch_skips_them_even_with_osascript_present(self) -> None:
        self.assertEqual(
            _live_skip_reason({"WORKSPACE_BACKUP_OFFLINE": "1"}, "/usr/bin/osascript"),
            "WORKSPACE_BACKUP_OFFLINE=1 - live osascript checks skipped",
        )

    def test_the_live_class_is_skipped_exactly_when_there_is_a_reason(self) -> None:
        # The reason alone proves nothing: this pins the decorator that acts on it.
        skipped = getattr(LiveFoundationTests, "__unittest_skip__", False)
        self.assertEqual(skipped, _LIVE_SKIP is not None)

    def test_a_missing_osascript_skips_them(self) -> None:
        self.assertEqual(_live_skip_reason({}, None), "osascript is macOS-only")

    def test_they_run_with_osascript_present_and_the_switch_unset(self) -> None:
        self.assertIsNone(
            _live_skip_reason({"WORKSPACE_BACKUP_OFFLINE": "0"}, "/usr/bin/osascript")
        )


@unittest.skipIf(_LIVE_SKIP is not None, _LIVE_SKIP or "")
class LiveFoundationTests(unittest.TestCase):
    """The only proof the Foundation bridge answers on this macOS release, argv included."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_a_local_temp_file_reads_as_not_in_icloud(self) -> None:
        path = self.tmp / "local.txt"
        path.write_bytes(b"fixture")
        states = bi.read_upload_states([path], run=backup_proc.run_cmd)
        self.assertEqual(bi.summarize_upload(states), bi.UPLOAD_NOT_ICLOUD)

    def test_a_missing_path_reads_as_unreadable_never_as_not_in_icloud(self) -> None:
        # Measured: Foundation answers a missing path's ubiquity keys exactly as it answers a
        # local file's, so without the script's reachability check a vanished snapshot would
        # pass as "not in iCloud" and status would call it healthy.
        present = self.tmp / "present.txt"
        present.write_bytes(b"fixture")
        states = bi.read_upload_states(
            [present, self.tmp / "absent.txt"], run=backup_proc.run_cmd
        )
        self.assertEqual(bi.summarize_upload(states), bi.UPLOAD_UNREADABLE)


if __name__ == "__main__":
    unittest.main()
