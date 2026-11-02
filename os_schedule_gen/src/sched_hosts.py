"""Which scheduler a platform installs into, and where that scheduler's files live.

Private to os_schedule_gen: consumers reach it through the re-export in
``os_schedule_gen.py``. These are the facts an installer needs about a host; the
builders that render each artifact stay in the parent.
"""

from __future__ import annotations

import os
from pathlib import Path

# Which scheduler a platform installs into. Values are stable strings rather than an
# enum because they are printed in operator-facing messages and asserted in tests.
BACKEND_TASK_SCHEDULER = "task-scheduler"
BACKEND_SYSTEMD = "systemd-user-timer"
BACKEND_LAUNCHD = "launchd-agent"
# Not an install backend: cron is emit-only. A .timer or .plist is a per-job file this
# tool wholly authors, so install and remove touch only what we wrote. `crontab -` is a
# read-modify-write of one shared file that also holds the operator's unrelated jobs, so
# a bug there destroys entries we never wrote. That asymmetry, not any difference in
# expressiveness, is why cron gets a line to paste rather than a fourth installer.
BACKEND_CRON_EMIT = "cron-emit"


def systemd_unit_dir(home: Path, environ: "dict[str, str] | None" = None) -> Path:
    """Where a systemd user unit lives, honoring ``XDG_CONFIG_HOME``.

    Resolved from the injectable *home* rather than the real ``~`` so tests never write
    into the operator's own unit directory, and from ``XDG_CONFIG_HOME`` when set because
    that is where systemd itself looks. Writing to ``~/.config`` on a box that
    relocated it would install a unit systemd never reads, the quietest possible failure.
    """
    env = os.environ if environ is None else environ
    base = env.get("XDG_CONFIG_HOME")
    root = Path(base) if base else home / ".config"
    return root / "systemd" / "user"


def logs_dir(state_dir: Path) -> Path:
    """Where a job's logs live under *state_dir*.

    One definition because the plist names this directory and whoever installs the agent
    must create it long before the job that writes into it ever runs. A repeated
    ``"logs"`` literal is how those two locations silently stop being the same place.
    """
    return state_dir / "logs"


def select_backend(platform: str, *, has_systemd: bool) -> str:
    """Which scheduling backend *platform* installs into.

    A pure predicate taking the platform and the systemd answer, rather than reading
    ``sys.platform`` and probing inside the installer, so every branch is reachable from
    a test on a single machine. It is also why the POSIX paths are fully covered from a
    Windows box.

    *has_systemd* is consulted only where the platform does not already decide, i.e. on
    the Linux branch; a caller avoids paying for the probe elsewhere by running it only
    there.
    """
    if platform == "win32":
        return BACKEND_TASK_SCHEDULER
    if platform == "darwin":
        return BACKEND_LAUNCHD
    return BACKEND_SYSTEMD if has_systemd else BACKEND_CRON_EMIT


def requires_elevation(backend: str) -> bool:
    """True when installing this backend's schedule needs an elevated prompt.

    Only Task Scheduler does. Both POSIX backends install into the invoking user's own
    scope (``systemctl --user``, ``~/Library/LaunchAgents``), so there is no privilege to
    acquire: the Windows elevation gate is genuinely inapplicable off Windows rather
    than being skipped by an exception.
    """
    return backend == BACKEND_TASK_SCHEDULER
