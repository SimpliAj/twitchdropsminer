import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.exceptions import MinerException
from src.services import watch_service
from src.services.watch_service import WatchService


def _channel(online_after_recheck: bool):
    ch = MagicMock(online=True, banned=False)
    ch.name = "dnl_mmx"
    ch._stream = MagicMock(_stream_url="old")
    ch.send_watch = AsyncMock(return_value=False)
    ch.update_stream = AsyncMock(return_value=online_after_recheck)
    return ch


class TestRecheck(unittest.IsolatedAsyncioTestCase):
    async def test_offline_recheck_reports_through_update_stream(self):
        ch = _channel(False)
        await WatchService(MagicMock()).recheck_failing_channel(ch)
        ch.update_stream.assert_awaited_once()
        self.assertEqual(ch._stream._stream_url, "old")

    async def test_still_live_drops_cached_stream_url(self):
        ch = _channel(True)
        await WatchService(MagicMock()).recheck_failing_channel(ch)
        self.assertIsNone(ch._stream._stream_url)

    async def test_recheck_error_is_swallowed(self):
        ch = _channel(True)
        ch.update_stream = AsyncMock(side_effect=MinerException("boom"))
        await WatchService(MagicMock()).recheck_failing_channel(ch)


class TestWatchLoopRecheck(unittest.IsolatedAsyncioTestCase):
    async def test_failing_watches_trigger_recheck_after_threshold(self):
        ch = _channel(False)
        twitch = MagicMock()
        twitch.watching_channel.get = AsyncMock(return_value=ch)
        svc = WatchService(twitch)
        svc.watch_sleep = AsyncMock()
        clock = [0.0]

        def fake_monotonic():
            clock[0] += 50.0
            return clock[0]

        calls = []

        async def recheck(channel):
            calls.append(channel)
            raise asyncio.CancelledError  # end the endless loop

        svc.recheck_failing_channel = recheck
        with patch.object(watch_service, "monotonic", fake_monotonic):
            with self.assertRaises(asyncio.CancelledError):
                await svc.watch_loop()
        self.assertEqual(calls, [ch])
        twitch._health_monitor.record_watch_ok.assert_not_called()

    async def test_successful_watch_never_rechecks(self):
        ch = _channel(True)
        ch.send_watch = AsyncMock(return_value=True)
        ch._watch_current.return_value = False  # skip the progress part of the loop
        twitch = MagicMock()
        n = [0]

        async def get():
            n[0] += 1
            if n[0] > 20:
                raise asyncio.CancelledError
            return ch

        twitch.watching_channel.get = get
        svc = WatchService(twitch)
        svc.watch_sleep = AsyncMock()
        svc.recheck_failing_channel = AsyncMock()
        clock = [0.0]

        def fake_monotonic():
            clock[0] += 50.0
            return clock[0]

        with patch.object(watch_service, "monotonic", fake_monotonic):
            with self.assertRaises(asyncio.CancelledError):
                await svc.watch_loop()
        svc.recheck_failing_channel.assert_not_called()
