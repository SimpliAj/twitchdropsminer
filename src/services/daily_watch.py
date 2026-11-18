"""
Daily watch: keep watching a Twitch category for N minutes per UTC day.

For events that are not Drops campaigns (e.g. viewership badges), so the drop
miner never watches them. Runs as its own task, alongside normal mining, and
reuses Channel.send_watch like idle-parallel does.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timezone
from time import monotonic
from typing import TYPE_CHECKING, Any

from src.config import WATCH_INTERVAL, ClientType
from src.utils import json_load, json_save


if TYPE_CHECKING:
    from pathlib import Path

    from src.core.client import Twitch
    from src.models.channel import Channel


logger = logging.getLogger("TwitchDrops")

GQL_URL = "https://gql.twitch.tv/gql"
# Looking up a live channel is throttled per game; an offline pick is retried at most this often.
REPICK_INTERVAL = 300.0
# Consecutive failed watches before the channel is considered dead and re-picked.
MAX_FAILS = 3
CANDIDATES = 5

STREAMS_QUERY = (
    "query($name:String!,$n:Int!){game(name:$name){streams(first:$n){edges{node{"
    "viewersCount broadcaster{login}}}}}}"
)
SEARCH_QUERY = (
    "query($q:String!){searchCategories(query:$q, first:10){edges{node{name}}}}"
)


def clamp_minutes(value: Any) -> int:
    try:
        return max(1, min(240, int(value)))
    except (TypeError, ValueError):
        return 60


async def anonymous_gql(twitch: Twitch, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    """Public data needs no login; the web client id is enough."""

    async def call() -> dict[str, Any]:
        async with twitch.request(
            "POST",
            GQL_URL,
            json={"query": query, "variables": variables},
            headers={"Client-Id": ClientType.WEB.CLIENT_ID},
        ) as response:
            return await response.json()

    # request() retries server errors forever; bound the whole call
    return await asyncio.wait_for(call(), timeout=15)


async def search_categories(twitch: Twitch, query: str) -> list[str]:
    """Twitch category names matching the text; never raises."""
    try:
        data = await anonymous_gql(twitch, SEARCH_QUERY, {"q": query})
        edges = data["data"]["searchCategories"]["edges"]
        return [e["node"]["name"] for e in edges if e["node"]["name"]][:10]
    except Exception as exc:
        logger.debug(f"Category search failed: {type(exc).__name__}")
        return []


class DailyWatch:
    def __init__(self, twitch: Twitch, path: Path) -> None:
        self._twitch = twitch
        self._path = path
        self._date = ""
        self._minutes: dict[str, int] = {}
        self._channels: dict[str, Channel] = {}
        self._last_pick: dict[str, float] = {}
        self._fails: dict[str, int] = {}
        self._warned: set[str] = set()
        self._load()

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def _load(self) -> None:
        data = json_load(self._path, {"date": "", "minutes": {}}, merge=False)
        minutes = data.get("minutes") if isinstance(data, dict) else None
        if isinstance(minutes, dict):
            self._date = str(data.get("date", ""))
            self._minutes = {str(k): int(v) for k, v in minutes.items() if isinstance(v, int)}

    def _save(self) -> None:
        with suppress(OSError):
            json_save(self._path, {"date": self._date, "minutes": self._minutes}, indent=None)

    def _roll(self) -> None:
        today = self._today()
        if self._date != today:
            self._date, self._minutes = today, {}
            self._warned.clear()
            self._save()

    @property
    def _quota(self) -> int:
        return clamp_minutes(getattr(self._twitch.settings, "daily_watch_minutes", 60))

    def status(self) -> list[dict[str, Any]]:
        self._roll()
        quota = self._quota
        result = []
        for game in getattr(self._twitch.settings, "daily_watch_games", []):
            channel = self._channels.get(game.lower())
            result.append({
                "game": game,
                "minutes": min(self._minutes.get(game.lower(), 0), quota),
                "quota": quota,
                "channel": channel.name if channel is not None and channel.online else None,
            })
        return result

    async def _find_channel(self, game: str) -> Channel | None:
        data = await anonymous_gql(self._twitch, STREAMS_QUERY, {"name": game, "n": CANDIDATES})
        edges = data["data"]["game"]["streams"]["edges"]
        # most viewed first, but only a channel the normal stream lookup can actually play
        for edge in sorted(edges, key=lambda e: -e["node"]["viewersCount"]):
            channel = await self._twitch._fetch_idle_channel_by_login(edge["node"]["broadcaster"]["login"])
            if channel is not None:
                return channel
        return None

    def _warn_once(self, key: str, message: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            logger.warning(message)

    async def _ensure_channel(self, game: str, key: str, quota: int) -> Channel | None:
        channel = self._channels.get(key)
        if channel is not None and channel.online and not channel.banned:
            return channel
        self._channels.pop(key, None)
        if monotonic() - self._last_pick.get(key, -REPICK_INTERVAL) < REPICK_INTERVAL:
            return None
        self._last_pick[key] = monotonic()
        try:
            channel = await self._find_channel(game)
        except Exception as exc:
            self._warn_once(key, f"Daily watch: could not look up streams for {game}: {type(exc).__name__}")
            return None
        if channel is None:
            self._warn_once(key, f"Daily watch: no live channel found for {game}")
            return None
        self._warned.discard(key)
        self._fails[key] = 0
        self._channels[key] = channel
        logger.info(
            f"Daily watch: watching {channel.name} for {game} "
            f"({self._minutes.get(key, 0)}/{quota} min today)"
        )
        return channel

    async def tick(self) -> None:
        """One minute: watch each configured game that has not reached today's quota."""
        self._roll()
        quota = self._quota
        games = {g.lower(): g for g in getattr(self._twitch.settings, "daily_watch_games", [])}
        for key in set(self._channels) - set(games):
            del self._channels[key]
        for key, game in games.items():
            if self._minutes.get(key, 0) >= quota:
                self._channels.pop(key, None)
                continue
            channel = await self._ensure_channel(game, key, quota)
            if channel is None:
                continue
            if await channel.send_watch():
                self._fails[key] = 0
                self._warned.discard(key)
                self._minutes[key] = self._minutes.get(key, 0) + 1
                self._save()
                logger.debug(f"Daily watch: {game} {self._minutes[key]}/{quota} min via {channel.name}")
                if self._minutes[key] >= quota:
                    self._channels.pop(key, None)
                    logger.info(f"Daily watch: {game} reached {quota} min for today")
            else:
                self._fails[key] = self._fails.get(key, 0) + 1
                self._warn_once(key, f"Daily watch: watch for {channel.name} ({game}) failed")
                if self._fails[key] >= MAX_FAILS:
                    del self._channels[key]

    async def run(self) -> None:
        while True:
            if not self._twitch._is_paused:
                try:
                    await self.tick()
                except Exception as exc:  # never take the miner down
                    logger.warning(f"Daily watch error: {type(exc).__name__}: {exc}")
            await asyncio.sleep(WATCH_INTERVAL.total_seconds())
