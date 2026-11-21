import asyncio
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.config.settings import Settings
from src.services import daily_watch
from src.services.daily_watch import DailyWatch, search_categories
from src.web.managers.settings import SettingsManager


def _channel(name="chan", online=True, watch_ok=True):
    return SimpleNamespace(
        name=name, online=online, banned=False, send_watch=AsyncMock(return_value=watch_ok)
    )


def _twitch(games=("GTA",), minutes=3):
    twitch = MagicMock()
    twitch._is_paused = False
    twitch.settings = SimpleNamespace(daily_watch_games=list(games), daily_watch_minutes=minutes)
    return twitch


class DailyWatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "daily_watch.json"
        self.addCleanup(self._tmp.cleanup)

    def _make(self, twitch, channel):
        dw = DailyWatch(twitch, self.path)
        dw._find_channel = AsyncMock(return_value=channel)
        return dw

    async def test_minutes_count_only_on_successful_watch(self):
        dw = self._make(_twitch(), _channel(watch_ok=False))
        await dw.tick()
        self.assertEqual(dw.status()[0]["minutes"], 0)
        dw._channels["gta"].send_watch.return_value = True
        await dw.tick()
        self.assertEqual(dw.status()[0]["minutes"], 1)

    async def test_quota_stops_watching(self):
        channel = _channel()
        dw = self._make(_twitch(minutes=2), channel)
        for _ in range(5):
            await dw.tick()
        self.assertEqual(channel.send_watch.await_count, 2)
        self.assertEqual(dw.status()[0]["minutes"], 2)

    async def test_utc_day_rollover_resets_progress(self):
        dw = self._make(_twitch(), _channel())
        with patch.object(DailyWatch, "_today", return_value="2026-11-18"):
            await dw.tick()
            await dw.tick()
            self.assertEqual(dw.status()[0]["minutes"], 2)
        with patch.object(DailyWatch, "_today", return_value="2026-11-19"):
            self.assertEqual(dw.status()[0]["minutes"], 0)
            await dw.tick()
            self.assertEqual(dw.status()[0]["minutes"], 1)

    async def test_offline_channel_is_repicked_after_throttle(self):
        first, second = _channel("a"), _channel("b")
        dw = self._make(_twitch(), first)
        await dw.tick()
        first.online = False
        dw._find_channel.return_value = second
        await dw.tick()  # within the throttle window: nothing is watched
        self.assertEqual(second.send_watch.await_count, 0)
        dw._last_pick["gta"] -= daily_watch.REPICK_INTERVAL
        await dw.tick()
        self.assertEqual(second.send_watch.await_count, 1)
        self.assertEqual(dw.status()[0]["channel"], "b")

    async def test_removed_game_stops_sending(self):
        twitch = _twitch()
        channel = _channel()
        dw = self._make(twitch, channel)
        await dw.tick()
        twitch.settings.daily_watch_games = []
        await dw.tick()
        self.assertEqual(channel.send_watch.await_count, 1)
        self.assertEqual(dw._channels, {})

    async def test_lookup_failure_does_not_raise(self):
        dw = DailyWatch(_twitch(), self.path)
        dw._find_channel = AsyncMock(side_effect=RuntimeError("boom"))
        await dw.tick()
        self.assertEqual(dw.status()[0]["minutes"], 0)

    async def test_persistence_round_trip(self):
        dw = self._make(_twitch(), _channel())
        await dw.tick()
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["minutes"], {"gta": 1})
        again = DailyWatch(_twitch(), self.path)
        again._date = saved["date"]  # same day: progress is kept
        self.assertEqual(again._minutes, {"gta": 1})
        self.assertEqual(saved["date"], datetime.now(timezone.utc).date().isoformat())


class SearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_returns_names_and_swallows_errors(self):
        twitch = MagicMock()
        payload = {"data": {"searchCategories": {"edges": [{"node": {"name": "Grand Theft Auto VI"}}]}}}
        with patch.object(daily_watch, "anonymous_gql", AsyncMock(return_value=payload)):
            self.assertEqual(await search_categories(twitch, "grand"), ["Grand Theft Auto VI"])
        with patch.object(daily_watch, "anonymous_gql", AsyncMock(side_effect=OSError)):
            self.assertEqual(await search_categories(twitch, "grand"), [])

    async def test_endpoint_validates_query_length(self):
        from src.web import app as web_app

        search = AsyncMock(return_value=["X"])
        with patch.object(web_app, "twitch_client", MagicMock()), \
                patch("src.services.daily_watch.search_categories", search):
            self.assertEqual(await web_app.search_games(q="a"), {"games": []})
            self.assertEqual(await web_app.search_games(q="x" * 61), {"games": []})
            search.assert_not_awaited()
            self.assertEqual(await web_app.search_games(q=" ab "), {"games": ["X"]})
            search.assert_awaited_once()


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_daily_watch_change_does_not_reload(self):
        settings = MagicMock(spec=Settings)
        settings.daily_watch_games = []
        settings.daily_watch_minutes = 60
        settings.make_predictions = False
        on_change = MagicMock()
        manager = SettingsManager(
            AsyncMock(), settings, MagicMock(), on_change=on_change, reload_debounce_seconds=0
        )
        manager.update_settings({"daily_watch_games": ["GTA"], "daily_watch_minutes": 9999})
        await asyncio.sleep(0.05)
        on_change.assert_not_called()
        self.assertEqual(settings.daily_watch_games, ["GTA"])
        self.assertEqual(settings.daily_watch_minutes, 240)


if __name__ == "__main__":
    unittest.main()


class WatchGateTests(unittest.TestCase):
    def test_daily_watch_channel_counts_as_watched(self):
        # Channel.send_watch drops watches for channels nobody is watching; daily watch
        # channels must pass that gate (live run: every watch failed without this).
        from src.models.channel import Channel

        twitch = MagicMock()
        twitch.watching_channel.get_with_default.return_value = None
        twitch._idle_channels_set = set()
        channel = Channel(twitch, id=1, login="honeymad")
        stream = MagicMock()
        channel._stream = stream
        twitch.daily_watch.channels.return_value = set()
        self.assertFalse(channel._watch_current(stream))
        twitch.daily_watch.channels.return_value = {channel}
        self.assertTrue(channel._watch_current(stream))
