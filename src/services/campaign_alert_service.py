from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    from src.core.client import Twitch
    from src.models.campaign import DropsCampaign

logger = logging.getLogger("TwitchDrops")
_ALERT_THRESHOLD_HOURS = 24


class CampaignAlertService:
    def __init__(self, twitch: Twitch) -> None:
        self._twitch = twitch
        self._alerted: set[str] = set()
        from src.config import DATA_DIR
        self._alert_file = DATA_DIR / "alerted_campaigns.json"
        self._load_alerted()

    def _load_alerted(self) -> None:
        if self._alert_file.exists():
            try:
                self._alerted = set(json.loads(self._alert_file.read_text()))
            except Exception:
                self._alerted = set()

    def _save_alerted(self) -> None:
        self._alert_file.parent.mkdir(exist_ok=True)
        self._alert_file.write_text(json.dumps(list(self._alerted)))

    def _get_campaigns_to_alert(self, campaigns: list[DropsCampaign]) -> list[DropsCampaign]:
        now = datetime.now(timezone.utc)
        threshold = now + timedelta(hours=_ALERT_THRESHOLD_HOURS)
        wanted = self._twitch.wanted_games
        result = []
        for c in campaigns:
            if c.id in self._alerted:
                continue
            if c.finished:
                continue
            if c.remaining_drops <= 0:
                continue
            if wanted and c.game not in wanted:
                continue
            if c.ends_at <= threshold:
                result.append(c)
        return result

    async def check_and_alert(self, campaigns: list[DropsCampaign]) -> None:
        to_alert = self._get_campaigns_to_alert(campaigns)
        if not to_alert:
            return

        from src.web.app import _get_push_config
        push_cfg = _get_push_config()
        alerts_enabled = push_cfg.get("campaign_end_alerts_enabled", True)
        if not alerts_enabled:
            return

        webhook_url = getattr(self._twitch.settings, "discord_webhook_drops", "")

        for campaign in to_alert:
            hours_left = int((campaign.ends_at - datetime.now(timezone.utc)).total_seconds() / 3600)
            logger.info(f"Campaign ending alert: {campaign.name} ({hours_left}h left)")
            self._alerted.add(campaign.id)

            if webhook_url:
                embed = {
                    "title": "⏰ Campaign Ending Soon",
                    "description": f"**{campaign.name}** ends in ~{hours_left}h",
                    "color": 0xFF4500,
                    "fields": [
                        {"name": "Game", "value": campaign.game.name, "inline": True},
                        {"name": "Unclaimed Drops", "value": str(campaign.remaining_drops), "inline": True},
                    ],
                    "url": campaign.campaign_url,
                }
                asyncio.create_task(self._send_discord_webhook(webhook_url, {"embeds": [embed]}))

        self._save_alerted()

        # Notify frontend via socket for browser push
        from src.web.app import sio
        await sio.emit("campaign_end_alert", [
            {
                "id": c.id,
                "name": c.name,
                "game": c.game.name,
                "hours_left": int((c.ends_at - datetime.now(timezone.utc)).total_seconds() / 3600),
                "remaining_drops": c.remaining_drops,
            }
            for c in to_alert
        ])

    async def _send_discord_webhook(self, url: str, payload: dict) -> None:
        if not url:
            return
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10))
        except Exception as e:
            logger.debug(f"Campaign alert webhook failed: {e}")
