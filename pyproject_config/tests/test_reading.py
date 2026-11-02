#!/usr/bin/env python3
"""The two public readers: the raw walk, and the path-list resolution built on it.

What these tests are actually defending
---------------------------------------
Every assertion here corresponds to a way this library could be wrong *silently*,
where the consuming gate would keep reporting PASS:

* **The table name is a parameter, not a constant.** A reader generalized from one
  gate's config layer fails by keeping a hardcoded table name that happens to match
  that first consumer. Both
  readers are therefore made to read two different tables out of one document and
  required to give different answers: an implementation that ignored ``table=``
  and always read the first consumer's own table would pass a single-table test
  forever.
* **Absence is empty; malformed is fatal.** A missing pyproject, a missing table
  and a missing key are all legitimately "no config" and must yield an empty
  result. A file that cannot be parsed, or a key holding something that is not a
  list of strings, must raise. Treating unreadable config as empty is the false
  clean a gate exists to prevent, because it silently shrinks
  the set of things being checked.
* **Nearest wins, and the base is the file's own directory.** Entries resolve
  relative to the pyproject that supplied them, never to the process working
  directory, so the answer is a function of the target. The
  walk test places a pyproject at an ancestor and asserts the resolved paths are
  rooted there, which an implementation resolving against ``start`` would fail.
* **Every requested key comes back.** Consumers index the result directly
  (``lists[_ALLOW_KEY]``), so a key that is absent from the table must still be
  present in the mapping with an empty list rather than raising ``KeyError`` at
  the call site.

Every filesystem touch is injected (``is_file`` / ``read_text`` fakes), so nothing
here reads the real tree and both branches of each seam stay reachable.
"""

from __future__ import annotations

import errno
import os
import unittest
from pathlib import Path

import pyproject_config as pc
from _fixtures import OTHER_TABLE as _OTHER_TABLE
from _fixtures import TABLE as _TABLE
from _fixtures import never_a_file as _never_a_file
from _fixtures import unreadable as _unreadable


