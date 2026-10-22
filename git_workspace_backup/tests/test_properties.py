"""Grouped suite: property tests for git_workspace_backup's untrusted-input parsers.

Every ``# fuzz-target`` subject has a module-level ``@given`` ``test_fuzz_<name>`` here, one
property per marked parser. They stay module-level so a fuzzing campaign can reach them
with getattr on the imported module; the TestCase at the bottom is what makes unittest run
them. The determinism profile (``_hypothesis_profile``, shared with every property suite)
is loaded before any ``@given`` runs, and as a profile rather than a decorator, because a
campaign may apply its own settings object and Hypothesis refuses a second one on a
decorated function.

What they are FOR: prune decides deletions by reading manifests and a ledger that may be
truncated, stale or foreign, so these parsers must never raise, must accept only names that
stay inside the directory they are joined to, must refuse a record with any one field
missing or of the wrong kind (a manifest's, in test_properties_manifest_fields.py), and
whatever they accept must survive a serialize-parse round trip. Coverage is carried by
test_records.py.

Drift is judged from git's own output and from config patterns, so the status parser must
never raise, must list exactly the ignored records -- never a rename's original path, which
git prints as a bare record that can itself read like a status line -- and the pattern
matcher must agree with a direct backtracking search while never itself backtracking.
Coverage is carried by test_drift.py.

Why the input pool carries record-shaped arms and unsafe-name pools: random bytes and
random JSON essentially never form a valid ledger or run record, and random text almost
never forms a valid ledger state. Measured: with those alone, deleting the parser's ``/``
refusal left every property here green, because nothing generated ever reached the
acceptance path the assertions guard.

The name oracles below restate the rules rather than importing the parser's predicate, so a
wrong predicate cannot also be the thing that judges it.
"""

from __future__ import annotations

import json
import unittest
from collections.abc import Sequence
from fnmatch import fnmatchcase

from hypothesis import example, given
from hypothesis import strategies as st

import _fixtures as fx  # noqa: F401 - installs the src/ bootstrap
import _hypothesis_profile  # noqa: F401 - loads the profile before any @given below

import backup_drift as bd
import backup_ledger as bl
import backup_manifest as bm
from backup_model import (
    LEDGER_STATES,
    OUTCOMES,
    BundleEntry,
    DirtyWorktree,
    FailedItem,
    FileEntry,
    Ledger,
    LedgerEntry,
    Manifest,
    RunRecord,
    WorktreeHead,
)

_TEXT = st.text(max_size=16)
_SHA = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)
#: Entry names the ledger must refuse: empty, dot names, separators, absolute, NUL.
_UNSAFE_NAMES = st.sampled_from(["", ".", "..", "a/b", "/abs", "../up", "nul\x00"])
#: Names a manifest must refuse: each is empty, absolute, or has an empty or dot component.
_ESCAPING_NAMES = st.sampled_from(
    ["", ".", "..", "/abs", "../x", "a/../../b", "a//b", "./a", "a/", "nul\x00"]
)
#: One plain path component: non-empty, not a dot name, no separator and no NUL.
_COMPONENT = st.text(
    alphabet=st.characters(exclude_characters="/\x00"), min_size=1, max_size=8
).filter(lambda part: part not in (".", ".."))
#: Machine labels: lowercase ASCII letters and digits in runs joined by single hyphens.
_MACHINES = st.from_regex(r"[a-z0-9]{1,8}(-[a-z0-9]{1,8}){0,2}", fullmatch=True)
#: Snapshot- or archive-relative POSIX names, such as ``memory/p.bundle``.
_RELATIVE_NAMES = st.lists(_COMPONENT, min_size=1, max_size=3).map("/".join)

#: JSON values of every kind; a field is broken by giving it one of a different kind.
_WRONG_KINDS = ("null", "true", "7", "1.5", '"s"', "[]", "{}")
#: Fields that legitimately hold more than one kind of value.
_ACCEPTED_KINDS = {"snapshot": frozenset({"str", "NoneType"})}


