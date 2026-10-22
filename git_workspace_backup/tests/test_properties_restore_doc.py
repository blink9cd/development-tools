"""Grouped suite: property tests for backup_restore_doc's renderer and sh-block extractor.

backup_rearm's command and setup-document rules are here too, because what they guard is
the renderer's re-arm step.

The conventions are test_properties.py's: every ``# fuzz-target`` subject has a module-level
``@given`` ``test_fuzz_<name>`` here, run by the TestCase at the bottom, and the shared
deterministic profile is loaded before any ``@given`` is defined. Coverage is carried by
test_restore_doc.py.

What they are FOR: only an ``sh`` fence in RESTORE.md is ever run. So the extractor must
return exactly the bodies of well-formed ``sh`` fences -- a fence left open, or closed by a
line that is not exactly the marker, must not swallow the blocks after it -- and the renderer
must never let a manifest value change which fences exist, send a restore outside its root,
or give two blocks write targets where one lies within the other.

Each rule is restated here rather than read off the subject, so a wrong predicate cannot
also be the thing that judges it: the extraction rule as a line scan, fence breakers and heads
as regular expressions, containment through ``posixpath.normpath``, nesting through
``PurePosixPath.is_relative_to``. The alphabets are dense in line breaks, backticks, dots and
slashes, because random text rarely spells a fence marker or a ``..`` component.
"""

from __future__ import annotations

import ntpath
import posixpath
import re
import shlex
import unittest
from pathlib import PurePosixPath

from hypothesis import example, given
from hypothesis import strategies as st

import _fixtures as fx  # noqa: F401 - installs the src/ bootstrap
import _hypothesis_profile  # noqa: F401 - loads the profile before any @given below

import backup_rearm as brr
import backup_restore_doc as brd
from backup_model import BundleEntry, FileEntry, Manifest, WorktreeHead

_CREATED = "2026-09-14T06:00:00+00:00"
_FENCE = "```"
_SH_OPENER = f"{_FENCE}sh"
#: What can close a fence early: a line break of either kind, or a fence marker.
_FENCE_BREAKER = re.compile(r"[\n\r]|```")
#: A head is empty (an unborn HEAD) or a full lowercase SHA-1 or SHA-256 object name.
_HEAD_RULE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})?")
#: Every fence line the renderer writes: an sh or a text opener, or a bare closer.
_FENCE_LINES = frozenset({_FENCE, _SH_OPENER, f"{_FENCE}text"})
#: The lines every bundle block holds besides its optional checkout and fetches: the
#: subshell's "(", "set -eu" and ")", the two absolute-path guards, the clone and the two
#: config commands.
_FIXED_BUNDLE_LINES = 8
#: A recorded head adds two lines: the checkout, and the check that HEAD is that head.
_HEAD_LINES = 2
#: The manual step naming what the restore places under a project slug, and the names it is
#: rendered for: memory bundles, and archive members inside a slug directory.
_SLUG_HEADING = "## Claude project folders"
_PROJECTS_PREFIX = "claude/projects/"
_MEMORY_PREFIX = "memory/"
_BUNDLE_SUFFIX = ".bundle"
_TEXT_OPENER = f"{_FENCE}text"
#: The empty-RESTORE_ROOT check, which only the first block holds.
_FIRST_BLOCK_LINES = 1
#: The directory beneath RESTORE_ROOT the files block writes, restated from the template.
_EXTRAS_TARGET = "extras"
_ATTACK_NAMES = (
    "extras.bundle",
    "claude.bundle",
    "workspace/evil.bundle",
    "memory/a/b.bundle",
    "...bundle",
    "x/...bundle",
    "memory/...bundle",
    "memory/.bundle",
)

_INFOS = st.sampled_from(("sh", "text", "shell", "bash", "sh x", "sh ", ""))
#: Body lines: anything but a line break, dense in lines that begin with a fence marker.
_LINES = st.one_of(
    st.sampled_from((_FENCE, _SH_OPENER, f"{_FENCE} ", f"{_FENCE}text", "````")),
    st.text(alphabet="`sh x~\r$", max_size=8),
    st.text(max_size=8),
).filter(lambda line: "\n" not in line)
_BLOCKS = st.lists(st.tuples(_INFOS, st.lists(_LINES, max_size=3)), max_size=4)

