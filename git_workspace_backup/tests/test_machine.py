#!/usr/bin/env python3
"""This machine's identity by example: the label a host name gives, and the file that keeps it.

The label properties live in test_properties_machine.py. Every host name here is an obvious
fixture, injected through the seam; no test reads this machine's own.
"""

from __future__ import annotations

import errno
import os
import sys
import tempfile
import unittest
from pathlib import Path

import _fixtures as fx  # noqa: F401 - installs the src/ bootstrap

import backup_machine as bm

# mkfifo(3) is POSIX-only. The split is an ``if`` so a win32 type-check pass
# still sees the name; the win32 arm exists only for that pass.
if sys.platform == "win32":

    def _make_fifo(path: Path) -> None:  # pragma: only-on win32
        """Refuse: Windows has no FIFOs, and this tool never runs there."""
        raise OSError(errno.ENOSYS, "mkfifo needs POSIX, which Windows lacks")

else:  # pragma: only-on posix
    _make_fifo = os.mkfifo

#: The fixture host name, and the label it derives.
FIXTURE_HOST = "Fixture-Mac.local"
FIXTURE_LABEL = "fixture-mac"


def _host(name: str) -> bm.HostnameFn:
    """A host-name seam answering *name*."""
    return lambda: name


class DeriveLabelTests(unittest.TestCase):
    def test_the_short_host_name_is_lowercased(self) -> None:
        self.assertEqual(bm.derive_label(FIXTURE_HOST), FIXTURE_LABEL)

    def test_unsafe_characters_become_one_hyphen_and_the_ends_are_trimmed(
        self,
    ) -> None:
        for host, label in (
            ("Fixture Mac", "fixture-mac"),
            ("fixture__mac", "fixture-mac"),
            ("fixture--mac", "fixture-mac"),
            ("-fixture-mac-", "fixture-mac"),
            ("Fixture’s MacBook Pro", "fixture-s-macbook-pro"),
            ("fixture/mac\x00", "fixture-mac"),
        ):
            with self.subTest(host=host):
                self.assertEqual(bm.derive_label(host), label)

    def test_a_long_host_name_is_capped_without_a_trailing_hyphen(self) -> None:
        host = "a" * (bm.MAX_LABEL_LENGTH - 1) + "-b" + "c" * 10
        label = bm.derive_label(host)
        self.assertEqual(label, "a" * (bm.MAX_LABEL_LENGTH - 1))
        self.assertLessEqual(len(bm.derive_label("x" * 500)), bm.MAX_LABEL_LENGTH)

    def test_an_empty_or_wholly_unsafe_host_name_falls_back(self) -> None:
        for host in ("", ".local", "---", "€€€", " "):
            with self.subTest(host=host):
                self.assertEqual(bm.derive_label(host), bm.FALLBACK_LABEL)


class ValidLabelTests(unittest.TestCase):
    def test_what_a_label_may_be(self) -> None:
        for label in (FIXTURE_LABEL, "m", "0", bm.FALLBACK_LABEL, "a" * 32):
            with self.subTest(label=label):
                self.assertTrue(bm.is_valid_label(label))

    def test_what_a_label_may_not_be(self) -> None:
        for label in (
            "",
            "Fixture-mac",
            "-fixture",
            "fixture-",
            "fixture--mac",
            "fixture.mac",
            "fixture mac",
            "fixture-mac\n",
            "a" * (bm.MAX_LABEL_LENGTH + 1),
            "..",
        ):
            with self.subTest(label=label):
                self.assertFalse(bm.is_valid_label(label))


class IdentityFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())) / "base"
        self.path = bm.identity_path(self.base)

    def test_the_identity_file_is_the_machine_file_under_the_state_base(self) -> None:
        self.assertEqual(self.path, self.base / bm.IDENTITY_NAME)
        self.assertEqual(bm.IDENTITY_NAME, "machine")

    def test_the_first_run_derives_and_keeps_one_line(self) -> None:
        self.assertEqual(
            bm.ensure_identity(self.base, _host(FIXTURE_HOST)), FIXTURE_LABEL
        )
        self.assertEqual(self.path.read_text(encoding="utf-8"), f"{FIXTURE_LABEL}\n")

    def test_a_later_run_reads_the_kept_identity_whatever_the_host_is_now(
        self,
    ) -> None:
        bm.ensure_identity(self.base, _host(FIXTURE_HOST))
        renamed = _host("Fixture-Mac-2.local")
        self.assertEqual(bm.ensure_identity(self.base, renamed), FIXTURE_LABEL)
        self.assertEqual(bm.read_identity(self.base, renamed), FIXTURE_LABEL)

    def test_a_hand_edited_valid_identity_is_honored(self) -> None:
        self.base.mkdir()
        self.path.write_text("  fixture-studio \n", encoding="utf-8", newline="\n")
        host = _host(FIXTURE_HOST)
        self.assertEqual(bm.ensure_identity(self.base, host), "fixture-studio")
        self.assertEqual(bm.read_identity(self.base, host), "fixture-studio")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "  fixture-studio \n")

    def test_an_invalid_identity_is_an_error_and_is_never_rewritten(self) -> None:
        self.base.mkdir()
        for content in (b"Fixture Mac\n", b"", b"a\nb\n", b"\xff\n", b"a" * 5000):
            with self.subTest(content=content[:20]):
                self.path.write_bytes(content)
                for reader in (bm.ensure_identity, bm.read_identity):
                    with self.assertRaises(bm.MachineError) as raised:
                        reader(self.base, _host(FIXTURE_HOST))
                    self.assertIn(str(self.path), str(raised.exception))
                self.assertEqual(self.path.read_bytes(), content)

    def test_an_identity_that_cannot_be_read_is_an_error(self) -> None:
        self.path.mkdir(parents=True)
        with self.assertRaises(bm.MachineError) as raised:
            bm.read_identity(self.base, _host(FIXTURE_HOST))
        self.assertIn("cannot be read", str(raised.exception))

    def test_reading_without_a_file_derives_and_writes_nothing(self) -> None:
        self.assertEqual(
            bm.read_identity(self.base, _host(FIXTURE_HOST)), FIXTURE_LABEL
        )
        self.assertFalse(self.base.exists())

    def test_an_identity_that_cannot_be_kept_is_an_error(self) -> None:
        def refuse(path: Path, text: str) -> None:
            raise PermissionError(13, "fixture refusal", str(path))

        with self.assertRaises(bm.MachineError) as raised:
            bm.ensure_identity(self.base, _host(FIXTURE_HOST), write_text=refuse)
        self.assertIn("could not keep", str(raised.exception))
        self.assertFalse(self.path.exists())

    def test_an_identity_that_does_not_read_back_is_an_error(self) -> None:
        def write_elsewhere(path: Path, text: str) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture-other\n", encoding="utf-8", newline="\n")

        with self.assertRaises(bm.MachineError) as raised:
            bm.ensure_identity(
                self.base, _host(FIXTURE_HOST), write_text=write_elsewhere
            )
        self.assertIn("does not read back", str(raised.exception))

    def test_a_fifo_in_place_of_the_identity_is_refused_without_waiting(self) -> None:
        self.base.mkdir()
        _make_fifo(self.path)
        with self.assertRaises(bm.MachineError):
            bm.read_identity(self.base, _host(FIXTURE_HOST))


if __name__ == "__main__":
    unittest.main()
