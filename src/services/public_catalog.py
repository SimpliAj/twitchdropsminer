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
from datetime import datetime, timedelta, timezone
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
MAX_ID_LEN = 128


def _usable(record: Any) -> bool:
    """True if the record has everything DropsCampaign / TimedDrop read from it."""
    try:
        if not (isinstance(record["name"], str) and str(int(record["game"]["id"]))):
            return False
        isoparse(record["startAt"])
        isoparse(record["endAt"])
        if not isinstance((record.get("allow") or {}).get("channels") or [], list):
            return False
        drops = record["timeBasedDrops"]
        if not isinstance(drops, list):
            return False
        for drop in drops:
            if not (isinstance(drop["id"], str) and isinstance(drop["name"], str)):
                return False
            isoparse(drop["startAt"])
            isoparse(drop["endAt"])
            int(drop["requiredMinutesWatched"])
            if not isinstance(drop["benefitEdges"] or [], list):
                return False
        return True
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


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
    forged["self"] = {"isAccountConnected": bool((states or {}).get(_link_key(record.get("accountLinkURL")), False))}
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
        for group in payload["data"]:
            for reward in group.get("rewards") or []:
                cid, status = reward.get("id"), reward.get("status")
                if (
                    isinstance(cid, str)
                    and 0 < len(cid) <= MAX_ID_LEN
                    and status in ("ACTIVE", "UPCOMING")
                    and _usable(reward)
                ):
                    found[cid] = reward
                if len(found) >= MAX_CAMPAIGNS:
                    return list(found.values())
        return list(found.values())
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        logger.warning(f"Public drops feed has an unexpected format: {exc}")
        return []


async def fetch_public_campaigns(
    session: aiohttp.ClientSession, proxy: str | None = None
) -> list[JsonType]:
    try:
        async with session.get(
            CATALOG_URL, timeout=aiohttp.ClientTimeout(total=20), proxy=proxy or None
        ) as resp:
            if resp.status != 200:
                logger.warning(f"Public drops feed returned HTTP {resp.status}")
                return []
            body = bytearray()
            async for part in resp.content.iter_chunked(64 * 1024):
                body += part
                if len(body) > MAX_BODY_BYTES:
                    logger.warning("Public drops feed response too large, ignoring it")
                    return []
            payload = json.loads(body)
    except Exception as exc:
        logger.warning(f"Public drops feed unavailable: {exc}")
        return []
    return parse_catalog(payload)