_NAMES = st.one_of(
    st.sampled_from(("wt", "x.", "-p", "workspace")),
    st.text(alphabet="ab.`$'\n\r-/", max_size=8),
    st.text(max_size=6),
)
_BUNDLE_NAMES = st.one_of(
    _NAMES.map(lambda name: f"{name}.bundle"),
    _NAMES.map(lambda name: f"memory/{name}.bundle"),
    _NAMES,
    st.sampled_from(("workspace.bundle", "memory/a.bundle", *_ATTACK_NAMES)),
)
_HEADS = st.one_of(
    st.sampled_from(("", "a" * 40, "0" * 64, "A" * 40, "a" * 41)),
    st.text(alphabet="0123456789abcdefA", min_size=40, max_size=40),
    st.text(max_size=4),
)
_WORKTREES = st.lists(
    st.builds(WorktreeHead, _NAMES, st.just("b" * 40), st.booleans()), max_size=2
).map(tuple)
_BUNDLES = st.lists(
    st.builds(BundleEntry, _BUNDLE_NAMES, _HEADS, st.just(()), _WORKTREES),
    max_size=3,
).map(tuple)
_MEMBERS = st.lists(
    st.one_of(
        _NAMES.map(lambda name: f"claude/projects/{name}/f"),
        _NAMES.map(lambda name: f"claude/projects/{name}"),
        st.sampled_from(("claude/settings.json", "home/.zshrc", "claude/projects/a/b")),
        _NAMES,
    ).map(lambda name: FileEntry(name, "0" * 64, 1)),
    max_size=3,
).map(tuple)
_CREATEDS = st.one_of(st.just(_CREATED), st.text(alphabet="0T:`\n\r", max_size=6))
#: A re-arm command the renderer must refuse: empty, or holding a C0 or C1 control character
#: (line breaks among them), a Unicode line or paragraph separator, a format character (bidi
#: controls, zero-width characters, the BOM: Unicode 15's category Cf, listed out), or a
#: fence marker.
_REARM_BREAKER = re.compile(
    "[\x00-\x1f\x7f-\x9f\u2028\u2029"
    "\u00ad\u0600-\u0605\u061c\u06dd\u070f\u0890\u0891\u08e2\u180e\u200b-\u200f"
    "\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff\ufff9-\ufffb\U000110bd\U000110cd"
    "\U00013430-\U0001343f\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0001"
    "\U000e0020-\U000e007f]|```"
)
#: Format characters Unicode 15 added, unassigned in 3.11's Unicode 14: random
#: text leaves them out, so the listing above judges the same on every supported version.
_LATER_FORMAT_CHARACTERS = [chr(code) for code in range(0x13439, 0x13440)]
_REARM_HEADING = "## What a bundle does not carry"
_REARM_CD = 'cd "$RESTORE_ROOT/workspace"'
_COMMANDS = st.one_of(
    st.sampled_from(
        ("python a.py --mode install", 'x "$PWD"', "", "```", "a\x00b", "a\u2028b")
    ),
    st.text(alphabet="ab `~$\n\r\x1b\x85\u2029\u202e\u200b\ufeff", max_size=8),
    st.text(
        alphabet=st.characters(exclude_characters=_LATER_FORMAT_CHARACTERS), max_size=6
    ),
)
#: Setup documents: dense in slashes, dots, backticks and breaks, as a climb or a stray
#: backtick is what the rule exists to refuse.
_SETUP_DOCS = st.one_of(
    st.none(),
    st.sampled_from(("docs/SETUP.md", "../SETUP.md", "/opt/SETUP.md", "a`b", "")),
    st.text(alphabet="aC:./\\`\n\u202e", max_size=8),
)
_REARMS = st.one_of(
    st.none(),
    st.builds(
        brr.ReArm,
        st.lists(_COMMANDS, max_size=3).map(tuple),
        st.booleans(),
        _SETUP_DOCS,
    ),
)


def _doc_of(blocks: "list[tuple[str, list[str]]]") -> str:
    """Markdown holding each (info string, body lines) block as a fence, prose between."""
    return "".join(
        f"prose\n\n{_FENCE}{info}\n"
        + "".join(f"{line}\n" for line in body)
        + f"{_FENCE}\n\n"
        for info, body in blocks
    )


