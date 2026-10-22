"""The restore-block commands that store a bundle's recorded stash entries back.

A mirror clone of a bundle writes ``refs/stash`` at the newest entry but with no reflog, and
``git stash list`` reads only the reflog, so the clone lists no stash at all; the older
entries' commits sit in the clone unnamed. So the commands delete that reflog-less ref and
store every recorded entry by its sha, oldest first, each with its recorded message, which
rebuilds the reflog newest-last exactly as it was. Then they prove the restored list holds
the recorded commits in order. They run after the checkout: ``stash store`` needs a work
tree.

Kept apart from backup_restore_doc, which is near the length cap. The caller has already
refused a sha that is not a full object id and a message that could end a Markdown fence;
every value is shell-quoted here regardless.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence

from backup_bundle import STASH_REF
from backup_model import StashEntry


def stash_restore_commands(target: str, stashes: Sequence[StashEntry]) -> list[str]:
    """The commands restoring *stashes* (oldest first) into the checkout at *target*.

    *target* is shell text already, quoted by the caller. No stashes, no commands.
    """
    if not stashes:
        return []
    commands = [f"git -C {target} update-ref -d {STASH_REF}"]
    for stash in stashes:
        message = shlex.quote(stash.message)
        commands.append(
            f"git -C {target} stash store -m {message} {shlex.quote(stash.sha)}"
        )
    # stash list prints the newest entry first: the recorded order, reversed.
    newest_first = " ".join(shlex.quote(stash.sha) for stash in reversed(stashes))
    commands.append(
        f'[ "$(git -C {target} stash list --format=%H)" = '
        f"\"$(printf '%s\\n' {newest_first})\" ] || "
        '{ echo "the restored stash list is not" "the recorded one" >&2; false; }'
    )
    return commands
