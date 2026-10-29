from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from time import monotonic
from typing import TYPE_CHECKING

import aiohttp


if TYPE_CHECKING:
    from src.core.client import Twitch

logger = logging.getLogger("TwitchDrops")

CHECK_INTERVAL_SECONDS = 60
LOGIN_STUCK_SECONDS = 30 * 60
WATCH_STALL_SECONDS = 15 * 60

_MESSAGES = {
    "login": (
        "Twitch login is waiting",
        "The account has been waiting for a Twitch login for 30+ minutes. Run the login helper.",
        "Twitch login completed.",
    ),
    "stall": (
        "Mining stalled",
        "A channel is selected but no watch has gone through for 15+ minutes.",
        "Watches are going through again.",
    ),
}


def login_in_progress() -> bool:
    from src.auth.browser_login import get_active_manager

    return get_active_manager() is not None


class HealthMonitorService:
    """Notices an instance that silently stopped earning (login pending, watches failing)
    and reports it once via Discord webhook / dashboard push, then once more on recovery."""

    def __init__(
        self,
        twitch: Twitch,
        *,
        clock: Callable[[], float] = monotonic,
        label: str | None = None,
    ) -> None:
        self._twitch = twitch
        self._clock = clock
        self._label = label or os.environ.get("TDM_LABEL") or f"port {os.environ.get('TDM_PORT') or os.environ.get('PORT') or '8080'}"
        self._last_watch_ok = clock()
        self._login_since: float | None = None
        self._active: set[str] = set()

    def record_watch_ok(self) -> None:
        self._last_watch_ok = self._clock()

    def evaluate(self) -> set[str]:
        now = self._clock()
        issues: set[str] = set()

        if login_in_progress():
            if self._login_since is None:
                self._login_since = now
            if now - self._login_since >= LOGIN_STUCK_SECONDS:
                issues.add("login")
        else:
            self._login_since = None

        should_watch = (
            self._twitch.watching_channel.get_with_default(None) is not None
            and not self._twitch.is_paused()
        )
        if not should_watch:
            # Nothing to mine (or paused): restart the window so resuming isn't an instant alarm.
            self._last_watch_ok = now
        elif now - self._last_watch_ok >= WATCH_STALL_SECONDS:
            issues.add("stall")
        return issues

    async def check_and_notify(self) -> None:
        issues = self.evaluate()
        raised, cleared = issues - self._active, self._active - issues
        self._active = issues
        for issue in sorted(raised):
            await self._notify(issue, resolved=False)
        for issue in sorted(cleared):
            await self._notify(issue, resolved=True)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
            try:
                await self.check_and_notify()
            except Exception:
                logger.exception("Health check failed")

    def snapshot(self) -> dict:
        """Current issues with their texts, for GET /api/health (polled by the Discord bot)."""
        return {
            "label": self._label,
            "active": [
                {"issue": issue, "title": title, "problem": problem, "recovery": recovery}
                for issue in sorted(self._active)
                for title, problem, recovery in [_MESSAGES[issue]]
            ],
        }

    async def _notify(self, issue: str, *, resolved: bool) -> None:
        title, problem, recovery = _MESSAGES[issue]
        if resolved:
            logger.info(f"Health: {title} resolved ({self._label})")
            embed = {"title": f"✅ TDM {self._label}: {title} resolved", "description": recovery, "color": 0x2ECC71}
        else:
            logger.warning(f"Health: {title} ({self._label}) - {problem}")
            embed = {"title": f"⚠️ TDM {self._label}: {title}", "description": problem, "color": 0xE74C3C}
        settings = self._twitch.settings
        webhook_url = getattr(settings, "discord_webhook_health", "") or getattr(
            settings, "discord_webhook_drops", ""
        )
        if webhook_url:
            await self._send_discord_webhook(webhook_url, {"embeds": [embed]})
        await self._emit({"issue": issue, "resolved": resolved, "label": self._label})

    async def _emit(self, data: dict) -> None:
        from src.web.app import sio

        await sio.emit("health_alert", data)

    async def _send_discord_webhook(self, url: str, payload: dict) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10))
        except Exception as e:
            logger.debug(f"Health alert webhook failed: {e}")
