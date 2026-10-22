"""Post a macOS notification when a run needs attention.

AppleScript receives the title and message as ``argv`` items, never interpolated into the
script text, so a quote or backslash in a failure reason cannot change what runs. A ``--``
ends osascript's own options before them: measured, ``osascript -e <script> -x`` refuses
``-x`` as an illegal option and posts nothing, while ``-- -x`` hands ``-x`` to the script.

The text is bounded before it becomes an argument. A message joins every failed item's
reason, so it can run long; Notification Center cuts a banner short anyway, and the full
detail is in ``last.json``. Whether a notification is displayed at all depends on per-app
notification permission this tool cannot read, which is why ``--mode status``, not this, is
the guarantee.
"""

from __future__ import annotations

from backup_proc import END_OF_OPTIONS, OSASCRIPT, OSASCRIPT_TIMEOUT_S, RunCmd

#: The most characters a posted title or message holds, the ellipsis included.
MESSAGE_LIMIT = 240
#: Marks text cut to MESSAGE_LIMIT.
ELLIPSIS = "\N{HORIZONTAL ELLIPSIS}"
_NUL = "\x00"
_SCRIPT = (
    "on run argv",
    "display notification (item 2 of argv) with title (item 1 of argv)",
    "end run",
)


# fuzz-target
def bounded_text(text: str) -> str:
    """*text* without NUL, cut to at most MESSAGE_LIMIT characters ending in an ellipsis.

    A NUL is dropped because no argv item can carry one: starting the process would raise
    ValueError, which the runner does not turn into an exit code. Never raises.
    """
    clean = text.replace(_NUL, "")
    if len(clean) <= MESSAGE_LIMIT:
        return clean
    return clean[: MESSAGE_LIMIT - len(ELLIPSIS)] + ELLIPSIS


def notify(title: str, message: str, *, run: RunCmd) -> bool:
    """Post *message* under *title*; True when osascript accepted it."""
    argv = [OSASCRIPT]
    for line in _SCRIPT:
        argv += ["-e", line]
    argv += [END_OF_OPTIONS, bounded_text(title), bounded_text(message)]
    code, _, _ = run(argv, None, OSASCRIPT_TIMEOUT_S)
    return code == 0