class ReadToolTableTest(unittest.TestCase):
    """The walk itself, exposed raw for consumers whose keys are not path lists.

    ``read_path_lists`` answers "what paths does my table declare?". A consumer
    whose table holds sub-tables (a scope-keyed mapping) cannot ask that question
    and still needs the identical walk: nearest pyproject above the target, absence
    is empty, malformed stops the gate, entries resolve against the file's own
    directory. Exposing the walk is what keeps that policy in one place instead of
    being re-decided, and re-broken, one folder over.

    The distinction these tests pin is the three-way one a raw reader must make and
    a path-list reader can collapse: no pyproject at all (``None``), a pyproject
    that declares no table (``{}``, and the caller still needs its path, for the
    resolution base and for error messages), and a pyproject that declares one.
    """

    def test_no_pyproject_anywhere_is_none(self) -> None:
        self.assertIsNone(
            pc.read_tool_table(
                Path("/proj/sub"),
                table=_TABLE,
                is_file=_never_a_file,
                read_text=_unreadable,
            )
        )

    def test_present_table_comes_back_with_its_pyproject(self) -> None:
        base = Path("/proj").resolve()
        found = pc.read_tool_table(
            Path("/proj/sub"),
            table=_TABLE,
            is_file=lambda p: p == base / "pyproject.toml",
            read_text=lambda _p: f'[tool.{_TABLE}]\nallow = ["a"]\n',
        )
        self.assertEqual(found, (base / "pyproject.toml", {"allow": ["a"]}))

    def test_a_pyproject_declaring_no_table_still_returns_its_path(self) -> None:
        """Distinct from ``None``: the file exists, so it is the resolution base.

        Collapsing these two would make a consumer unable to tell "nothing
        configures me" from "nothing configures me here", and would leave it with
        no directory to resolve a later-added entry against.
        """
        base = Path("/proj").resolve()
        found = pc.read_tool_table(
            Path("/proj"),
            table=_TABLE,
            is_file=lambda p: p == base / "pyproject.toml",
            read_text=lambda _p: f"[tool.{_OTHER_TABLE}]\nallow = []\n",
        )
        self.assertEqual(found, (base / "pyproject.toml", {}))

    def test_values_come_back_raw_rather_than_resolved(self) -> None:
        """The whole reason this exists: a SUB-TABLE survives, where a path list cannot.

        ``read_path_lists`` would reject this value as "not a list of strings". A
        raw reader must hand it over untouched so its consumer can apply its own
        shape rules to it.
        """
        base = Path("/proj").resolve()
        found = pc.read_tool_table(
            Path("/proj"),
            table=_TABLE,
            is_file=lambda p: p == base / "pyproject.toml",
            read_text=lambda _p: f'[tool.{_TABLE}.bases]\n"docs/x.md" = ["a", "b"]\n',
        )
        assert found is not None
        self.assertEqual(found[1], {"bases": {"docs/x.md": ["a", "b"]}})

    def test_the_table_name_selects_which_table_is_read(self) -> None:
        base = Path("/proj").resolve()
        body = f'[tool.{_TABLE}]\nk = "mine"\n[tool.{_OTHER_TABLE}]\nk = "theirs"\n'

        def is_file(path: Path) -> bool:
            return path == base / "pyproject.toml"

        mine = pc.read_tool_table(
            Path("/proj"), table=_TABLE, is_file=is_file, read_text=lambda _p: body
        )
        theirs = pc.read_tool_table(
            Path("/proj"),
            table=_OTHER_TABLE,
            is_file=is_file,
            read_text=lambda _p: body,
        )
        assert mine is not None and theirs is not None
        self.assertEqual((mine[1], theirs[1]), ({"k": "mine"}, {"k": "theirs"}))

    def test_nearest_pyproject_wins(self) -> None:
        deep = Path("/proj/sub").resolve()
        bodies = {
            deep / "pyproject.toml": f'[tool.{_TABLE}]\nk = "near"\n',
            Path("/proj").resolve() / "pyproject.toml": f'[tool.{_TABLE}]\nk = "far"\n',
        }
        found = pc.read_tool_table(
            deep,
            table=_TABLE,
            is_file=lambda p: p in bodies,
            read_text=lambda p: bodies[p],
        )
        self.assertEqual(found, (deep / "pyproject.toml", {"k": "near"}))

    def test_malformed_toml_raises(self) -> None:
        base = Path("/proj").resolve()
        with self.assertRaises(pc.ConfigError) as caught:
            pc.read_tool_table(
                Path("/proj"),
                table=_TABLE,
                is_file=lambda p: p == base / "pyproject.toml",
                read_text=lambda _p: "not = = valid",
            )
        self.assertIn("cannot read or parse", str(caught.exception))


