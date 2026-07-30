import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.services.prediction_service import PredictionService, sweep_stale_pending_by_age


def _entry(channel="streamer_a", result="PENDING", hours_ago=1, event_id="evt-1"):
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    return {"event_id": event_id, "ts": ts, "channel": channel, "result": result}


class TestSweepStalePendingByAge(unittest.TestCase):
    def test_old_pending_becomes_unknown(self):
        hist = [_entry(hours_ago=25)]
        self.assertTrue(sweep_stale_pending_by_age(hist))
        self.assertEqual(hist[0]["result"], "UNKNOWN")

    def test_recent_pending_stays_pending(self):
        hist = [_entry(hours_ago=1)]
        self.assertFalse(sweep_stale_pending_by_age(hist))
        self.assertEqual(hist[0]["result"], "PENDING")

    def test_non_pending_entries_untouched(self):
        hist = [_entry(hours_ago=25, result="WIN")]
        self.assertFalse(sweep_stale_pending_by_age(hist))
        self.assertEqual(hist[0]["result"], "WIN")

    def test_malformed_timestamp_is_not_treated_as_stale(self):
        hist = [{"event_id": "e1", "ts": "not-a-timestamp", "channel": "a", "result": "PENDING"}]
        self.assertFalse(sweep_stale_pending_by_age(hist))
        self.assertEqual(hist[0]["result"], "PENDING")


class TestMarkChannelStalePending(unittest.TestCase):
    def test_flips_only_matching_channel_pending_entries(self):
        hist = [
            _entry(channel="streamer_a", result="PENDING", event_id="e1"),
            _entry(channel="streamer_b", result="PENDING", event_id="e2"),
            _entry(channel="streamer_a", result="WIN", event_id="e3"),
        ]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "predictions_history.json"
            p.write_text(json.dumps(hist))
            with patch("src.services.prediction_service._get_predictions_file", return_value=p):
                svc = PredictionService(MagicMock())
                svc.mark_channel_stale_pending("streamer_a")
            result = json.loads(p.read_text())

        by_id = {e["event_id"]: e for e in result}
        self.assertEqual(by_id["e1"]["result"], "UNKNOWN")
        self.assertEqual(by_id["e2"]["result"], "PENDING")
        self.assertEqual(by_id["e3"]["result"], "WIN")

    def test_channel_matching_is_case_insensitive(self):
        hist = [_entry(channel="streamer_a", result="PENDING", event_id="e1")]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "predictions_history.json"
            p.write_text(json.dumps(hist))
            with patch("src.services.prediction_service._get_predictions_file", return_value=p):
                svc = PredictionService(MagicMock())
                svc.mark_channel_stale_pending("Streamer_A")
            result = json.loads(p.read_text())

        self.assertEqual(result[0]["result"], "UNKNOWN")


class TestRecordResultOverridesUnknown(unittest.IsolatedAsyncioTestCase):
    async def test_late_result_overrides_unknown_entry(self):
        hist = [{
            "event_id": "evt-9",
            "ts": datetime.now(timezone.utc).isoformat(),
            "channel": "streamer_a",
            "result": "UNKNOWN",
            "outcome_id": "o1",
            "points_bet": 100,
            "points_won": 0,
        }]
        twitch = MagicMock()
        twitch.settings.discord_webhook_points = ""
        twitch.gui._broadcaster.emit = AsyncMock()
        svc = PredictionService(twitch)
        event = {"winning_outcome_id": "o1", "outcomes": [{"id": "o1", "total_points": 100}]}

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "predictions_history.json"
            p.write_text(json.dumps(hist))
            with patch("src.services.prediction_service._get_predictions_file", return_value=p):
                await svc._record_result("evt-9", event, "streamer_a")
            result = json.loads(p.read_text())

        self.assertEqual(result[0]["result"], "WIN")

    async def test_pending_entry_still_resolves_normally(self):
        hist = [{
            "event_id": "evt-10",
            "ts": datetime.now(timezone.utc).isoformat(),
            "channel": "streamer_a",
            "result": "PENDING",
            "outcome_id": "o2",
            "points_bet": 50,
            "points_won": 0,
        }]
        twitch = MagicMock()
        twitch.settings.discord_webhook_points = ""
        twitch.gui._broadcaster.emit = AsyncMock()
        svc = PredictionService(twitch)
        event = {"winning_outcome_id": "o1", "outcomes": [{"id": "o1", "total_points": 100}]}

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "predictions_history.json"
            p.write_text(json.dumps(hist))
            with patch("src.services.prediction_service._get_predictions_file", return_value=p):
                await svc._record_result("evt-10", event, "streamer_a")
            result = json.loads(p.read_text())

        self.assertEqual(result[0]["result"], "LOSE")


from src.services.message_handlers import MessageHandlerService


class TestOnChannelUpdateStalePendingHook(unittest.TestCase):
    def test_channel_going_offline_marks_stale_pending(self):
        twitch = MagicMock()
        twitch.watching_channel.get_with_default.return_value = None
        service = MessageHandlerService(twitch)

        channel = MagicMock()
        channel.name = "streamer_a"
        stream_before = MagicMock()

        service.on_channel_update(channel, stream_before, None)

        twitch._prediction_service.mark_channel_stale_pending.assert_called_once_with("streamer_a")

    def test_channel_staying_online_does_not_mark_stale_pending(self):
        twitch = MagicMock()
        twitch.watching_channel.get_with_default.return_value = None
        twitch.can_watch.return_value = False
        service = MessageHandlerService(twitch)

        channel = MagicMock()
        channel.name = "streamer_a"
        stream = MagicMock()

        service.on_channel_update(channel, stream, stream)

        twitch._prediction_service.mark_channel_stale_pending.assert_not_called()

    def test_channel_coming_online_does_not_mark_stale_pending(self):
        twitch = MagicMock()
        twitch.watching_channel.get_with_default.return_value = None
        twitch.can_watch.return_value = False
        service = MessageHandlerService(twitch)

        channel = MagicMock()
        channel.name = "streamer_a"

        service.on_channel_update(channel, None, MagicMock())

        twitch._prediction_service.mark_channel_stale_pending.assert_not_called()


class TestPredictionsEndpointSweep(unittest.IsolatedAsyncioTestCase):
    async def test_endpoint_sweeps_and_persists_stale_pending(self):
        import src.web.app as app_module

        hist = [{
            "event_id": "evt-1",
            "ts": (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(),
            "channel": "streamer_a",
            "result": "PENDING",
            "outcome_chosen": "Blue",
            "points_bet": 100,
            "points_won": 0,
        }]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "predictions_history.json"
            p.write_text(json.dumps(hist))
            with patch("src.services.prediction_service._get_predictions_file", return_value=p):
                result = await app_module.get_predictions()
            persisted = json.loads(p.read_text())

        self.assertEqual(result["predictions"][0]["result"], "UNKNOWN")
        self.assertEqual(persisted[0]["result"], "UNKNOWN")

    async def test_endpoint_leaves_fresh_pending_alone(self):
        import src.web.app as app_module

        hist = [{
            "event_id": "evt-2",
            "ts": datetime.now(timezone.utc).isoformat(),
            "channel": "streamer_a",
            "result": "PENDING",
            "outcome_chosen": "Red",
            "points_bet": 50,
            "points_won": 0,
        }]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "predictions_history.json"
            p.write_text(json.dumps(hist))
            with patch("src.services.prediction_service._get_predictions_file", return_value=p):
                result = await app_module.get_predictions()

        self.assertEqual(result["predictions"][0]["result"], "PENDING")


if __name__ == "__main__":
    unittest.main()
