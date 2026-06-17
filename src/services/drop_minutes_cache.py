"""Persists drop watch minutes locally to survive Twitch API inconsistencies."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from src.config.paths import DATA_DIR

logger = logging.getLogger(__name__)

_CACHE_FILE = DATA_DIR / "drop_minutes_cache.json"
_cache: dict[str, int] = {}
_dirty = False


def load():
    global _cache
    try:
        if _CACHE_FILE.exists():
            _cache = json.loads(_CACHE_FILE.read_text())
    except Exception:
        _cache = {}


def get(drop_id: str, api_minutes: int) -> int:
    return max(api_minutes, _cache.get(drop_id, 0))


def update(drop_id: str, minutes: int):
    global _dirty
    if minutes > 0 and minutes > _cache.get(drop_id, 0):
        _cache[drop_id] = minutes
        _dirty = True
        _flush()


def _flush():
    global _dirty
    if not _dirty:
        return
    try:
        _CACHE_FILE.write_text(json.dumps(_cache))
        _dirty = False
    except Exception as e:
        logger.debug(f"drop_minutes_cache flush failed: {e}")