class ReadPathListsTest(unittest.TestCase):
    """The public entry point: walk, parse, resolve every requested key."""

    def test_no_pyproject_yields_every_key_empty(self) -> None:
        lists = pc.read_path_lists(
            Path("/proj/sub"),
            table=_TABLE,
            keys=("allow", "exclude"),
            is_file=_never_a_file,
            read_text=_unreadable,
        )
        self.assertEqual(lists, {"allow": [], "exclude": []})

    def test_reads_every_requested_key_from_the_table(self) -> None:
        base = Path("/proj").resolve()
        body = (
            f"[tool.{_TABLE}]\n"
            'allow = ["kcov_cover"]\n'
            'exclude = ["vendor"]\n'
            'allow_absent = ["prospective"]\n'
        )
        lists = pc.read_path_lists(
            Path("/proj/sub"),
            table=_TABLE,
            keys=("allow", "exclude", "allow_absent"),
            is_file=lambda p: p == base / "pyproject.toml",
            read_text=lambda _p: body,
        )
        self.assertEqual(lists["allow"], [(base / "kcov_cover").resolve()])
        self.assertEqual(lists["exclude"], [(base / "vendor").resolve()])
        self.assertEqual(lists["allow_absent"], [(base / "prospective").resolve()])

    def test_the_table_name_selects_which_table_is_read(self) -> None:
        """The library's core claim: nothing about one consumer is hardcoded."""
        base = Path("/proj").resolve()
        body = (
            f"[tool.{_TABLE}]\n"
            'allow = ["mine"]\n'
            f"[tool.{_OTHER_TABLE}]\n"
            'allow = ["theirs"]\n'
        )

        def is_file(path: Path) -> bool:
            return path == base / "pyproject.toml"

        mine = pc.read_path_lists(
            Path("/proj"),
            table=_TABLE,
            keys=("allow",),
            is_file=is_file,
            read_text=lambda _p: body,
        )
        theirs = pc.read_path_lists(
            Path("/proj"),
            table=_OTHER_TABLE,
            keys=("allow",),
            is_file=is_file,
            read_text=lambda _p: body,
        )
        self.assertEqual(mine["allow"], [(base / "mine").resolve()])
        self.assertEqual(theirs["allow"], [(base / "theirs").resolve()])

    def test_absent_key_in_a_present_table_is_empty(self) -> None:
        base = Path("/proj").resolve()
        lists = pc.read_path_lists(
            Path("/proj"),
            table=_TABLE,
            keys=("allow",),
            is_file=lambda p: p == base / "pyproject.toml",
            read_text=lambda _p: f"[tool.{_TABLE}]\ndone_bar_stage = true\n",
        )
        self.assertEqual(lists, {"allow": []})

    def test_nearest_pyproject_wins_and_sets_the_base(self) -> None:
        """The walk stops at the first hit, and paths resolve against ITS directory."""
        deep = Path("/proj/sub").resolve()
        bodies = {
            deep / "pyproject.toml": f'[tool.{_TABLE}]\nallow = ["near"]\n',
            Path("/proj").resolve() / "pyproject.toml": (
                f'[tool.{_TABLE}]\nallow = ["far"]\n'
            ),
        }
        lists = pc.read_path_lists(
            deep,
            table=_TABLE,
            keys=("allow",),
            is_file=lambda p: p in bodies,
            read_text=lambda p: bodies[p],
        )
        self.assertEqual(lists["allow"], [(deep / "near").resolve()])

    def test_ancestor_pyproject_is_found_when_the_start_dir_has_none(self) -> None:
        """The `continue` arm of the walk: skip a dir with no pyproject, keep climbing."""
        base = Path("/proj").resolve()
        lists = pc.read_path_lists(
            Path("/proj/a/b/c"),
            table=_TABLE,
            keys=("allow",),
            is_file=lambda p: p == base / "pyproject.toml",
            read_text=lambda _p: f'[tool.{_TABLE}]\nallow = ["x"]\n',
        )
        self.assertEqual(lists["allow"], [(base / "x").resolve()])

    def test_malformed_toml_raises(self) -> None:
        base = Path("/proj").resolve()
        with self.assertRaises(pc.ConfigError) as caught:
            pc.read_path_lists(
                Path("/proj"),
                table=_TABLE,
                keys=("allow",),
                is_file=lambda p: p == base / "pyproject.toml",
                read_text=lambda _p: "not = = valid",
            )
        # Consumers surface this verbatim on stderr as their exit-2 diagnostic.
        self.assertIn("cannot read or parse", str(caught.exception))

    def test_os_error_while_reading_raises(self) -> None:
        base = Path("/proj").resolve()

        def boom(_p: Path) -> str:
            raise OSError("nope")

        with self.assertRaises(pc.ConfigError):
            pc.read_path_lists(
                Path("/proj"),
                table=_TABLE,
                keys=("allow",),
                is_file=lambda p: p == base / "pyproject.toml",
                read_text=boom,
            )

    def test_undecodable_bytes_raise(self) -> None:
        """A non-UTF-8 pyproject is unreadable config, not absent config."""
        base = Path("/proj").resolve()

        def boom(_p: Path) -> str:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

        with self.assertRaises(pc.ConfigError):
            pc.read_path_lists(
                Path("/proj"),
                table=_TABLE,
                keys=("allow",),
                is_file=lambda p: p == base / "pyproject.toml",
                read_text=boom,
            )

    def test_malformed_value_raises_through_the_public_entry(self) -> None:
        base = Path("/proj").resolve()
        with self.assertRaises(pc.ConfigError):
            pc.read_path_lists(
                Path("/proj"),
                table=_TABLE,
                keys=("allow",),
                is_file=lambda p: p == base / "pyproject.toml",
                read_text=lambda _p: f"[tool.{_TABLE}]\nallow = 3\n",
            )

    def test_no_keys_requested_is_an_empty_mapping(self) -> None:
        """A consumer asking for nothing gets nothing, and still parses no file twice."""
        base = Path("/proj").resolve()
        lists = pc.read_path_lists(
            Path("/proj"),
            table=_TABLE,
            keys=(),
            is_file=lambda p: p == base / "pyproject.toml",
            read_text=lambda _p: f'[tool.{_TABLE}]\nallow = ["x"]\n',
        )
        self.assertEqual(lists, {})


