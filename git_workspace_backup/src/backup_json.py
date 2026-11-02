"""Strict JSON field readers shared by every workspace_backup parser.

Each reader raises ``ShapeError`` on the first deviation; each public parser catches it
once and returns None. ``bool`` is refused where an ``int`` is expected, because
``isinstance(True, int)`` holds in Python and a record saying ``"size": true`` is not one
this tool wrote.

Every record file shares one envelope (``tool`` and ``schema`` beside the record's own
fields, as sorted, indented JSON), so it is written and opened here, once. The name rule
lives here for the same reason: the manifest and the ledger must agree on what a name that
cannot leave its directory is; and so does the form names are compared in, as APFS compares
them, which status and the restore document's checks share.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Callable, Mapping
from typing import TypeVar

from backup_model import SCHEMA_VERSION, TOOL_ID

T = TypeVar("T")

#: The fields every record file carries beside its own.
_ENVELOPE_KEYS = frozenset({"tool", "schema"})
#: Path components that leave, or are ambiguous about, the directory a name is joined to.
_UNSAFE_COMPONENTS = frozenset({"", ".", ".."})


class ShapeError(Exception):
    """A decoded JSON value does not have the shape this tool writes."""


def envelope_to_json(body: Mapping[str, object]) -> str:
    """*body* in this tool's envelope as stable JSON: sorted, two-space indent, final newline."""
    payload = {"tool": TOOL_ID, "schema": SCHEMA_VERSION, **body}
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def open_envelope(
    data: "bytes | str",
    body_keys: frozenset[str],
    *,
    optional: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """The object in *data*; ShapeError unless it is this tool's envelope around *body_keys*.

    Each key in *optional* may be present or absent: a field added after records without it
    were written. A record from another tool or a different schema version is refused like
    any other wrong shape: none of this tool's decisions may rest on a record it did not write.
    """
    obj = loads_object(data)
    require_keys(obj, body_keys | _ENVELOPE_KEYS | (optional & set(obj)))
    if get_str(obj, "tool") != TOOL_ID or get_int(obj, "schema") != SCHEMA_VERSION:
        raise ShapeError("a record from another tool or schema version")
    return obj


def loads_object(data: "bytes | str") -> dict[str, object]:
    """Decode *data* as a JSON object; ShapeError for invalid JSON or any other top level."""
    try:
        value = json.loads(data)
    except (ValueError, RecursionError) as exc:  # UnicodeDecodeError is a ValueError
        raise ShapeError(f"not JSON: {exc}") from exc
    return as_object(value)


def as_object(value: object) -> dict[str, object]:
    """*value* as a string-keyed object, or ShapeError."""
    if not isinstance(value, dict):
        raise ShapeError("expected a JSON object")
    return {str(key): item for key, item in value.items()}


def require_keys(obj: dict[str, object], keys: frozenset[str]) -> None:
    """ShapeError unless *obj* has exactly *keys*: an extra key is a schema this tool never wrote."""
    if set(obj) != keys:
        raise ShapeError(f"expected keys {sorted(keys)}, found {sorted(obj)}")


def is_safe_relative_name(name: str) -> bool:
    """True when *name* is a relative POSIX path that cannot leave the directory it joins.

    Splitting on ``/`` turns the empty name and a leading ``/`` into an empty component, so
    the one component check refuses empty, absolute, ``a//b``, ``a/``, ``./a`` and
    ``a/../b`` alike. A NUL is refused because no filesystem call accepts one.
    """
    return "\x00" not in name and _UNSAFE_COMPONENTS.isdisjoint(name.split("/"))


def apfs_name_key(name: str) -> str:
    """*name* in one form for comparing as APFS does: NFC, casefolded, then NFC again.

    APFS ignores case and Unicode normalization form by default, so two names equal here name
    one file. Casefolding can leave a decomposed sequence, so the form is settled once more
    after it.
    """
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", name).casefold())


def relative_posix_name(obj: dict[str, object], key: str) -> str:
    """The string at *key*, which must satisfy :func:`is_safe_relative_name`."""
    name = get_str(obj, key)
    if not is_safe_relative_name(name):
        raise ShapeError(f"{key} is not a safe relative name: {name!r}")
    return name


def get_str(obj: dict[str, object], key: str) -> str:
    """The string at *key*."""
    value = obj.get(key)
    if not isinstance(value, str):
        raise ShapeError(f"{key} must be a string")
    return value


def get_optional_str(obj: dict[str, object], key: str) -> "str | None":
    """The string or null at *key*."""
    value = obj.get(key)
    if value is None or isinstance(value, str):
        return value
    raise ShapeError(f"{key} must be a string or null")


def get_bool(obj: dict[str, object], key: str) -> bool:
    """The boolean at *key*."""
    value = obj.get(key)
    if not isinstance(value, bool):
        raise ShapeError(f"{key} must be a boolean")
    return value


def get_int(obj: dict[str, object], key: str, *, minimum: int = 0) -> int:
    """The integer at *key*, at least *minimum*; a boolean is not an integer here."""
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ShapeError(f"{key} must be an integer >= {minimum}")
    return value


def get_list(
    obj: dict[str, object], key: str, item: Callable[[object], T]
) -> tuple[T, ...]:
    """The list at *key*, each element read by *item*."""
    value = obj.get(key)
    if not isinstance(value, list):
        raise ShapeError(f"{key} must be a list")
    return tuple(item(entry) for entry in value)


def str_item(value: object) -> str:
    """A list element that must be a string."""
    if not isinstance(value, str):
        raise ShapeError("list element must be a string")
    return value
