"""
Public community drops feed, used when the account-bound "Campaigns" GQL
query is blocked by Twitch's integrity check.

The campaign LIST needs no account: the feed below lists every active
campaign. We only take id + status from it and let the normal
CampaignDetails queries fill in the rest. Never raises.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import aiohttp
from dateutil.parser import isoparse


if TYPE_CHECKING:
    from src.config import JsonType


logger = logging.getLogger("TwitchDrops")

CATALOG_URL = "https://twitch-drops-api.sunkwi.com/v2/drops"
MAX_CAMPAIGNS = 2000
MAX_AGE = timedelta(minutes=30)
MAX_BODY_BYTES = 8 * 1024 * 1024
# The API allows 1 request per 300 s per IP; instances on one host share a cache file
# so a second instance (or a restart) doesn't get a 429 and an empty campaign list.
CACHE_TTL = 300
# per-user cache dir, not the world-writable temp dir: another local user must not be able to plant a feed
CACHE_FILE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "tdm" / "public_drops_feed.json"
_not_before: float = 0.0


_ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,200}$")
_LOGIN_RE = re.compile(r"^[A-Za-z0-9_]{1,25}$")
_TEXT_MAX = 300
_URL_MAX = 500
_MAX_DROPS = 200
_MAX_BENEFITS = 30
_MAX_CHANNELS = 2000
_MAX_GROUPS = 2000
_DISTRIBUTION_TYPES = ("BADGE", "EMOTE", "DIRECT_ENTITLEMENT")


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > _TEXT_MAX:
        raise ValueError("bad text")
    return value


def _ident(value: Any) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ValueError("bad id")
    return value


def _https_url(value: Any) -> str:
    """Only plain https URLs reach the dashboard (no javascript:/data: links or images)."""
    if isinstance(value, str) and len(value) <= _URL_MAX and value.startswith("https://"):
        return value
    return ""


def _date(value: Any) -> str:
    isoparse(value)
    return value


def _clean(record: Any) -> JsonType | None:
    """Copy only the fields the miner reads, validated; None if anything is off.

    The feed is a third party's data: nothing is passed through as-is, so it
    cannot smuggle in account state (`self`), script URLs or oversized values."""
    try:
        status = record["status"]
        if status not in ("ACTIVE", "UPCOMING"):
            return None
        allow = record.get("allow") or {}
        channels = allow.get("channels") or []
        drops = record["timeBasedDrops"]
        if len(channels) > _MAX_CHANNELS or len(drops) > _MAX_DROPS:
            return None
        game = record["game"]
        cleaned: JsonType = {
            "id": _ident(record["id"]),
            "status": status,
            "name": _text(record["name"]),
            "game": {
                "id": str(int(game["id"])),
                "displayName": _text(game.get("displayName") or game.get("name")),
                "slug": game.get("slug") if _ID_RE.match(str(game.get("slug", ""))) else "",
                "boxArtURL": _https_url(game.get("boxArtURL")),
            },
            "startAt": _date(record["startAt"]),
            "endAt": _date(record["endAt"]),
            "accountLinkURL": _https_url(record.get("accountLinkURL")),
            "detailsURL": _https_url(record.get("detailsURL")),
            "allow": {
                "isEnabled": bool(allow.get("isEnabled", True)),
                "channels": [
                    {
                        "id": str(int(ch["id"])),
                        "name": ch["name"] if _LOGIN_RE.match(ch["name"]) else _fail(),
                        "displayName": str(ch.get("displayName") or ch["name"])[:50],
                    }
                    for ch in channels
                ],
            },
            "timeBasedDrops": [],
        }
        for drop in drops:
            benefits = drop["benefitEdges"] or []
            preconditions = drop.get("preconditionDrops") or []
            if len(benefits) > _MAX_BENEFITS or len(preconditions) > _MAX_DROPS:
                return None
            minutes = int(drop["requiredMinutesWatched"])
            if not 0 <= minutes <= 100_000:
                return None
            cleaned["timeBasedDrops"].append(
                {
                    "id": _ident(drop["id"]),
                    "name": _text(drop["name"]),
                    "startAt": _date(drop["startAt"]),
                    "endAt": _date(drop["endAt"]),
                    "requiredMinutesWatched": minutes,
                    "preconditionDrops": [{"id": _ident(pre["id"])} for pre in preconditions] or None,
                    "benefitEdges": [
                        {
                            "benefit": {
                                "id": _ident(edge["benefit"]["id"]),
                                "name": _text(edge["benefit"]["name"]),
                                "distributionType": edge["benefit"].get("distributionType")
                                if edge["benefit"].get("distributionType") in _DISTRIBUTION_TYPES
                                else "UNKNOWN",
                                "imageAssetURL": _https_url(edge["benefit"].get("imageAssetURL")),
                            }
                        }
                        for edge in benefits
                    ],
                }
            )
        return cleaned
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def _fail() -> Any:
    raise ValueError("bad value")


def _link_key(url: Any) -> str:
    parsed = urlparse(url) if isinstance(url, str) else None
    if not parsed or not parsed.netloc:
        return ""
    return parsed.netloc.lower() + parsed.path.rstrip("/").lower()


def link_states(reward_history: list[JsonType]) -> dict[str, bool]:
    """Account-link state per link URL, from the inventory's reward history.

    Every reward the account ever earned carries `isConnected` and the URL of
    the account link it needs -- the only per-account link signal Twitch still
    returns once the campaign queries are blocked."""
    states: dict[str, bool] = {}
    for entry in reward_history or []:
        key = _link_key(entry.get("requiredAccountLink"))
        if key:
            states[key] = states.get(key, False) or bool(entry.get("isConnected"))
    return states


def campaign_from_feed(record: JsonType, states: dict[str, bool] | None = None) -> JsonType:
    """Shape a feed record like a DropCampaignDetails response.

    The feed is not account specific. A campaign counts as linked only when the
    account's own reward history proves its link URL is connected; anything
    unproven is "not linked" rather than guessed. Campaigns already in progress
    keep their real state -- inventory data wins when merged (InventoryService)."""
    forged = dict(record)
    forged.setdefault("accountLinkURL", "")
    known = (states or {}).get(_link_key(record.get("accountLinkURL")))
    forged["self"] = {"isAccountConnected": bool(known), "linkStateUnknown": known is None}
    allow = record.get("allow") or {}
    forged["allow"] = {"isEnabled": allow.get("isEnabled", True), "channels": allow.get("channels") or []}
    forged["timeBasedDrops"] = [
        {**drop, "preconditionDrops": drop.get("preconditionDrops"), "requiredSubs": 0}
        for drop in record["timeBasedDrops"]
    ]
    return forged


def parse_catalog(payload: Any, now: datetime | None = None) -> list[JsonType]:
    """Return the usable active/upcoming campaign records, [] if the payload is unusable."""
    now = now or datetime.now(timezone.utc)
    try:
        updated = isoparse(payload["lastUpdatedAt"])
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if now - updated > MAX_AGE:
            logger.warning(f"Public drops feed is stale (last update {updated.isoformat()})")
            return []
        found: dict[str, JsonType] = {}
        groups = payload["data"]
        if not isinstance(groups, list) or len(groups) > _MAX_GROUPS:
            raise ValueError("unexpected number of groups")
        for group in groups:
            for reward in group.get("rewards") or []:
                # The feed carries box art only on the group, not on each reward's game.
                game = reward.get("game") if isinstance(reward, dict) else None
                if isinstance(game, dict) and not game.get("boxArtURL"):
                    game["boxArtURL"] = group.get("gameBoxArtURL")
                cleaned = _clean(reward)
                if cleaned is not None:
                    found[cleaned["id"]] = cleaned
                if len(found) >= MAX_CAMPAIGNS:
                    return list(found.values())
        return list(found.values())
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        logger.warning(f"Public drops feed has an unexpected format: {exc}")
        return []


def _read_cache() -> tuple[Any, float] | None:
    """(payload, age in seconds) of the shared cache file, None if absent/unreadable."""
    try:
        st = CACHE_FILE.stat()
        if st.st_size > MAX_BODY_BYTES or (hasattr(os, "getuid") and st.st_uid != os.getuid()):
            return None
        return json.loads(CACHE_FILE.read_text(encoding="utf-8")), time.time() - st.st_mtime
    except (OSError, ValueError):
        return None


def _write_cache(body: bytes) -> None:
    tmp: str | None = None
    try:
        CACHE_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=CACHE_FILE.parent, prefix=".feed-")
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        os.replace(tmp, CACHE_FILE)
    except OSError as exc:
        logger.debug(f"Could not cache the public drops feed: {exc}")
        if tmp is not None:
            Path(tmp).unlink(missing_ok=True)


def _retry_after(resp: aiohttp.ClientResponse) -> float:
    try:
        return max(0.0, min(float(resp.headers.get("Retry-After", CACHE_TTL)), 3600.0))
    except (TypeError, ValueError):
        return float(CACHE_TTL)


async def fetch_public_campaigns(
    session: aiohttp.ClientSession, proxy: str | None = None
) -> list[JsonType]:
    global _not_before
    cached = _read_cache()
    if cached is not None and cached[1] < CACHE_TTL:
        return parse_catalog(cached[0])
    if time.monotonic() >= _not_before:
        try:
            async with session.get(
                CATALOG_URL, timeout=aiohttp.ClientTimeout(total=20), proxy=proxy or None
            ) as resp:
                if resp.status == 200:
                    body = bytearray()
                    async for part in resp.content.iter_chunked(64 * 1024):
                        body += part
                        if len(body) > MAX_BODY_BYTES:
                            raise ValueError("response too large")
                    payload = json.loads(body)
                    _write_cache(bytes(body))
                    return parse_catalog(payload)
                logger.warning(f"Public drops feed returned HTTP {resp.status}")
                if resp.status == 429:
                    _not_before = time.monotonic() + _retry_after(resp)
        except Exception as exc:
            logger.warning(f"Public drops feed unavailable: {exc}")
    # rate limited or failed: an older cached copy still beats an empty list (parse_catalog rejects stale data);
    # re-read it, another instance on this host may have just fetched the feed we were refused
    cached = _read_cache() or cached
    return parse_catalog(cached[0]) if cached is not None else []
