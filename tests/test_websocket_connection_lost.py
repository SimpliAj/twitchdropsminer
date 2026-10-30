import unittest
from unittest.mock import AsyncMock, MagicMock

import aiohttp
from aiohttp import WSMsgType

from src.exceptions import WebsocketClosed
from src.websocket.websocket import Websocket


class TestWebsocketConnectionLost(unittest.IsolatedAsyncioTestCase):
    """A missed PONG during a network blip is a normal reconnect, not an
    error with a traceback in the log."""

    def _make_websocket(self, data) -> Websocket:
        pool = MagicMock()
        pool._twitch = MagicMock()
        ws = Websocket(pool, index=0)
        fake = MagicMock()
        fake.receive = AsyncMock(return_value=aiohttp.WSMessage(WSMsgType.ERROR, data, None))
        ws._ws.set(fake)
        return ws

    async def test_pong_timeout_is_a_warning(self):
        ws = self._make_websocket(aiohttp.ServerTimeoutError("No PONG received after 15.0 seconds"))
        with self.assertLogs("TwitchDrops.websocket", level="WARNING") as logs:
            with self.assertRaises(WebsocketClosed):
                await ws._gather_recv([], timeout=0.5)
        self.assertEqual([r.levelname for r in logs.records], ["WARNING"])

    async def test_other_errors_still_log_as_errors(self):
        ws = self._make_websocket(ValueError("boom"))
        with self.assertLogs("TwitchDrops.websocket", level="WARNING") as logs:
            with self.assertRaises(WebsocketClosed):
                await ws._gather_recv([], timeout=0.5)
        self.assertEqual([r.levelname for r in logs.records], ["ERROR"])