def _tuples(
    strategy: st.SearchStrategy[object],
) -> st.SearchStrategy[tuple[object, ...]]:
    return st.lists(strategy, max_size=3).map(tuple)


def _is_safe_relative_name(name: str) -> bool:
    """The manifest name rule, restated independently of the parser it judges."""
    return (
        bool(name)
        and "\x00" not in name
        and not name.startswith("/")
        and all(part not in ("", ".", "..") for part in name.split("/"))
    )


def _is_safe_entry(entry: LedgerEntry) -> bool:
    """The ledger entry rule: one safe component and a known state."""
    return (
        _is_safe_relative_name(entry.name)
        and "/" not in entry.name
        and entry.state in ("promoted", "incoming")
    )


def _manifest_names(manifest: Manifest) -> list[str]:
    """Every name the manifest parser must judge: files, archive members and bundles."""
    file_entries = (*manifest.files, *manifest.archive_members)
    return [entry.name for entry in file_entries] + [
        bundle.name for bundle in manifest.bundles
    ]


def _manifests(names: st.SearchStrategy[str]) -> st.SearchStrategy[Manifest]:
    """Manifests whose file, archive-member and bundle names are drawn from *names*."""
    file_entries = _tuples(
        st.builds(FileEntry, name=names, sha256=_SHA, size=st.integers(0, 2**40))
    )
    return st.builds(
        Manifest,
        created=_TEXT,
        complete=st.booleans(),
        bundles=_tuples(
            st.builds(
                BundleEntry,
                name=names,
                head=_SHA,
                refs=_tuples(_TEXT),
                worktree_heads=_tuples(
                    st.builds(
                        WorktreeHead, name=_TEXT, sha=_SHA, reachable=st.booleans()
                    )
                ),
            )
        ),
        files=file_entries,
        archive_members=file_entries,
        dirty_worktrees=_tuples(
            st.builds(
                DirtyWorktree, path_name=_TEXT, changed_paths=st.integers(-1, 10**6)
            )
        ),
        failed=_FAILED_ITEMS,
        skipped_symlinks=_tuples(_TEXT),
        unclassified_ignored=_tuples(_TEXT),
        machine=st.none() | _MACHINES,
    )


def _objects_in(value: object) -> list[dict[str, object]]:
    """Every JSON object in *value*, outermost first, in a stable order."""
    if isinstance(value, list):
        return [found for child in value for found in _objects_in(child)]
    if not isinstance(value, dict):
        return []
    return [value, *(found for child in value.values() for found in _objects_in(child))]


def _rewritten(
    document: str, index: int, key: str, value: object, delete: bool
) -> bytes:
    """*document* with field *key* of its *index*-th object deleted or set to *value*."""
    payload = json.loads(document)
    target = _objects_in(payload)[index]
    if delete:
        del target[key]
    else:
        target[key] = value
    return json.dumps(payload).encode()


def _chosen_field(document: str, choice: int) -> tuple[int, str]:
    """The (object index, key) of the *choice*-th field (modulo) across every object in
    *document*, nested entries included."""
    objects = _objects_in(json.loads(document))
    fields = [(index, key) for index, obj in enumerate(objects) for key in sorted(obj)]
    return fields[choice % len(fields)]


def _variants_with_one_field_broken(document: str, choice: int) -> list[bytes]:
    """*document* broken at one field: deleted first, then joined by an unexpected key,
    then given a value of each wrong kind -- one variant apiece.

    The field is _chosen_field's, so a reader that skips a check anywhere in the tree is
    reachable. The unexpected-key variant is what reaches a reader that stops calling
    require_keys: deleting a field or changing its kind is still refused by the typed
    getters alone.
    """
    objects = _objects_in(json.loads(document))
    index, key = _chosen_field(document, choice)
    accepted = _ACCEPTED_KINDS.get(key, frozenset({type(objects[index][key]).__name__}))
    wrong = [json.loads(text) for text in _WRONG_KINDS]
    deleted = _rewritten(document, index, key, None, delete=True)
    extra = _rewritten(document, index, "unexpected_key", 1, delete=False)
    return [deleted, extra] + [
        _rewritten(document, index, key, value, delete=False)
        for value in wrong
        if type(value).__name__ not in accepted
    ]


