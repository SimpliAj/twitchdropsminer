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

    async def test_unexpected_lookup_error_is_swallowed(self):
        ch = _channel(True)
        ch.update_stream = AsyncMock(side_effect=RuntimeError("network"))
        await WatchService(MagicMock()).recheck_failing_channel(ch)
        self.assertEqual(ch._stream._stream_url, "old")


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


class _Stop(Exception):
    pass


class TestReloadKeepsWatchedChannel(unittest.IsolatedAsyncioTestCase):
    """Drives the real state loop through CHANNELS_CLEANUP."""

    async def _cleanup(self, *, offline_after_recheck, full_cleanup_channels):
        from src.core.client import Twitch
        from src.utils import AwaitableValue
        from src.config.constants import State

        watched = MagicMock(id=1, offline=False, game="wanted")
        watched.name = "watched"
        other = MagicMock(id=2, offline=False, game="other-not-wanted", pending_online=False)

        async def recheck(channel):
            channel.offline = offline_after_recheck

        client = MagicMock()
        client._health_task = object()
        client.get_auth = AsyncMock(return_value=MagicMock(user_id=42))
        client.websocket.start = AsyncMock()
        client._irc_service.start = AsyncMock()
        client._watch_service.watch_loop = AsyncMock()
        client._scheduler_service.run_scheduler = AsyncMock()
        client.watching_channel = AwaitableValue()
        client.watching_channel.set(watched)
        client.wanted_games = ["wanted"]
        client.channels = {}
        client._watch_service.recheck_failing_channel = AsyncMock(side_effect=recheck)
        client.settings.idle_use_followed = False
        client.channels.update({ch.id: ch for ch in full_cleanup_channels})
        client._state = State.INVENTORY_FETCH

        def change_state(state):
            if state is State.INVENTORY_FETCH:
                client._state = State.CHANNELS_CLEANUP
            elif state is State.CHANNELS_FETCH:
                raise _Stop

        client.change_state = change_state
        with self.assertRaises(_Stop):
            await Twitch._run(client)
        return client, watched, other

    async def test_live_watched_channel_stays_tracked_with_its_topics(self):
        client, watched, _ = await self._cleanup(offline_after_recheck=False, full_cleanup_channels=[])
        client._watch_service.recheck_failing_channel.assert_awaited_once_with(watched)
        self.assertIs(client.channels[watched.id], watched)
        client._remove_channel_topics.assert_not_called()

    async def test_ended_watched_channel_is_not_forced_back(self):
        client, watched, _ = await self._cleanup(offline_after_recheck=True, full_cleanup_channels=[])
        client._watch_service.recheck_failing_channel.assert_awaited_once_with(watched)
        self.assertNotIn(watched.id, client.channels)
