"""Grouped suite: the malformed-field property for backup_manifest's parser.

The conventions are test_properties.py's, whose strategies and field-breaking helpers this
suite reuses: the shared deterministic profile is loaded before any ``@given`` is defined,
and the TestCase at the bottom is what makes unittest run the property. Kept out of that
file, which is near the length cap.

What it is for: prune decides deletions by reading manifests, so a manifest with any one
field missing, of the wrong kind, or joined by an unexpected key must parse as foreign,
with two exceptions: a bundle entry's missing ``stashes`` and the manifest's missing
``machine``, which must parse (the pre-stash and pre-identity rules below). Coverage is carried by test_records.py.

Why a deterministic case sits beside the property: the derandomized profile reaches any
given field only by chance, so a field the property mishandles can stay hidden when the
suite runs alone and surface only in the full run, as a bundle entry's ``stashes`` field
once did.
"""

from __future__ import annotations

import json
import unittest
from dataclasses import replace

from hypothesis import given

import _fixtures as fx  # noqa: F401 - installs the src/ bootstrap
import _hypothesis_profile  # noqa: F401 - loads the profile before any @given below

import backup_manifest as bm
from backup_model import BundleEntry, Manifest
from test_properties import (
    _CHOICE,
    _MANIFESTS,
    _chosen_field,
    _objects_in,
    _variants_with_one_field_broken,
)

#: The pre-stash snapshot rule: a bundle entry written before stash entries were recorded
#: has no ``stashes`` and reads as recording none, or every older snapshot would be kept
#: past its retention forever (backup_manifest's module docstring). Only bundle entries
#: carry this key, and only its deletion is exempt: a wrong kind or an unexpected key
#: beside it is still foreign. Restated, not imported from the parser, so the parser's own
#: constant cannot also be the thing that judges it.
_KEY_A_PRE_STASH_BUNDLE_LACKS = "stashes"
#: The pre-identity snapshot rule, by the same precedent: a manifest written before machine
#: identity has no ``machine`` and reads as naming none. Only its deletion is exempt.
_KEY_A_PRE_IDENTITY_MANIFEST_LACKS = "machine"
#: Every key whose deletion alone is exempt: exactly these two.
_KEYS_AN_OLDER_SNAPSHOT_LACKS = frozenset(
    {_KEY_A_PRE_STASH_BUNDLE_LACKS, _KEY_A_PRE_IDENTITY_MANIFEST_LACKS}
)

#: A minimal manifest holding one bundle, for driving the property at a chosen field.
_ONE_BUNDLE_MANIFEST = Manifest(
    created="2026-01-01T00:00:00Z",
    complete=True,
    bundles=(
        BundleEntry(
            name="memory/p.bundle",
            head="0" * 64,
            refs=("refs/heads/main",),
            worktree_heads=(),
        ),
    ),
    files=(),
    archive_members=(),
    dirty_worktrees=(),
    failed=(),
    skipped_symlinks=(),
    unclassified_ignored=(),
    machine="fixture-mac",
)


def _choice_of(document: str, key: str) -> int:
    """The choice that makes _variants_with_one_field_broken pick the first *key*."""
    objects = _objects_in(json.loads(document))
    fields = [name for obj in objects for name in sorted(obj)]
    return fields.index(key)


def _assert_one_malformed_field_is_foreign(document: str, choice: int) -> None:
    """The property's body, callable with a chosen field.

    Each exempt deletion is asserted accepted rather than skipped, so the exemption
    cannot quietly cover a variant the parser refuses, or outlive the rule it names.
    """
    deleted, *refused = _variants_with_one_field_broken(document, choice)
    if _chosen_field(document, choice)[1] in _KEYS_AN_OLDER_SNAPSHOT_LACKS:
        assert bm.parse_manifest(deleted) is not None, deleted
    else:
        refused.append(deleted)
    for variant in refused:
        assert bm.parse_manifest(variant) is None, variant


@given(_MANIFESTS.map(bm.manifest_to_json), _CHOICE)
def test_a_manifest_with_one_malformed_field_is_foreign(
    document: str, choice: int
) -> None:
    """Deleting any one field, or giving it a value of another kind, makes it foreign."""
    _assert_one_malformed_field_is_foreign(document, choice)


class ManifestFieldsPropertyTest(unittest.TestCase):
    """Runs the property, and drives its body at the fields that need a fixed example."""

    def test_malformed_manifest_property_holds(self) -> None:
        test_a_manifest_with_one_malformed_field_is_foreign()

    def test_the_property_holds_at_a_bundle_entry_stashes_field(self) -> None:
        """The property, driven at the field Hypothesis once reached only by chance."""
        document = bm.manifest_to_json(_ONE_BUNDLE_MANIFEST)
        _assert_one_malformed_field_is_foreign(
            document, _choice_of(document, _KEY_A_PRE_STASH_BUNDLE_LACKS)
        )

    def test_a_bundle_entry_without_stashes_reads_as_recording_none(self) -> None:
        """The exempt deletion parses to the same manifest: an empty stash list."""
        document = bm.manifest_to_json(_ONE_BUNDLE_MANIFEST)
        choice = _choice_of(document, _KEY_A_PRE_STASH_BUNDLE_LACKS)
        deleted = _variants_with_one_field_broken(document, choice)[0]
        self.assertNotIn(_KEY_A_PRE_STASH_BUNDLE_LACKS.encode(), deleted)
        self.assertEqual(bm.parse_manifest(deleted), _ONE_BUNDLE_MANIFEST)

    def test_the_property_holds_at_the_manifest_machine_field(self) -> None:
        """The pre-identity exemption, driven at its field rather than reached by chance."""
        document = bm.manifest_to_json(_ONE_BUNDLE_MANIFEST)
        _assert_one_malformed_field_is_foreign(
            document, _choice_of(document, _KEY_A_PRE_IDENTITY_MANIFEST_LACKS)
        )

    def test_a_manifest_without_machine_reads_as_naming_none(self) -> None:
        """The exempt deletion parses to the same manifest, naming no machine."""
        document = bm.manifest_to_json(_ONE_BUNDLE_MANIFEST)
        choice = _choice_of(document, _KEY_A_PRE_IDENTITY_MANIFEST_LACKS)
        deleted = _variants_with_one_field_broken(document, choice)[0]
        self.assertNotIn(b'"machine"', deleted)
        self.assertEqual(
            bm.parse_manifest(deleted), replace(_ONE_BUNDLE_MANIFEST, machine=None)
        )
