"""JSON serialization and deserialization utilities."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar, cast

from yarl import URL

from src.config import JsonType

logger = logging.getLogger("TwitchDrops")


_JSON_T = TypeVar("_JSON_T", bound=Mapping[Any, Any])
_MISSING = object()


# Serialization environment - maps type names to deserialization functions
SERIALIZE_ENV: dict[str, Callable[[Any], object]] = {
    "set": set,
    "URL": URL,
    "datetime": lambda d: datetime.fromtimestamp(d, timezone.utc),
}


def json_minify(data: JsonType | list[JsonType]) -> str:
    """Return minified JSON string (no whitespace) for payload usage."""
    return json.dumps(data, separators=(",", ":"))


def isonow() -> str:
    """Return the current UTC time in Twitch's expected ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _serialize(obj: Any) -> Any:
    """
    Custom JSON encoder for special types.

    Converts datetime, set, Enum, and URL objects to serializable format.
    Stores both the type name and the converted data for proper deserialization.
    """
    # convert data
    d: int | str | float | list[Any] | JsonType
    if isinstance(obj, datetime):
        if obj.tzinfo is None:
            # assume naive objects are UTC
            obj = obj.replace(tzinfo=timezone.utc)
        d = obj.timestamp()
    elif isinstance(obj, set):
        d = list(obj)
    elif isinstance(obj, Enum):
        # NOTE: IntEnum cannot be used, as it will get serialized as a plain integer,
        # then loaded back as an integer as well.
        d = obj.value
    elif isinstance(obj, URL):
        d = str(obj)
    else:
        raise TypeError(obj)
    # store with type
    return {
        "__type": type(obj).__name__,
        "data": d,
    }


def _remove_missing(obj: JsonType) -> JsonType:
    """
    Remove _MISSING sentinel values from a dictionary recursively.

    This modifies obj in place, but returns it for convenience.
    Used during deserialization to clean up unrecognized types.
    """
    for key, value in obj.copy().items():
        if value is _MISSING:
            del obj[key]
        elif isinstance(value, dict):
            _remove_missing(value)
            if not value:
                # the dict is empty now, so remove it's key entirely
                del obj[key]
    return obj


def _deserialize(obj: JsonType) -> Any:
    """
    Custom JSON decoder hook for special types.

    Reconstructs objects from serialized format using SERIALIZE_ENV.
    Returns _MISSING sentinel for unrecognized types (to be cleaned up later).
    """
    if "__type" in obj:
        obj_type = obj["__type"]
        if obj_type in SERIALIZE_ENV:
            return SERIALIZE_ENV[obj_type](obj["data"])
        else:
            return _MISSING
    return obj


def merge_case_variant_keys(
    data: Mapping[str, Any], *, combine: Callable[[Any, Any], Any] | None = None
) -> dict[str, Any]:
    """
    Merge dict keys that only differ by case (e.g. 'Jynxzi' vs 'jynxzi') into a
    single lowercase key.

    Twitch channel logins are case-insensitive, but some call sites historically
    stored a display-cased login (e.g. user-typed idle channel names) instead of
    the canonical lowercase one, splitting one channel's data across two keys.

    combine(existing, new) picks the merged value on conflict; defaults to max()
    for numeric values (a balance/counter only grows between snapshots) and to
    the last value seen otherwise.
    """
    merged: dict[str, Any] = {}
    for key, value in data.items():
        lower_key = key.lower()
        if lower_key in merged:
            prev = merged[lower_key]
            if combine is not None:
                merged[lower_key] = combine(prev, value)
            elif isinstance(prev, (int, float)) and isinstance(value, (int, float)):
                merged[lower_key] = max(prev, value)
            else:
                merged[lower_key] = value
        else:
            merged[lower_key] = value
    return merged


def merge_json(obj: JsonType, template: Mapping[Any, Any]) -> None:
    """
    Merge a JSON object with a template, ensuring all expected keys exist.

    NOTE: This modifies object in place.

    - Removes keys not present in template
    - Overwrites values with wrong type from template
    - Recursively merges nested dictionaries
    - Adds missing keys from template
    """
    for k, v in list(obj.items()):
        if k not in template:
            # unknown key: overwrite from template
            del obj[k]
        elif type(v) is not type(template[k]):
            # types don't match: overwrite from template
            obj[k] = template[k]
        elif isinstance(v, dict):
            assert isinstance(template[k], dict)
            merge_json(v, template[k])
    # ensure the object is not missing any keys
    for k in template:
        if k not in obj:
            obj[k] = template[k]


def json_load(path: Path, defaults: _JSON_T, *, merge: bool = True) -> _JSON_T:
    """
    Load JSON from a file with defaults and optional merging.

    Args:
        path: Path to JSON file
        defaults: Default values to use if file doesn't exist or merge is enabled
        merge: If True, merge loaded data with defaults template

    Returns:
        Loaded and optionally merged JSON data
    """
    defaults_dict: JsonType = dict(defaults)
    if path.exists():
        try:
            with open(path, encoding="utf8") as file:
                combined: JsonType = _remove_missing(json.load(file, object_hook=_deserialize))
        except json.JSONDecodeError as e:
            # A truncated/corrupt file (e.g. a write interrupted mid-flush)
            # used to raise here and propagate up to whatever caller's own
            # try/except swallowed it — silently freezing that file's cached
            # value forever, since every subsequent load attempt hit the same
            # exception before ever reaching the code that would rewrite it.
            # Falling back to defaults, same as the file-doesn't-exist path,
            # lets the next successful save heal it instead.
            logger.warning(f"Corrupt JSON in {path}, falling back to defaults: {e}")
            combined = defaults_dict
        else:
            if merge:
                merge_json(combined, defaults_dict)
    else:
        combined = defaults_dict
    return cast(_JSON_T, combined)


def json_save(path: Path, contents: Mapping[Any, Any], *, sort: bool = False) -> None:
    """
    Save data to a JSON file with custom serialization.

    Args:
        path: Path to save JSON file
        contents: Data to serialize
        sort: If True, sort keys alphabetically
    """
    with open(path, "w", encoding="utf8") as file:
        json.dump(contents, file, default=_serialize, sort_keys=sort, indent=4)