class MessagesNameTheFileRelativelyTest(unittest.TestCase):
    """A ConfigError names its pyproject relative to the target, never absolutely.

    Every consumer prints this message on stderr as its exit-2 diagnostic, so an
    absolute path would carry the home directory into every gate's output. The
    relative form still names the one file to edit: it is resolved
    against the very target the caller passed. The start sits two levels below the
    pyproject, so the walk-up case is the one pinned, not the trivial same-directory one.
    """

    _BASE = Path("/proj").resolve()
    _PYPROJECT = _BASE / "pyproject.toml"
    _START = _BASE / "a" / "b"
    _SHOWN = os.path.join(os.pardir, os.pardir, "pyproject.toml")

    def _assert_relative(self, message: str) -> None:
        self.assertNotIn(str(self._PYPROJECT), message)
        self.assertIn(self._SHOWN, message)

    def _raised_by_path_lists(self, body: str) -> str:
        with self.assertRaises(pc.ConfigError) as caught:
            pc.read_path_lists(
                self._START,
                table=_TABLE,
                keys=("allow",),
                is_file=lambda p: p == self._PYPROJECT,
                read_text=lambda _p: body,
            )
        return str(caught.exception)

    def test_parse_error_from_the_raw_reader(self) -> None:
        with self.assertRaises(pc.ConfigError) as caught:
            pc.read_tool_table(
                self._START,
                table=_TABLE,
                is_file=lambda p: p == self._PYPROJECT,
                read_text=lambda _p: "not = = valid",
            )
        self._assert_relative(str(caught.exception))

    def test_parse_error_from_the_path_list_reader(self) -> None:
        self._assert_relative(self._raised_by_path_lists("not = = valid"))

    def test_malformed_value(self) -> None:
        message = self._raised_by_path_lists(f"[tool.{_TABLE}]\nallow = 3\n")
        self._assert_relative(message)

    def test_start_beside_the_file_names_it_alone(self) -> None:
        with self.assertRaises(pc.ConfigError) as caught:
            pc.read_tool_table(
                self._BASE,
                table=_TABLE,
                is_file=lambda p: p == self._PYPROJECT,
                read_text=lambda _p: "not = = valid",
            )
        self.assertTrue(
            str(caught.exception).startswith("cannot read or parse pyproject.toml:")
        )

    def test_os_error_names_the_file_relatively(self) -> None:
        # str(OSError) carries its filename (here the absolute path), so the message
        # must quote only the error's own text beside the relative name.
        def missing(path: Path) -> str:
            raise FileNotFoundError(
                errno.ENOENT, "No such file or directory", str(path)
            )

        with self.assertRaises(pc.ConfigError) as caught:
            pc.read_tool_table(
                self._START,
                table=_TABLE,
                is_file=lambda p: p == self._PYPROJECT,
                read_text=missing,
            )
        message = str(caught.exception)
        self._assert_relative(message)
        self.assertIn("No such file or directory", message)

    def test_file_start_names_a_sibling_pyproject_alone(self) -> None:
        # A file target's walk starts at its directory, so a pyproject beside it is
        # named bare, not as "../pyproject.toml".
        start = self._BASE / "tool.py"
        with self.assertRaises(pc.ConfigError) as caught:
            pc.read_tool_table(
                start,
                table=_TABLE,
                is_file=lambda p: p in {self._PYPROJECT, start},
                read_text=lambda _p: "not = = valid",
            )
        self.assertTrue(
            str(caught.exception).startswith("cannot read or parse pyproject.toml:")
        )


