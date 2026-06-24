from __future__ import annotations

import asyncio
import logging
import ssl
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.client import Twitch

logger = logging.getLogger("TwitchDrops")

IRC_HOST = "irc.chat.twitch.tv"
IRC_PORT = 6697
PING_INTERVAL = 240  # seconds


class IRCService:
    def __init__(self, twitch: Twitch):
        self._twitch = twitch
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._current_channel: str | None = None
        self._running = False
        self._task: asyncio.Task | None = None
        self._mention_cooldowns: dict[str, float] = {}  # channel -> last_notified timestamp

    async def start(self) -> None:
        if not self._twitch.settings.irc_chat_presence:
            return
        self._running = True
        self._task = asyncio.create_task(self._connect_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass

    async def join(self, channel_login: str) -> None:
        if not self._twitch.settings.irc_chat_presence or not self._writer:
            return
        channel_login = channel_login.lower()
        if self._current_channel:
            await self._send(f"PART #{self._current_channel}")
        self._current_channel = channel_login
        await self._send(f"JOIN #{channel_login}")
        logger.debug(f"IRC: joined #{channel_login}")

    async def part(self, channel_login: str) -> None:
        if self._writer and self._current_channel == channel_login.lower():
            await self._send(f"PART #{channel_login.lower()}")
            self._current_channel = None

    async def _send(self, line: str) -> None:
        if self._writer:
            try:
                self._writer.write((line + "\r\n").encode())
                await self._writer.drain()
            except Exception as e:
                logger.debug(f"IRC send error: {e}")

    async def _connect_loop(self) -> None:
        while self._running:
            try:
                await self._connect()
            except Exception as e:
                logger.warning(f"IRC disconnected: {e}")
            finally:
                self._writer = None
                self._reader = None
            if self._running:
                await asyncio.sleep(30)

    async def _connect(self) -> None:
        token = getattr(self._twitch._auth_state, "access_token", "") or ""
        username = (getattr(self._twitch._auth_state, "user_login", "") or "justinfan12345").lower()
        ssl_ctx = ssl.create_default_context()
        self._reader, self._writer = await asyncio.open_connection(IRC_HOST, IRC_PORT, ssl=ssl_ctx)
        await self._send(f"PASS oauth:{token}")
        await self._send(f"NICK {username}")
        await self._send("CAP REQ :twitch.tv/commands twitch.tv/tags")
        if self._current_channel:
            await self._send(f"JOIN #{self._current_channel}")
        ping_task = asyncio.create_task(self._ping_loop())
        try:
            while self._running:
                line = await asyncio.wait_for(self._reader.readline(), timeout=PING_INTERVAL + 60)
                line = line.decode(errors="ignore").strip()
                if not line:
                    continue
                if line.startswith("PING"):
                    await self._send("PONG " + line[5:])
                elif "PRIVMSG" in line:
                    self._on_privmsg(line)
        finally:
            ping_task.cancel()

    async def _ping_loop(self) -> None:
        while self._running:
            await asyncio.sleep(PING_INTERVAL)
            await self._send("PING :tmi.twitch.tv")

    def _on_privmsg(self, line: str) -> None:
        if not self._twitch.settings.irc_mention_notify:
            return
        username = (getattr(self._twitch._auth_state, "user_login", "") or "").lower()
        if not username:
            return
        # Parse: @tags :nick!user@host PRIVMSG #channel :message
        try:
            parts = line.split("PRIVMSG", 1)
            if len(parts) < 2:
                return
            channel_msg = parts[1].strip()
            channel, _, text = channel_msg.partition(" :")
            channel = channel.strip().lstrip("#")
            text = text.strip()
        except Exception:
            return
        if username not in text.lower():
            return
        now = time.monotonic()
        last = self._mention_cooldowns.get(channel, 0)
        if now - last < 60:
            return
        self._mention_cooldowns[channel] = now
        asyncio.create_task(self._notify_mention(channel, text))

    async def _notify_mention(self, channel: str, text: str) -> None:
        webhook_url = (
            self._twitch.settings.discord_webhook_mentions
            or self._twitch.settings.discord_webhook_points
        )
        if not webhook_url:
            return
        embed = {
            "title": "📣 Mention in Chat",
            "color": 0xFFB800,
            "fields": [
                {"name": "Channel", "value": channel, "inline": True},
                {"name": "Message", "value": text[:500], "inline": False},
            ],
        }
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                await session.post(webhook_url, json={"embeds": [embed]}, timeout=aiohttp.ClientTimeout(total=10))
        except Exception as e:
            logger.debug(f"Mention webhook failed: {e}")
