import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.services.prediction_service import PredictionService


def _make_service(make_predictions=True, prediction_channels=None):
    twitch = MagicMock()
    twitch.settings.make_predictions = make_predictions
    twitch.settings.prediction_channels = prediction_channels or []
    twitch.settings.bet_strategy = "SMART"
    twitch.settings.bet_percentage = 5
    twitch.settings.bet_max_points = 50000
    twitch.settings.bet_minimum_points = 1000
    twitch.settings.bet_percentage_gap = 20
    twitch.settings.bet_delay_seconds = 0
    twitch.channels = {}
    return PredictionService(twitch)


def _make_channel(name):
    ch = MagicMock()
    ch.name = name
    return ch


_EVENT_CREATED_MSG = {
    "type": "event-created",
    "data": {
        "event": {
            "id": "evt-001",
            "title": "Will it happen?",
            "outcomes": [
                {"id": "o1", "title": "Yes", "total_points": 1000, "total_users": 80},
                {"id": "o2", "title": "No", "total_points": 200, "total_users": 20},
            ],
        }
    },
}


class TestPredictionChannelWhitelist(unittest.IsolatedAsyncioTestCase):
    async def test_empty_whitelist_allows_all_channels(self):
        svc = _make_service(prediction_channels=[])
        channel = _make_channel("streamer_a")
        svc._twitch.channels = {1: channel}

        with patch.object(svc, "_delayed_bet", new_callable=AsyncMock) as mock_bet:
            await svc.process_prediction(1, _EVENT_CREATED_MSG)
            # Task is created, not called directly — check pending
            self.assertIn("evt-001", svc._pending)

        # cancel pending task to avoid warnings
        for t in svc._pending.values():
            t.cancel()

    async def test_whitelist_blocks_unlisted_channel(self):
        svc = _make_service(prediction_channels=["streamer_b"])
        channel = _make_channel("streamer_a")
        svc._twitch.channels = {1: channel}

        await svc.process_prediction(1, _EVENT_CREATED_MSG)
        self.assertNotIn("evt-001", svc._pending)

    async def test_whitelist_allows_listed_channel(self):
        svc = _make_service(prediction_channels=["Streamer_A"])
        channel = _make_channel("streamer_a")
        svc._twitch.channels = {1: channel}

        await svc.process_prediction(1, _EVENT_CREATED_MSG)
        self.assertIn("evt-001", svc._pending)

        for t in svc._pending.values():
            t.cancel()

    async def test_whitelist_case_insensitive(self):
        svc = _make_service(prediction_channels=["STREAMER_A"])
        channel = _make_channel("Streamer_A")
        svc._twitch.channels = {1: channel}

        await svc.process_prediction(1, _EVENT_CREATED_MSG)
        self.assertIn("evt-001", svc._pending)

        for t in svc._pending.values():
            t.cancel()


if __name__ == "__main__":
    unittest.main()
