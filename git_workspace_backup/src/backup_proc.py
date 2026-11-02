"""Subprocess seam for workspace_backup: every external command runs bounded.

The real runner delegates to the shared ``bounded_run`` library, which kills the whole
process tree on expiry. A bare ``subprocess.run(timeout=)`` kills only the direct child,
and launchd skips the next calendar firing while any part of a hung run is still alive.
Neither a timeout nor a missing binary raises: both come back as an exit code a caller can
record as one failed item.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

# bounded_run, the shared process-tree-killing runner, at one fixed place: its folder
# directly under the repository root, two levels above this file's own folder. Appended,
# as in git_workspace_backup.py, and for the same reason.
sys.path.append(str(Path(__file__).resolve().parents[2] / "bounded_run"))

import bounded_run  # noqa: E402 - import follows the sys.path bootstrap above

#: (argv, cwd, timeout seconds) -> (returncode, stdout, stderr)
RunCmd = Callable[[Sequence[str], "Path | None", float], "tuple[int, str, str]"]
#: The git seam: (git argv tail, cwd) -> (returncode, stdout, stderr)
RunGit = Callable[[Sequence[str], Path], "tuple[int, str, str]"]

#: Reported when a command outlived its bound (the GNU ``timeout`` convention).
TIMEOUT_RC = 124
#: Reported when the executable could not be started at all.
NOT_STARTED_RC = 127

#: Git's bound is generous: a full-history bundle, a mirror clone and an fsck all scale
#: with repository size.
GIT_TIMEOUT_S = 900.0
MEMORY_SNAPSHOT_TIMEOUT_S = 300.0
OSASCRIPT_TIMEOUT_S = 60.0
LAUNCHCTL_TIMEOUT_S = 30.0

#: osascript's executable, for every call site: the notification and the upload-state query.
#: Absolute, as launchctl's is: the agent's PATH puts git's directory first, and a Homebrew
#: git's directory is one its user can write, so a bare name could run a planted stand-in.
OSASCRIPT = "/usr/bin/osascript"
#: launchctl's executable, for every call site: print, bootout and bootstrap.
LAUNCHCTL = "/bin/launchctl"
#: Ends osascript's own options, so each argument after it reaches the script as data.
#: Measured: ``osascript -e <script> -x`` refuses ``-x`` as an illegal option and runs
#: nothing, while ``-- -x`` hands ``-x`` to the script.
END_OF_OPTIONS = "--"


def run_cmd(
    argv: Sequence[str], cwd: "Path | None", timeout: float
) -> "tuple[int, str, str]":
    """Run *argv* in *cwd* bounded by *timeout*; never raises for a timeout or missing binary."""
    try:
        done = bounded_run.run_bounded(argv, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        return TIMEOUT_RC, "", f"timed out after {timeout:g}s: {argv[0]}"
    except OSError as exc:
        return NOT_STARTED_RC, "", f"could not start {argv[0]}: {exc}"
    return done.returncode, done.stdout, done.stderr


def git_runner(run: RunCmd, timeout: float = GIT_TIMEOUT_S) -> RunGit:
    """Adapt *run* to the RunGit seam, prefixing ``git`` and fixing the bound."""

    def _run_git(args: Sequence[str], cwd: Path) -> "tuple[int, str, str]":
        return run(["git", *args], cwd, timeout)

    return _run_git
