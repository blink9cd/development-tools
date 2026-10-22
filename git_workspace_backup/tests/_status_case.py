#!/usr/bin/env python3
"""Shared status facts: a healthy destination, the snapshot names around it, and the clock.

Deliberately NOT named ``test_*``, so the aggregator's discover skips it. Both status suites
build every case as a ``replace`` of :data:`HEALTHY`, so each case differs from health in
exactly the fields it names.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import _fixtures as fx  # noqa: F401 - installs the src/ bootstrap

import backup_icloud as bi
import backup_status as bs
from backup_model import (
    OUTCOME_COMPLETE,
    OUTCOME_SKIPPED,
    STATE_PROMOTED,
    FileEntry,
    Ledger,
    LedgerEntry,
    Manifest,
    RunRecord,
)

#: This machine's identity in every status case, and another machine's: obvious fixtures.
MACHINE = "fixture-mac"
OTHER_MACHINE = "fixture-studio"
NEWEST = "2026-09-14T060000"
OLDER = "2026-09-13T060000"
LATER = "2026-09-14T090000"
#: The moment NEWEST records.
TAKEN = datetime(2026, 9, 14, 6, 0, 0)
NOW = datetime(2026, 9, 14, 12, 0, 0)
MINUTE = timedelta(minutes=1)
MANIFEST = Manifest(
    "c", True, (), (FileEntry("extras.zip", "0" * 64, 1),), (), (), (), (), ()
)
COMPLETE_RUN = RunRecord("t", OUTCOME_COMPLETE, "ok", NEWEST, ())
SKIPPED_RUN = RunRecord("t", OUTCOME_SKIPPED, "already done today", None, ())


def entry(name: str, *, complete: bool = True, sha: str = "") -> LedgerEntry:
    """A promoted ledger entry for *name*."""
    return LedgerEntry(name, STATE_PROMOTED, complete, sha)


HEALTHY = bs.StatusFacts(
    agent=bs.AGENT_LOADED,
    last_run=COMPLETE_RUN,
    ledger=Ledger((entry(NEWEST),)),
    keep=7,
    newest_complete=NEWEST,
    newest_manifest=MANIFEST,
    upload=bi.UPLOAD_UPLOADED,
    now=NOW,
    in_icloud_drive=True,
    machine=MACHINE,
)


def text_of(verdict: bs.Verdict) -> str:
    """Every line of *verdict*, joined for a substring check."""
    return "\n".join(verdict.lines)