_FAILED_ITEMS = _tuples(st.builds(FailedItem, item=_TEXT, reason=_TEXT))
_MANIFESTS = _manifests(_RELATIVE_NAMES)
_ANY_NAME_MANIFESTS = _manifests(_ESCAPING_NAMES | _RELATIVE_NAMES | _TEXT)
#: Each field is valid about half the time, so both refusals and acceptance occur.
_LEDGER_ENTRIES = st.builds(
    LedgerEntry,
    name=_UNSAFE_NAMES | _TEXT,
    state=st.sampled_from(sorted(LEDGER_STATES)) | _TEXT,
    complete=st.booleans(),
    manifest_sha256=_SHA,
)
#: Unique by name: a ledger never holds a name twice, and refuses to be built that way.
_LEDGERS = st.lists(
    _LEDGER_ENTRIES, max_size=4, unique_by=lambda entry: entry.name
).map(lambda es: Ledger(tuple(es)))
_VALID_LEDGERS = st.lists(
    st.builds(
        LedgerEntry,
        name=_COMPONENT,
        state=st.sampled_from(sorted(LEDGER_STATES)),
        complete=st.booleans(),
        manifest_sha256=_SHA,
    ),
    max_size=3,
    unique_by=lambda entry: entry.name,
).map(lambda es: Ledger(tuple(es)))
_RUN_RECORDS = st.builds(
    RunRecord,
    finished=_TEXT,
    outcome=st.sampled_from(sorted(OUTCOMES)),
    detail=_TEXT,
    snapshot=st.none() | _TEXT,
    failed=_FAILED_ITEMS,
)
#: Arbitrary JSON documents, so the parsers see well-formed JSON of the wrong shape too.
_JSON = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=8),
    lambda children: (
        st.lists(children, max_size=4)
        | st.dictionaries(st.text(max_size=8), children, max_size=4)
    ),
    max_leaves=12,
).map(lambda value: json.dumps(value).encode())
_INPUTS = (
    st.binary(max_size=256)
    | _JSON
    | _ANY_NAME_MANIFESTS.map(lambda manifest: bm.manifest_to_json(manifest).encode())
    | _LEDGERS.map(lambda ledger: bl.ledger_to_json(ledger).encode())
    | _RUN_RECORDS.map(lambda record: bl.run_record_to_json(record).encode())
)
_CHOICE = st.integers(min_value=0, max_value=10**4)


@given(_INPUTS)
def test_fuzz_parse_manifest(data: bytes) -> None:
    """Never raises; anything accepted has safe names and survives a round trip."""
    parsed = bm.parse_manifest(data)
    if parsed is not None:
        assert bm.parse_manifest(bm.manifest_to_json(parsed).encode()) == parsed
        assert all(_is_safe_relative_name(name) for name in _manifest_names(parsed))


@given(_INPUTS)
def test_fuzz_parse_ledger(data: bytes) -> None:
    """Never raises; every accepted entry name is one safe path component."""
    parsed = bl.parse_ledger(data)
    if parsed is not None:
        assert bl.parse_ledger(bl.ledger_to_json(parsed).encode()) == parsed
        assert all(_is_safe_entry(entry) for entry in parsed.entries)


@given(_INPUTS)
def test_fuzz_parse_run_record(data: bytes) -> None:
    """Never raises; anything accepted survives a serialize-parse round trip."""
    parsed = bl.parse_run_record(data)
    if parsed is not None:
        assert bl.parse_run_record(bl.run_record_to_json(parsed).encode()) == parsed


@given(_MANIFESTS)
def test_every_manifest_round_trips(manifest: Manifest) -> None:
    """The writer and the reader agree on every well-formed manifest."""
    assert bm.parse_manifest(bm.manifest_to_json(manifest).encode()) == manifest


