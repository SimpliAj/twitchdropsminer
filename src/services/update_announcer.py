from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

import aiohttp

from src.config.paths import DATA_DIR
from src.version import __version__


if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger("TwitchDrops")

RELEASES_API = "https://api.github.com/repos/SimpliAj/twitchdropsminer/releases/latest"
CHECK_INTERVAL = 6 * 3600
ANNOUNCED_FILE = DATA_DIR / "last_announced_version.txt"


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", version))


def is_newer(latest: str, current: str) -> bool:
    return bool(latest) and version_key(latest) > version_key(current)


async def fetch_latest_release() -> dict | None:
    """Latest GitHub release as {version, url, notes}, or None when unavailable."""
    async with (
        aiohttp.ClientSession() as session,
        session.get(RELEASES_API, timeout=aiohttp.ClientTimeout(total=5)) as response,
    ):
        if response.status != 200:
            return None
        data = await response.json()
    return {
        "version": data.get("tag_name", "").lstrip("v"),
        "url": data.get("html_url"),
        "notes": data.get("body", ""),
    }


async def announce_new_release(webhook_url: str) -> None:
    """Post one webhook message per new release; never raises."""
    if not webhook_url:
        return
    try:
        release = await fetch_latest_release()
        if not release or not is_newer(release["version"], __version__):
            return
        try:
            if ANNOUNCED_FILE.read_text().strip() == release["version"]:
                return
        except OSError:
            pass
        embed = {
            "title": f"🆕 TDM {release['version']} is available",
            "description": f"You are running {__version__}.",
            "url": release["url"],
            "color": 0x9146FF,
        }
        async with (
            aiohttp.ClientSession() as session,
            session.post(
                webhook_url, json={"embeds": [embed]}, timeout=aiohttp.ClientTimeout(total=10)
            ) as response,
        ):
            if response.status >= 300:
                logger.warning(f"Release announcement rejected (status {response.status})")
                return
        ANNOUNCED_FILE.write_text(release["version"])
    except Exception as exc:
        logger.warning(f"Release announcement failed: {exc!r}")


async def announce_loop(get_webhook_url: Callable[[], str]) -> None:
    while True:
        await announce_new_release(get_webhook_url())
        await asyncio.sleep(CHECK_INTERVAL)