def _scanned_sh_bodies(lines: "list[str]") -> tuple[str, ...]:
    """The extraction rule, restated as a scan over *lines*.

    An opener is a line that is exactly ```sh. The first later line beginning with ``` ends
    its block when it is exactly ```; any other such line -- or none -- yields nothing from
    that opener, and the scan resumes on the line after it.
    """
    bodies: list[str] = []
    index = 0
    while index < len(lines):
        if lines[index] != _SH_OPENER:
            index += 1
            continue
        rest = range(index + 1, len(lines))
        end = next((at for at in rest if lines[at].startswith(_FENCE)), None)
        if end is None or lines[end] != _FENCE:
            index += 1
            continue
        bodies.append("".join(f"{line}\n" for line in lines[index + 1 : end]))
        index = end + 1
    return tuple(bodies)


def _is_beneath_its_root(relative: str) -> bool:
    """Whether *relative*, joined onto a root directory, names a path strictly beneath it.

    Judged by component, so ``..a`` -- an ordinary directory name -- is beneath its root.
    """
    normalized = posixpath.normpath(relative)
    return (
        not posixpath.isabs(relative)
        and normalized != "."
        and normalized.split("/")[0] != ".."
    )


def _is_equal_or_nested(first: str, second: str) -> bool:
    """Whether either relative path is, or lies within, the other."""
    first_path, second_path = PurePosixPath(first), PurePosixPath(second)
    return first_path.is_relative_to(second_path) or second_path.is_relative_to(
        first_path
    )


def _bundle(name: str) -> BundleEntry:
    return BundleEntry(name, "a" * 40, (), ())


def _archived_slugs(members: "tuple[FileEntry, ...]") -> "list[str]":
    """Each slug directory the archive holds something inside, first-seen order, once."""
    slugs: "list[str]" = []
    for member in members:
        rest = member.name.removeprefix(_PROJECTS_PREFIX)
        if rest != member.name and "/" in rest and rest.split("/")[0] not in slugs:
            slugs.append(rest.split("/")[0])
    return slugs


def _slug_step_words(lines: "list[str]") -> "list[list[str]]":
    """Each line of the text fence after the slug step heading, split as a shell reads it."""
    start = lines.index(_SLUG_HEADING)
    opener = lines.index(_TEXT_OPENER, start)
    closer = lines.index(_FENCE, opener)
    return [shlex.split(line) for line in lines[opener + 1 : closer]]


@given(_BLOCKS)
@example([("sh", ["echo one"]), ("text", ['cp -R home/. "$HOME/"']), ("sh", [])])
@example([("sh", ["echo a", "```text", 'cp -R home/. "$HOME/"']), ("sh", ["echo b"])])
@example([("sh", ["echo a", "``` "]), ("text", ['cp -R home/. "$HOME/"'])])
def test_fuzz_extract_sh_blocks(blocks: "list[tuple[str, list[str]]]") -> None:
    """Exactly the well-formed ``sh`` fences' bodies, in order; nothing a broken one swallows."""
    doc = _doc_of(blocks)
    extracted = brd.extract_sh_blocks(doc)
    assert extracted == _scanned_sh_bodies(doc.split("\n"))
    if not any(line.startswith(_FENCE) for _, body in blocks for line in body):
        expected = tuple(
            "".join(f"{line}\n" for line in body)
            for info, body in blocks
            if info == "sh"
        )
        assert extracted == expected


