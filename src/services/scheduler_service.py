"""Scheduler service for automatic pause/resume based on time of day."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.client import Twitch

logger = logging.getLogger("TwitchDrops")


class SchedulerService:
    def __init__(self, twitch: Twitch) -> None:
        self._twitch = twitch
        self._trigger: asyncio.Event | None = None

    def _parse_time(self, time_str: str) -> time:
        parts = time_str.strip().split(":")
        return time(int(parts[0]), int(parts[1]))

    def _should_be_paused(self) -> bool:
        start = self._parse_time(self._twitch.settings.scheduler_start)
        stop = self._parse_time(self._twitch.settings.scheduler_stop)
        now = datetime.now().time()
        if start < stop:
            return not (start <= now < stop)
        else:
            return not (now >= start or now < stop)

    def trigger_check(self) -> None:
        """Trigger an immediate scheduler check."""
        if self._trigger is not None:
            self._trigger.set()

    async def run_scheduler(self) -> None:
        self._trigger = asyncio.Event()  # create inside running loop
        logger.info("Scheduler service started")
        while True:
            if self._twitch.settings.scheduler_enabled:
                should_pause = self._should_be_paused()
                logger.info(f"Scheduler check: should_pause={should_pause}, is_paused={self._twitch.is_paused()}, override={self._twitch._user_override}")
                if should_pause and not self._twitch.is_paused() and not self._twitch._user_override:
                    logger.info("Scheduler: pausing mining (outside active window)")
                    self._twitch.pause(source="scheduler")
                elif not should_pause and self._twitch.is_paused() and self._twitch._pause_source == "scheduler":
                    logger.info("Scheduler: resuming mining (entering active window)")
                    self._twitch.resume()
            self._trigger.clear()
            try:
                await asyncio.wait_for(self._trigger.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
