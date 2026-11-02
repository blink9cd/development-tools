"""The ``creationflags`` every ``bounded_run`` spawn passes to ``Popen``.

Private to ``bounded_run``: it sits in that tool's own ``src/`` folder, so nothing outside the
tool folder imports it. Its name carries the tool's prefix, so it cannot shadow another tool's
module on ``sys.path``.

A pure function of the platform and the caller's choice, so every arm is pinned on any host;
the values are documented Win32 ones that ``subprocess`` does not export on every platform.
"""

from __future__ import annotations

#: CreateProcess flag: the child joins its job before its first instruction runs, and so before
#: it can start a grandchild that would be born outside the job.
CREATE_SUSPENDED = 0x00000004

#: CreateProcess flag: a console-subsystem child gets no console window. Without it, a child of a
#: parent that has no console (``pythonw.exe``) is given a new, visible one.
CREATE_NO_WINDOW = 0x08000000

#: The platform value on which the Win32 flags apply.
_WIN32 = "win32"


def spawn_creationflags(*, platform: str, hide_window: bool) -> int:
    """Return the spawn flags for *platform*: zero off Windows, where Popen rejects any other.

    On win32 the child always starts suspended, until it is in its job; *hide_window* adds
    ``CREATE_NO_WINDOW``. Elsewhere *hide_window* means nothing, as there is no console to hide.
    """
    if platform != _WIN32:
        return 0
    return CREATE_SUSPENDED | (CREATE_NO_WINDOW if hide_window else 0)
