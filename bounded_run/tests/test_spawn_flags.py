"""The spawn creation flags: suspended on win32, plus no console when asked, and 0 elsewhere.

The pure helper is pinned per platform on every host. The two spawn functions are pinned by a
``Popen`` recorder that raises before any process exists, so no child is ever started here.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import bounded_run
from boundedrun_spawn_flags import (
    CREATE_NO_WINDOW,
    CREATE_SUSPENDED,
    spawn_creationflags,
)

#: The documented Win32 values, spelled out so a changed constant fails here.
_SUSPENDED = 0x00000004
_SUSPENDED_NO_WINDOW = 0x08000004


class _Spawned(Exception):
    """Raised by the Popen recorder, so the spawn functions stop before any process exists."""


def _record_popen(calls: list[dict[str, Any]]) -> Callable[..., object]:
    """A Popen stand-in that records its keyword arguments, then raises :class:`_Spawned`."""

    def popen(*_args: object, **kwargs: Any) -> object:
        calls.append(kwargs)
        raise _Spawned

    return popen


class SpawnCreationflagsTests(unittest.TestCase):
    def test_the_constants_are_the_documented_win32_values(self) -> None:
        self.assertEqual((CREATE_SUSPENDED, CREATE_NO_WINDOW), (0x00000004, 0x08000000))

    def test_win32_without_hide_window_is_only_suspended(self) -> None:
        self.assertEqual(
            spawn_creationflags(platform="win32", hide_window=False), _SUSPENDED
        )

    def test_win32_with_hide_window_adds_no_window(self) -> None:
        self.assertEqual(
            spawn_creationflags(platform="win32", hide_window=True),
            _SUSPENDED_NO_WINDOW,
        )

    def test_darwin_is_zero_even_with_hide_window(self) -> None:
        self.assertEqual(spawn_creationflags(platform="darwin", hide_window=True), 0)

    def test_linux_is_zero(self) -> None:
        self.assertEqual(spawn_creationflags(platform="linux", hide_window=False), 0)


class SpawnPassesTheFlagsTests(unittest.TestCase):
    def _spawn_flags(self, spawn: Callable[[], object]) -> object:
        """Run *spawn* against the Popen recorder; return the creationflags it was given."""
        calls: list[dict[str, Any]] = []
        with (
            mock.patch.object(subprocess, "Popen", _record_popen(calls)),
            self.assertRaises(_Spawned),
        ):
            spawn()
        self.assertEqual(len(calls), 1)
        return calls[0]["creationflags"]

    def test_run_bounded_passes_the_flags_to_popen(self) -> None:
        flags = self._spawn_flags(
            lambda: bounded_run.run_bounded(["x"], timeout=1, hide_window=True)
        )
        self.assertEqual(
            flags, spawn_creationflags(platform=sys.platform, hide_window=True)
        )

    def test_run_bounded_bytes_passes_the_flags_to_popen(self) -> None:
        flags = self._spawn_flags(
            lambda: bounded_run.run_bounded_bytes(["x"], timeout=1, hide_window=True)
        )
        self.assertEqual(
            flags, spawn_creationflags(platform=sys.platform, hide_window=True)
        )

    def test_both_forward_hide_window_and_default_it_off(self) -> None:
        # Off win32 both flag values are 0, so the assertions above cannot tell a dropped
        # hide_window from a forwarded one. A fake that echoes its arguments can, on any host.
        def echo(*, platform: str, hide_window: bool) -> object:
            return (platform, hide_window)

        cases: list[tuple[Callable[[], object], bool]] = [
            (lambda: bounded_run.run_bounded(["x"], timeout=1, hide_window=True), True),
            (lambda: bounded_run.run_bounded(["x"], timeout=1), False),
            (
                lambda: bounded_run.run_bounded_bytes(
                    ["x"], timeout=1, hide_window=True
                ),
                True,
            ),
            (lambda: bounded_run.run_bounded_bytes(["x"], timeout=1), False),
        ]
        with mock.patch.object(bounded_run, "spawn_creationflags", echo):
            for spawn, expected in cases:
                with self.subTest(expected=expected):
                    self.assertEqual(self._spawn_flags(spawn), (sys.platform, expected))


if __name__ == "__main__":
    unittest.main()