@given(_BUNDLES, _CREATEDS, _MEMBERS)
@example(
    (
        BundleEntry(
            "workspace.bundle", "a" * 40, (), (WorktreeHead("wt", "b" * 40, False),)
        ),
        _bundle("memory/a.bundle"),
    ),
    _CREATED,
    (),
)
@example(
    (
        BundleEntry(
            "workspace.bundle",
            "a" * 40,
            (),
            (WorktreeHead("wt\n```\necho pwned", "b" * 40, False),),
        ),
    ),
    _CREATED,
    (),
)
@example((BundleEntry("x```.bundle", "", (), ()),), _CREATED, ())
@example((BundleEntry("workspace.bundle", "A" * 40, (), ()),), _CREATED, ())
@example((_bundle("...bundle"),), _CREATED, ())
@example((_bundle("memory/...bundle"),), _CREATED, ())
@example((_bundle(".bundle"),), _CREATED, ())
@example((_bundle("workspace.bundle"), _bundle("workspace/evil.bundle")), _CREATED, ())
@example((_bundle("extras.bundle"),), _CREATED, ())
@example((_bundle("memory/a.bundle"), _bundle("memory/a.bundle")), _CREATED, ())
@example(
    (_bundle("workspace.bundle"), _bundle("memory/-m q'$(x)`y`.bundle")), _CREATED, ()
)
@example(
    (_bundle("memory/-p.bundle"),),
    _CREATED,
    tuple(
        FileEntry(name, "0" * 64, 1)
        for name in (
            "claude/projects/-p/k/f",
            "claude/projects/-m q'$(x)/f",
            "claude/projects/a\nb/f",
        )
    ),
)
def test_fuzz_render_restore_doc(
    bundles: "tuple[BundleEntry, ...]",
    created: str,
    members: "tuple[FileEntry, ...]",
) -> None:
    """Refuses, or renders fences exactly as written, each target beneath its root and apart."""
    manifest = Manifest(created, True, bundles, (), members, (), (), (), ())
    texts = [created, *(entry.name for entry in bundles), *_archived_slugs(members)]
    texts += [head.name for entry in bundles for head in entry.worktree_heads]
    try:
        doc = brd.render_restore_doc(manifest)
    except brd.RestoreDocError:
        return
    assert not any(_FENCE_BREAKER.search(text) for text in texts)
    assert all(_HEAD_RULE.fullmatch(entry.head) for entry in bundles)
    for entry in bundles:
        assert _is_beneath_its_root(entry.name), entry.name
        assert _is_beneath_its_root(brd.restore_dir(entry.name)), entry.name
    targets = [_EXTRAS_TARGET, *(brd.restore_dir(entry.name) for entry in bundles)]
    for index, first in enumerate(targets):
        for second in targets[index + 1 :]:
            assert not _is_equal_or_nested(first, second), (first, second)
    fence_lines = {line for line in doc.split("\n") if line.startswith(_FENCE)}
    assert fence_lines <= _FENCE_LINES
    blocks = brd.extract_sh_blocks(doc)
    assert len(blocks) == len(bundles) + 1
    for index, (entry, block) in enumerate(zip(bundles, blocks)):
        lines = _FIXED_BUNDLE_LINES + _HEAD_LINES * bool(entry.head)
        lines += len(entry.worktree_heads)
        lines += _FIRST_BLOCK_LINES if index == 0 else 0
        assert block.count("\n") == lines
    # The slug step is there exactly when a memory bundle or an archived slug directory is,
    # and names each corpus, then each archived slug directory, as one shell word.
    doc_lines = doc.split("\n")
    slugs = [
        entry.name[len(_MEMORY_PREFIX) : -len(_BUNDLE_SUFFIX)]
        for entry in bundles
        if entry.name.startswith(_MEMORY_PREFIX)
    ]
    archived = _archived_slugs(members)
    assert (_SLUG_HEADING in doc_lines) == bool(slugs or archived)
    if slugs or archived:
        expected = [[f"$RESTORE_ROOT/claude/projects/{slug}/memory"] for slug in slugs]
        expected += [
            [f"$RESTORE_ROOT/extras/claude/projects/{slug}"] for slug in archived
        ]
        assert _slug_step_words(doc_lines) == expected


def _rearm_fence(lines: "list[str]") -> "list[str]":
    """The lines inside the text fence after the re-arm heading."""
    opener = lines.index(_TEXT_OPENER, lines.index(_REARM_HEADING))
    return lines[opener + 1 : lines.index(_FENCE, opener + 1)]


