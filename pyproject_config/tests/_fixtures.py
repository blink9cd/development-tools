#!/usr/bin/env python3
"""Fixtures shared by more than one of pyproject_config's grouped suites.

Deliberately not named ``test_*``: it holds no test cases, and the aggregator's
``discover`` pattern must not try to load it as one. Anything used by a single suite
stays in that suite; only what genuinely crosses suite boundaries lives here.

Nothing here touches the filesystem. The two seams are the library's injected
``is_file`` / ``read_text`` in their "no file anywhere" form, which is what lets a
case drive the absent-config branch without a real tree: black-box tests must not
reach the host.
"""

from __future__ import annotations

from pathlib import Path

# Table names deliberately not any real tool's, so a hardcoded fallback table name
# cannot accidentally satisfy a test that means to prove the table
# name is a parameter. They live here rather than per-suite so that reason is stated
# once, instead of two suites independently picking a name for reasons nobody records.
TABLE = "widget_gate"
OTHER_TABLE = "other_gate"


def never_a_file(_path: Path) -> bool:
    """is_file seam: nothing exists anywhere up the chain."""
    return False


def unreadable(_path: Path) -> str:
    """read_text seam that must never be called (no file was ever found)."""
    raise AssertionError("read_text must not be called when no pyproject exists")
