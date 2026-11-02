"""Read iCloud Drive's per-file upload state through Foundation.

``NSURLUbiquitousItemIsUploadedKey`` is the owning component's own answer to "has this left
the machine"; a local folder existing proves nothing, because a signed-out account or a paused
sync leaves it present and writable. It is read through osascript's JavaScript-for-Automation
bridge, so no compiled helper ships. The script text is one constant: paths travel only as
argv items after a ``--`` that ends osascript's own options, never interpolated into the text.

The script answers ``null`` for a path it cannot reach, or whose keys it cannot read. That is
measured rather than habit: Foundation answers both ubiquity keys for a missing path exactly as
it does for a local file (success, with no value), so the keys alone would report a vanished
snapshot as "not in iCloud", which status treats as healthy. A key read with no value is false.
Only an answer of exactly one two-boolean entry per path is believed; a null entry, a non-zero
exit (a timeout and a missing osascript included) or any other output is unreadable, never
uploaded.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from backup_proc import END_OF_OPTIONS, OSASCRIPT, OSASCRIPT_TIMEOUT_S, RunCmd

UPLOAD_NONE = "none"
UPLOAD_NOT_ICLOUD = "not-icloud"
UPLOAD_UPLOADED = "uploaded"
UPLOAD_PENDING = "pending"
UPLOAD_UNREADABLE = "unreadable"

_LANGUAGE_OPTIONS = ("-l", "JavaScript")
_JXA = """ObjC.import("Foundation");
function flag(url, key) {
  const value = Ref();
  if (!url.getResourceValueForKeyError(value, $[key], Ref())) {
    return null;
  }
  return ObjC.unwrap(value[0]) === true;
}
function state(path) {
  const url = $.NSURL.fileURLWithPath(path);
  if (!url.checkResourceIsReachableAndReturnError(Ref())) {
    return null;
  }
  const ubiquitous = flag(url, "NSURLIsUbiquitousItemKey");
  const uploaded = ubiquitous === true
    ? flag(url, "NSURLUbiquitousItemIsUploadedKey")
    : false;
  if (ubiquitous === null || uploaded === null) {
    return null;
  }
  return { ubiquitous: ubiquitous, uploaded: uploaded };
}
function run(argv) {
  return JSON.stringify(argv.map(function (path) { return state(path); }));
}"""
_STATE_KEYS = frozenset({"ubiquitous", "uploaded"})


@dataclass(frozen=True)
class UploadState:
    """Whether one path is in iCloud at all, and whether iCloud has its current content."""

    ubiquitous: bool
    uploaded: bool


def read_upload_states(
    paths: Sequence[Path], *, run: RunCmd
) -> "tuple[UploadState, ...] | None":
    """One state per path, in order; None when osascript fails or answers unexpectedly.

    No paths asks nothing and answers ``()``.
    """
    if not paths:
        return ()
    argv = [
        OSASCRIPT,
        *_LANGUAGE_OPTIONS,
        "-e",
        _JXA,
        END_OF_OPTIONS,
        *(str(path) for path in paths),
    ]
    code, out, _ = run(argv, None, OSASCRIPT_TIMEOUT_S)
    if code != 0:
        return None
    return parse_upload_states(out, len(paths))


# fuzz-target
def parse_upload_states(text: str, expected: int) -> "tuple[UploadState, ...] | None":
    """The JSON answer as states, or None unless it is exactly *expected* well-formed entries.

    Never raises: text that is not JSON, or nests deeper than the decoder can follow, is None.
    """
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(value, list) or len(value) != expected:
        return None
    states: list[UploadState] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != _STATE_KEYS:
            return None
        ubiquitous, uploaded = item["ubiquitous"], item["uploaded"]
        # isinstance, not a truthiness or int check: JSON 0 and 1 are not answers.
        if not isinstance(ubiquitous, bool) or not isinstance(uploaded, bool):
            return None
        states.append(UploadState(ubiquitous, uploaded))
    return tuple(states)


def summarize_upload(states: "tuple[UploadState, ...] | None") -> str:
    """One word for a snapshot's files: not in iCloud, uploaded, pending, or unreadable.

    A snapshot partly in iCloud and partly not is unreadable: its files share one folder, so
    the mix means the answer cannot be trusted, not that half the snapshot is safe.
    """
    if states is None:
        return UPLOAD_UNREADABLE
    if not any(state.ubiquitous for state in states):
        return UPLOAD_NOT_ICLOUD
    if not all(state.ubiquitous for state in states):
        return UPLOAD_UNREADABLE
    if all(state.uploaded for state in states):
        return UPLOAD_UPLOADED
    return UPLOAD_PENDING