def _setup_doc_breaks(setup_doc: "str | None") -> bool:
    """Whether the renderer must refuse *setup_doc*: the command rule, a backtick, or a path
    absolute on either platform (a leading root under either separator, or a drive) or
    holding a ``..`` component under either separator.

    The drive test is ``ntpath.splitdrive``, which treats any ``X:`` prefix as a drive on every
    supported interpreter; pathlib's Windows parser accepted only an ASCII letter there before
    3.12, so it would judge ``.:x`` differently by version."""
    if setup_doc is None:
        return False
    if not setup_doc or _REARM_BREAKER.search(setup_doc) or "`" in setup_doc:
        return True
    if setup_doc[:1] in ("/", "\\") or ntpath.splitdrive(setup_doc)[0]:
        return True
    return ".." in re.split(r"[/\\]", setup_doc)


@given(_BUNDLES, _REARMS)
@example((), brr.ReArm(("ok", "x\n```sh\necho pwned"), configured=True))
@example((), brr.ReArm(("a\u202eb", "a\u200bb", "\ufeffa"), configured=True))
@example((), brr.ReArm(("ok",), configured=True, setup_doc="docs/../../x.md"))
@example((), brr.ReArm(("ok",), configured=False, setup_doc="docs/SETUP.md"))
@example((), brr.ReArm(("a\x85b",), configured=False))
@example((), brr.ReArm(("ok",), configured=True, setup_doc=".:x.md"))
@example((), brr.ReArm(("ok",), configured=False, setup_doc="::x"))
@example((_bundle("workspace.bundle"),), brr.ReArm((), configured=True))
def test_fuzz_rearm_command_problem(
    bundles: "tuple[BundleEntry, ...]", rearm: "brr.ReArm | None"
) -> None:
    """The rules refuse exactly what could break the fence or the prose; the renderer applies them.

    Rendered, re-arm commands never change which fences exist, and each is one line of the
    re-arm step's text fence, after the line changing into the restored workspace. A setup
    document that renders is named in the step's prose, in backticks.
    """
    commands = () if rearm is None else rearm.commands
    setup_doc = None if rearm is None else rearm.setup_doc
    for command in commands:
        breaks = not command or bool(_REARM_BREAKER.search(command))
        assert (brr.rearm_command_problem(command) is not None) == breaks, command
    manifest = Manifest(_CREATED, True, bundles, (), (), (), (), (), ())
    try:
        baseline = brd.render_restore_doc(manifest)
    except brd.RestoreDocError:
        return
    refused = any(not c or _REARM_BREAKER.search(c) for c in commands)
    refused = refused or _setup_doc_breaks(setup_doc)
    try:
        doc = brd.render_restore_doc(manifest, rearm=rearm)
    except brd.RestoreDocError:
        assert refused, commands
        return
    assert not refused, commands
    lines = doc.split("\n")
    assert {line for line in lines if line.startswith(_FENCE)} <= _FENCE_LINES
    assert brd.extract_sh_blocks(doc) == brd.extract_sh_blocks(baseline)
    fence = _rearm_fence(lines)
    assert fence[0] == _REARM_CD
    if rearm is not None:
        assert fence[1:] == list(rearm.commands)
    if setup_doc is not None:
        prose = " ".join(
            doc[doc.index(_REARM_HEADING) :].split(_TEXT_OPENER)[0].split()
        )
        assert f"`{setup_doc}`" in prose, setup_doc


@given(_SETUP_DOCS)
@example("docs/../../x.md")
@example("..\\x.md")
@example("C:x.md")
@example("a\u200bb")
@example(".:x.md")
@example("::x")
def test_fuzz_setup_doc_problem(setup_doc: "str | None") -> None:
    """The setup-document rule refuses exactly what could break the prose or leave the root."""
    if setup_doc is not None:
        refuses = brr.setup_doc_problem(setup_doc) is not None
        assert refuses == _setup_doc_breaks(setup_doc), setup_doc


class RestoreDocPropertiesRunUnderUnittestTest(unittest.TestCase):
    """Runs the module-level properties, which unittest would otherwise never collect."""

    def test_extract_sh_blocks_property_holds(self) -> None:
        test_fuzz_extract_sh_blocks()

    def test_render_restore_doc_property_holds(self) -> None:
        test_fuzz_render_restore_doc()

    def test_rearm_command_problem_property_holds(self) -> None:
        test_fuzz_rearm_command_problem()

    def test_setup_doc_problem_property_holds(self) -> None:
        test_fuzz_setup_doc_problem()
