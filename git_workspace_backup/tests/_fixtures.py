#!/usr/bin/env python3
"""Fixtures shared by workspace_backup's grouped suites: throwaway repos, fake runner, tree.

The tree is the fixture workspace and homes a build reads, from :func:`make_tree`.

Deliberately not named ``test_*``, so the aggregator's discover skips it. Importing it puts
the tool folder on ``sys.path`` and imports ``git_workspace_backup``, whose bootstrap makes the
private ``src/`` modules importable: the one bootstrap every suite relies on.

Every repository is created under a caller-owned temporary directory. Every fixture git
command, through ``git()`` here or through the tool's own runner inside
``isolated_git_env()``, runs with, and only with, this isolation:

- a fixed fake author and committer identity;
- no global or system config (``GIT_CONFIG_GLOBAL`` at the null device, and
  ``GIT_CONFIG_NOSYSTEM``);
- no inherited variable that redirects the repository, work tree, index or object store, and
  no inherited command-line config (see :func:`is_stripped_git_var`);
- ``core.hooksPath`` at the null device, so no hook runs, wherever one was put;
- a discovery ceiling at the system temp dir, so a path outside every fixture repo never
  resolves to a work tree above it.

The rest of the inherited environment (``PATH``, and so which ``git`` binary runs) is
not isolated. Every name here is an obvious fake.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import git_workspace_backup  # noqa: E402,F401 - its bootstrap puts src/ on sys.path

import backup_proc  # noqa: E402 - importable only after the bootstrap above
import bounded_run  # noqa: E402 - on sys.path once backup_proc's own bootstrap has run
from backup_build import BuildRequest  # noqa: E402 - importable only after the bootstrap
from backup_config import BackupConfig  # noqa: E402 - importable only after the bootstrap
from backup_roots import Roots  # noqa: E402 - importable only after the bootstrap

#: Inherited variables no fixture git command may see, by exact name. Two classes:
#:
#: - Redirects: a repository, work tree, index or object store other than the one git runs
#:   in. git exports GIT_DIR and GIT_INDEX_FILE to a hook's children, so a gate run from a
#:   hook would otherwise send a fixture's init, add and commit into the real repository.
#: - Command-line config: what a parent ``git -c`` exports (GIT_CONFIG_PARAMETERS), or what
#:   GIT_CONFIG_COUNT indexes. It outranks every config file, so aimed at core.hooksPath it
#:   makes a fixture commit run the host's hooks.
STRIPPED_GIT_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
)
#: ...and by prefix: the numbered key/value pairs GIT_CONFIG_COUNT indexes.
STRIPPED_GIT_VAR_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
#: Where fixture git looks for hooks. The null device is not a directory, so nothing can
#: ever be installed there, and git treats a hooks path holding no hook as "no hooks".
FIXTURE_HOOKS_PATH = os.devnull

#: Applied to os.environ for any test that runs real git, so the tool's own runner
#: (which inherits the environment) sees the same isolation as the fixture helpers.
#: Always applied after the stripped variables are removed: its own GIT_CONFIG_* entries
#: match the strip rule.
ISOLATED_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Fixture Author",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "Fixture Author",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    # Fixture repos live in TemporaryDirectory()s under the system temp dir. Discovery may
    # climb to one of those but never into the temp dir itself, so a directory outside every
    # fixture repo reads as "not a repository" even on a host whose temp dir is in a work tree.
    "GIT_CEILING_DIRECTORIES": os.path.realpath(tempfile.gettempdir()),
    # The fixtures' own command-line config: no hook runs, whether host config or a template
    # directory put one in reach.
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "core.hooksPath",
    "GIT_CONFIG_VALUE_0": FIXTURE_HOOKS_PATH,
}
#: Bound on any single fixture git command.
FIXTURE_TIMEOUT_S = 60.0
#: One path component longer than any macOS filesystem accepts (255 bytes): a name no
#: folder can have, and so never one this tool wrote.
TOO_LONG_NAME = "2" * 300
#: What a planted file holds, and the mtime it is backdated to, so that any write a test
#: forbids changes one or the other however coarse the filesystem's clock. The mtime is
#: 2001-09-09, long before any test runs.
PLANTED_BYTES = b"fixture: never opened through a symlink\n"
PLANTED_MTIME_NS = 1_000_000_000 * 1_000_000_000
#: Programs a FakeRunner never hands to its real runner, whatever that runner is. A test
#: that forgot a route must fail loudly rather than touch the host:
#:
#: - launchctl: bootout, bootstrap or print act on the real launchd domain, where they can
#:   install or remove a live agent;
#: - osascript: reads the real Foundation and iCloud state, and can post a notification;
#: - memory_snapshot.py: ``--mode snapshot`` commits the host's auto-memory corpora.
#:
#: A test that genuinely needs one of them calls ``backup_proc.run_cmd`` directly.
NEVER_REAL = frozenset({"launchctl", "osascript", "memory_snapshot.py"})

Route = Callable[[Sequence[str]], "tuple[int, str, str]"]


def is_stripped_git_var(name: str) -> bool:
    """True when *name* is an inherited variable no fixture git command may see.

    The one rule both ``git`` and ``isolated_git_env`` apply.
    """
    return name in STRIPPED_GIT_VARS or name.startswith(STRIPPED_GIT_VAR_PREFIXES)


def isolated_git_env() -> AbstractContextManager[dict[str, str]]:
    """A context manager that applies ISOLATED_GIT_ENV to os.environ for its duration.

    Every variable :func:`is_stripped_git_var` names is removed first, for that duration too.
    """
    return _isolated_os_environ()


@contextmanager
def _isolated_os_environ() -> Iterator[dict[str, str]]:
    # patch.dict with no values snapshots the mapping and restores all of it on exit, so the
    # names deleted here come back too. Strip, then apply: the reverse order would delete the
    # fixtures' own GIT_CONFIG_* entries.
    with mock.patch.dict(os.environ) as patched:
        for name in [key for key in os.environ if is_stripped_git_var(key)]:
            del os.environ[name]
        os.environ.update(ISOLATED_GIT_ENV)
        yield patched


def _without_stripped(environ: Mapping[str, str]) -> dict[str, str]:
    """*environ* minus every variable :func:`is_stripped_git_var` names."""
    return {
        key: value for key, value in environ.items() if not is_stripped_git_var(key)
    }


def git(cwd: Path, *args: str) -> str:
    """Run one fixture git command in *cwd* with the isolated environment; return stdout.

    Bounded through ``bounded_run`` rather than ``subprocess.run(timeout=)``: a capturing
    run's timeout kills only the direct child and then re-reads pipes a surviving git
    helper can hold open, so the bound would not hold. Raises ``CalledProcessError``
    (carrying stderr) on a non-zero exit, so a broken fixture fails loudly at its source.
    """
    done = bounded_run.run_bounded(
        ["git", *args],
        timeout=FIXTURE_TIMEOUT_S,
        cwd=cwd,
        env={**_without_stripped(os.environ), **ISOLATED_GIT_ENV},
    )
    done.check_returncode()
    return done.stdout


def make_repo(parent: Path, name: str = "workspace") -> Path:
    """A fresh repository ``parent/name`` on branch ``main`` with one commit."""
    repo = parent / name
    repo.mkdir(parents=True)
    git(repo, "init", "--quiet", "--initial-branch=main")
    (repo / "README.md").write_text("fixture\n", encoding="utf-8", newline="\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "--quiet", "-m", "initial")
    return repo


def add_commit(repo: Path, message: str) -> str:
    """Add an empty commit to *repo*'s current HEAD and return its sha."""
    git(repo, "commit", "--quiet", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


def plant_untouched_file(path: Path) -> None:
    """Write :data:`PLANTED_BYTES` to *path* and backdate it to :data:`PLANTED_MTIME_NS`."""
    path.write_bytes(PLANTED_BYTES)
    os.utime(path, ns=(PLANTED_MTIME_NS, PLANTED_MTIME_NS))


def file_fingerprint(path: Path) -> "tuple[bytes, int] | None":
    """The regular file *path*'s bytes and mtime, or None when nothing is there."""
    if not path.exists():
        return None
    return path.read_bytes(), path.stat().st_mtime_ns


def failing_when(fragment: str, code: int = 1) -> backup_proc.RunCmd:
    """A runner answering any command whose joined argv contains *fragment* with exit *code*.

    Every other command runs for real through ``backup_proc.run_cmd``. A bundle or scratch
    path that itself contains *fragment* makes an earlier command match, so name them apart.
    """

    def run(
        argv: Sequence[str], cwd: "Path | None", timeout: float
    ) -> "tuple[int, str, str]":
        if fragment in " ".join(argv):
            return code, "", f"fixture failure: {fragment}"
        return backup_proc.run_cmd(argv, cwd, timeout)

    return run


def failing_on_argv(argv_tail: Sequence[str], code: int = 1) -> backup_proc.RunCmd:
    """A runner answering only a git command whose argv after ``git`` is *argv_tail* with *code*.

    For a query whose words another command also carries: ``rev-parse --show-toplevel`` alone,
    say, which the layout check asks together with ``--git-common-dir``.
    """
    wanted = list(argv_tail)

    def run(
        argv: Sequence[str], cwd: "Path | None", timeout: float
    ) -> "tuple[int, str, str]":
        if list(argv[1:]) == wanted:
            return code, "", f"fixture failure: {' '.join(wanted)}"
        return backup_proc.run_cmd(argv, cwd, timeout)

    return run


class FakeRunner:
    """A RunCmd that answers some programs from canned routes and runs the rest for real.

    A route is keyed by the basename of ``argv[0]`` (``launchctl``, ``osascript``) or, for an
    interpreter invocation, of ``argv[1]`` (``memory_snapshot.py``). Every call is recorded.
    An unrouted call naming a program in NEVER_REAL is refused with AssertionError before
    ``real`` is reached; every other unrouted call runs through ``real``.
    """

    def __init__(
        self,
        routes: "dict[str, Route]",
        real: backup_proc.RunCmd = backup_proc.run_cmd,
    ) -> None:
        self.routes = routes
        self.real = real
        self.calls: list[list[str]] = []

    def __call__(
        self, argv: Sequence[str], cwd: "Path | None", timeout: float
    ) -> "tuple[int, str, str]":
        self.calls.append(list(argv))
        keys = tuple(self._keys(argv))
        for key in keys:
            if key in self.routes:
                return self.routes[key](argv)
        refused = [key for key in keys if key in NEVER_REAL]
        if refused:
            raise AssertionError(
                f"{refused[0]} must never run for real in a test: add a route for it"
            )
        return self.real(argv, cwd, timeout)

    @staticmethod
    def _keys(argv: Sequence[str]) -> Iterator[str]:
        yield Path(argv[0]).name
        if len(argv) > 1:
            yield Path(argv[1]).name


#: A memory_snapshot route that always succeeds.
MEMORY_OK: "dict[str, Route]" = {"memory_snapshot.py": lambda argv: (0, "", "")}


def upload_states_json(*pairs: "tuple[bool, bool]") -> str:
    """An upload-state answer holding one entry per ``(ubiquitous, uploaded)`` pair."""
    return json.dumps([{"ubiquitous": u, "uploaded": up} for u, up in pairs])


def osascript_paths(argv: Sequence[str]) -> list[str]:
    """The arguments an osascript call hands its script: everything after ``--``.

    A route sizes its answer from this rather than from a fixed argv offset, so the answer
    still matches when the script's own options change.
    """
    items = list(argv)
    return items[items.index(backup_proc.END_OF_OPTIONS) + 1 :]


def osascript_answering(code: int, text: str) -> Route:
    """An osascript route that exits *code* having printed *text*."""
    return lambda argv: (code, text, "")


@dataclass(frozen=True)
class TreeFixture:
    """A workspace with every ref shape, a Claude home, a home, and a build request."""

    tmp: Path
    workspace: Path
    home: Path
    staging: Path
    request: BuildRequest
    detached_only: str


def make_tree(tmp: Path) -> TreeFixture:
    """Build the fixture tree under *tmp*; the caller must hold isolated_git_env().

    The workspace holds a branch, a tag, a stash and a detached linked worktree ``wt`` whose
    one commit no ref reaches (``detached_only``), beside ignored folders the build extras,
    regenerates and leaves unclassified. The Claude home holds one memory repository and one
    project whose memory repository has no commits.
    """
    workspace = make_repo(tmp)
    git(workspace, "branch", "side")
    git(workspace, "tag", "v1")
    (workspace / "README.md").write_text(
        "stashed change\n", encoding="utf-8", newline="\n"
    )
    git(workspace, "stash", "--quiet")
    (workspace / ".gitignore").write_text(
        ".notes/\nnew_folder/\ncache/\n", encoding="utf-8", newline="\n"
    )
    (workspace / "blob.bin").write_bytes(os.urandom(20000))
    git(workspace, "add", ".gitignore", "blob.bin")
    git(workspace, "commit", "--quiet", "-m", "content")
    for folder in (".notes", "new_folder", "cache"):
        (workspace / folder).mkdir()
        (workspace / folder / "f.md").write_text(folder, encoding="utf-8", newline="\n")
    git(workspace, "worktree", "add", "--quiet", "--detach", str(tmp / "wt"))
    detached_only = add_commit(tmp / "wt", "detached only")
    home = tmp / "home"
    claude = home / ".claude"
    (claude / "projects" / "-empty" / "memory").mkdir(parents=True)
    git(claude / "projects" / "-empty" / "memory", "init", "--quiet")
    make_repo(claude / "projects" / "-fixture-proj", name="memory")
    (claude / "settings.json").write_text("{}", encoding="utf-8", newline="\n")
    (home / ".zshrc").write_text("z", encoding="utf-8", newline="\n")
    staging = tmp / "staging"
    request = BuildRequest(
        roots=Roots(workspace, claude, home),
        config=BackupConfig(
            pyproject=workspace / "pyproject.toml",
            destination=tmp / "dest",
            keep=7,
            extras=(".notes",),
            regenerable=("cache",),
            claude_files=("settings.json",),
            home_files=(".zshrc",),
            # On, as in the workspace this tool came from: the build suites pin the step.
            memory_snapshot=True,
        ),
        staging=staging,
        tools_dir=tmp / "tools",
        python="/opt/fixture-python",
    )
    return TreeFixture(tmp, workspace, home, staging, request, detached_only)