@given(_ANY_NAME_MANIFESTS)
def test_a_manifest_parses_only_when_every_name_is_safe(manifest: Manifest) -> None:
    """A generated manifest is accepted exactly when every joined name stays inside."""
    safe = all(_is_safe_relative_name(name) for name in _manifest_names(manifest))
    parsed = bm.parse_manifest(bm.manifest_to_json(manifest).encode())
    assert (parsed == manifest) if safe else (parsed is None)


@given(_LEDGERS)
def test_a_ledger_parses_only_when_every_entry_is_safe(ledger: Ledger) -> None:
    """A generated ledger is accepted exactly when all names and states are valid."""
    safe = all(_is_safe_entry(entry) for entry in ledger.entries)
    parsed = bl.parse_ledger(bl.ledger_to_json(ledger).encode())
    assert (parsed == ledger) if safe else (parsed is None)


@given(_RUN_RECORDS)
def test_every_run_record_round_trips(record: RunRecord) -> None:
    """The writer and the reader agree on every well-formed run record."""
    assert bl.parse_run_record(bl.run_record_to_json(record).encode()) == record


@given(_RUN_RECORDS.map(bl.run_record_to_json), _CHOICE)
def test_a_run_record_with_one_malformed_field_is_foreign(
    document: str, choice: int
) -> None:
    """Deleting any one field, or giving it a value of another kind, makes it foreign."""
    for variant in _variants_with_one_field_broken(document, choice):
        assert bl.parse_run_record(variant) is None, variant


@given(_VALID_LEDGERS.map(bl.ledger_to_json), _CHOICE)
def test_a_ledger_with_one_malformed_field_is_foreign(
    document: str, choice: int
) -> None:
    """Deleting any one field, or giving it a value of another kind, makes it foreign."""
    for variant in _variants_with_one_field_broken(document, choice):
        assert bl.parse_ledger(variant) is None, variant


#: One porcelain v1 status record: (status, path, the original path a rename or copy adds).
StatusRecord = tuple[str, str, str]
#: Paths git could print, including ones that themselves read like a status record.
_STATUS_PATHS = st.sampled_from(["!! x", "?? y", "R  z", "a/", "/", "d/e"]) | st.text(
    alphabet=st.characters(exclude_characters="\x00"), min_size=1, max_size=8
)
_STATUS_RECORDS = st.tuples(
    st.sampled_from(["!!", "??", " M", "A ", "R ", " R", "C ", "RM"]),
    _STATUS_PATHS,
    _STATUS_PATHS,
)


def _status_output(records: Sequence[StatusRecord]) -> str:
    """What ``git status --porcelain -z`` prints for *records*."""
    fields: list[str] = []
    for status, path, original in records:
        fields.append(f"{status} {path}")
        if {"R", "C"} & set(status):
            fields.append(original)
    return "".join(f"{field}\0" for field in fields)


def _ignored_in(records: Sequence[StatusRecord]) -> list[str]:
    """The ignored paths in *records*, sorted, trailing slashes removed, empties dropped."""
    return sorted(
        path.rstrip("/")
        for status, path, _ in records
        if status == "!!" and path.rstrip("/")
    )


_STATUS_OUTPUTS = (
    st.text(max_size=64)
    | st.lists(st.text(max_size=8), max_size=6).map("\0".join)
    | st.lists(_STATUS_RECORDS, max_size=6).map(_status_output)
)
#: Pattern and path components that exercise every rule: ``**``, wildcards, a character
#: class, a hidden-name glob, and the empty component a doubled slash makes.
_GLOB_PATTERNS = st.lists(
    st.sampled_from(["**", "*", "?", "a", "b", "a*", "[ab]", ".*_c", ""]), max_size=5
).map("/".join)
_GLOB_PATHS = st.lists(
    st.sampled_from(["a", "b", "ab", ".x_c", "z", ""]), min_size=1, max_size=5
).map("/".join)
_ANY_GLOB_TEXT = st.text(alphabet="ab*?[]!/.-", max_size=16) | st.text(max_size=16)


