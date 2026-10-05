"""
Public community drops feed, used when the account-bound "Campaigns" GQL
query is blocked by Twitch's integrity check.

The campaign LIST needs no account: the feed below lists every active
campaign. We only take id + status from it and let the normal
CampaignDetails queries fill in the rest. Never raises.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import aiohttp
from dateutil.parser import isoparse


if TYPE_CHECKING:
    from src.config import JsonType


logger = logging.getLogger("TwitchDrops")

CATALOG_URL = "https://twitch-drops-api.sunkwi.com/v2/drops"
MAX_CAMPAIGNS = 2000
MAX_AGE = timedelta(minutes=30)


def parse_catalog(payload: Any, now: datetime | None = None) -> list[JsonType]:
    """Return [{"id", "status"}] for active/upcoming campaigns, [] if the payload is unusable."""
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
                if cid and status in ("ACTIVE", "UPCOMING"):
                    found[cid] = {"id": cid, "status": status}
                if len(found) >= MAX_CAMPAIGNS:
                    return list(found.values())
        return list(found.values())
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        logger.warning(f"Public drops feed has an unexpected format: {exc}")
        return []


async def fetch_public_campaigns(session: aiohttp.ClientSession) -> list[JsonType]:
    try:
        async with session.get(CATALOG_URL, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                logger.warning(f"Public drops feed returned HTTP {resp.status}")
                return []
            payload = await resp.json(content_type=None)
    except Exception as exc:
        logger.warning(f"Public drops feed unavailable: {exc}")
        return []
    return parse_catalog(payload)