class NearestWalkTest(unittest.TestCase):
    """``nearest``: which file answers, decided without reading it.

    Split out of the parse so a consumer can tell one source from another by identity.
    That matters for a table whose entries accumulate down a tree: read per subject
    instead of per file, one shared declaration would be parsed, and judged stale,
    once for every subject that happens to sit beneath it.
    """

    def test_the_nearest_of_several_wins(self) -> None:
        root = Path("/proj").resolve()
        present = {root / "pyproject.toml", root / "a" / "pyproject.toml"}
        found = pc.nearest(root / "a" / "b", is_file=present.__contains__)
        self.assertEqual(found, root / "a" / "pyproject.toml")

    def test_no_pyproject_anywhere_is_none(self) -> None:
        self.assertIsNone(pc.nearest(Path("/proj"), is_file=lambda _p: False))


class ChainWalkTest(unittest.TestCase):
    """``chain``: every declaring file above a subject, bounded by the checkout.

    The bound is the point. An unbounded walk would let whatever pyproject happens to
    sit above the checkout change a verdict (the same host-independence rule the rest
    of this library follows), and that difference is invisible in any tree that has no
    such file, which is most of them until one appears.
    """

    def test_every_file_from_start_to_stop_nearest_first(self) -> None:
        root = Path("/proj").resolve()
        present = {root / "pyproject.toml", root / "a" / "pyproject.toml"}
        found = pc.chain(root / "a" / "b", stop=root, is_file=present.__contains__)
        self.assertEqual(
            found, (root / "a" / "pyproject.toml", root / "pyproject.toml")
        )

    def test_the_walk_stops_at_the_ceiling(self) -> None:
        # A pyproject above the bound must not contribute, however real it is.
        root = Path("/proj").resolve()
        present = {root / "pyproject.toml", root.parent / "pyproject.toml"}
        found = pc.chain(root / "a", stop=root, is_file=present.__contains__)
        self.assertEqual(found, (root / "pyproject.toml",))

    def test_start_equal_to_stop_returns_that_files_own_entry(self) -> None:
        root = Path("/proj").resolve()
        present = {root / "pyproject.toml"}
        found = pc.chain(root, stop=root, is_file=present.__contains__)
        self.assertEqual(found, (root / "pyproject.toml",))

    def test_start_outside_stop_yields_nothing(self) -> None:
        # Not an error and not a fallback: what to do about a subject reached from
        # outside the bound is the caller's policy, so this reports the fact only.
        root = Path("/proj").resolve()
        present = {Path("/elsewhere").resolve() / "pyproject.toml"}
        found = pc.chain(Path("/elsewhere"), stop=root, is_file=present.__contains__)
        self.assertEqual(found, ())

    def test_no_pyproject_inside_the_bound_yields_nothing(self) -> None:
        # Distinct from the case above only to the caller, which must not answer this
        # one by widening the search upward: that is what the bound forbids.
        root = Path("/proj").resolve()
        found = pc.chain(root / "a", stop=root, is_file=lambda _p: False)
        self.assertEqual(found, ())