def _backtracking_match(pattern: tuple[str, ...], parts: tuple[str, ...]) -> bool:
    """The matching rule, restated as a direct recursive search over every split."""
    if not pattern:
        return True
    if pattern[0] == "**":
        return any(
            _backtracking_match(pattern[1:], parts[index:])
            for index in range(len(parts) + 1)
        )
    return (
        bool(parts)
        and fnmatchcase(parts[0], pattern[0])
        and _backtracking_match(pattern[1:], parts[1:])
    )


@given(_STATUS_OUTPUTS)
@example("!! /")
def test_fuzz_parse_ignored(out: str) -> None:
    """Never raises; every listed path is non-empty, NUL-free, unslashed and in order."""
    listed = bd.parse_ignored(out)
    assert list(listed) == sorted(listed)
    assert all(
        path and "\x00" not in path and not path.endswith("/") for path in listed
    )


@given(st.lists(_STATUS_RECORDS, max_size=6))
def test_parse_ignored_lists_exactly_the_ignored_records(
    records: list[StatusRecord],
) -> None:
    """No rename's original path is listed, however much it looks like a status record."""
    assert list(bd.parse_ignored(_status_output(records))) == _ignored_in(records)


@given(_GLOB_PATTERNS | _ANY_GLOB_TEXT, _GLOB_PATHS | _ANY_GLOB_TEXT)
def test_fuzz_pattern_matches(pattern: str, path: str) -> None:
    """Never raises; a pattern that covers a path covers everything beneath it."""
    if bd.pattern_matches(pattern, path):
        assert bd.pattern_matches(pattern, f"{path}/child")


@given(_GLOB_PATTERNS, _GLOB_PATHS)
@example("*", "ab")
@example("**/**/a", "a")
def test_pattern_matches_agrees_with_a_backtracking_search(
    pattern: str, path: str
) -> None:
    """The non-backtracking matcher decides every case as the direct search does.

    A pattern with no components matches nothing: it names no path at all.
    """
    components = tuple(part for part in pattern.split("/") if part)
    expected = bool(components) and _backtracking_match(
        components, tuple(path.split("/"))
    )
    assert bd.pattern_matches(pattern, path) is expected


class PropertiesRunUnderUnittestTest(unittest.TestCase):
    """Runs the module-level properties, which unittest would otherwise never collect."""

    def test_manifest_parser_property_holds(self) -> None:
        test_fuzz_parse_manifest()

    def test_ledger_parser_property_holds(self) -> None:
        test_fuzz_parse_ledger()

    def test_run_record_parser_property_holds(self) -> None:
        test_fuzz_parse_run_record()

    def test_manifest_round_trip_property_holds(self) -> None:
        test_every_manifest_round_trips()

    def test_manifest_name_safety_property_holds(self) -> None:
        test_a_manifest_parses_only_when_every_name_is_safe()

    def test_ledger_safety_property_holds(self) -> None:
        test_a_ledger_parses_only_when_every_entry_is_safe()

    def test_run_record_round_trip_property_holds(self) -> None:
        test_every_run_record_round_trips()

    def test_malformed_run_record_property_holds(self) -> None:
        test_a_run_record_with_one_malformed_field_is_foreign()

    def test_malformed_ledger_property_holds(self) -> None:
        test_a_ledger_with_one_malformed_field_is_foreign()

    def test_status_parser_property_holds(self) -> None:
        test_fuzz_parse_ignored()

    def test_status_parser_lists_exactly_the_ignored_records(self) -> None:
        test_parse_ignored_lists_exactly_the_ignored_records()

    def test_pattern_matcher_property_holds(self) -> None:
        test_fuzz_pattern_matches()

    def test_pattern_matcher_agrees_with_a_backtracking_search(self) -> None:
        test_pattern_matches_agrees_with_a_backtracking_search()
