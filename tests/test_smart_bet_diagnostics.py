import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.services.prediction_service import PredictionService


def _outcome(outcome_id, title, total_users, total_points=None):
    return {
        "id": outcome_id,
        "title": title,
        "total_users": total_users,
        "total_points": total_points if total_points is not None else total_users * 100,
    }


class TestChooseOutcomeSmartDiagnostics(unittest.TestCase):
    def setUp(self):
        self.svc = PredictionService(MagicMock())

    def test_smart_bets_when_gap_exceeds_threshold(self):
        outcomes = [_outcome("a", "Blue", 80), _outcome("b", "Red", 20)]
        cfg = {"bet_strategy": "SMART", "bet_percentage_gap": 20}

        chosen, diag = self.svc._choose_outcome(outcomes, cfg, channel_name="teststreamer")

        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["id"], "a")
        self.assertAlmostEqual(diag["top_pct"], 80.0)
        self.assertAlmostEqual(diag["second_pct"], 20.0)
        self.assertAlmostEqual(diag["gap"], 60.0)

    def test_smart_skips_when_gap_below_threshold(self):
        outcomes = [_outcome("a", "Blue", 55), _outcome("b", "Red", 45)]
        cfg = {"bet_strategy": "SMART", "bet_percentage_gap": 20}

        chosen, diag = self.svc._choose_outcome(outcomes, cfg, channel_name="teststreamer")

        self.assertIsNone(chosen)
        self.assertIsNotNone(diag)
        self.assertAlmostEqual(diag["gap"], 10.0)

    def test_smart_diag_none_for_other_strategies(self):
        outcomes = [_outcome("a", "Blue", 80), _outcome("b", "Red", 20)]
        cfg = {"bet_strategy": "MOST_VOTED", "bet_percentage_gap": 20}

        chosen, diag = self.svc._choose_outcome(outcomes, cfg, channel_name="teststreamer")

        self.assertEqual(chosen["id"], "a")
        self.assertIsNone(diag)

    def test_logs_bet_and_skip_decisions(self):
        outcomes_bet = [_outcome("a", "Blue", 80), _outcome("b", "Red", 20)]
        outcomes_skip = [_outcome("a", "Blue", 55), _outcome("b", "Red", 45)]
        cfg = {"bet_strategy": "SMART", "bet_percentage_gap": 20}

        with self.assertLogs("TwitchDrops", level="INFO") as cm:
            self.svc._choose_outcome(outcomes_bet, cfg, channel_name="chan_a")
        self.assertTrue(any("BET on" in line for line in cm.output))

        with self.assertLogs("TwitchDrops", level="INFO") as cm:
            self.svc._choose_outcome(outcomes_skip, cfg, channel_name="chan_a")
        self.assertTrue(any("SKIPPED" in line for line in cm.output))


class TestSavePendingBetPersistsDiagnostics(unittest.TestCase):
    def test_smart_diag_persisted_on_history_entry(self):
        svc = PredictionService(MagicMock())
        event = {"title": "Who wins?"}
        outcome = {"id": "o1", "title": "Blue"}
        smart_diag = {"top_pct": 70.0, "second_pct": 30.0, "gap": 40.0}

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "predictions_history.json"
            with patch("src.services.prediction_service._get_predictions_file", return_value=p):
                svc._save_pending_bet(
                    "evt-1", "streamer_a", event, outcome, 100, "SMART", smart_diag
                )
            hist = json.loads(p.read_text())

        self.assertEqual(hist[0]["top_pct"], 70.0)
        self.assertEqual(hist[0]["second_pct"], 30.0)
        self.assertEqual(hist[0]["gap"], 40.0)

    def test_no_diag_fields_when_smart_diag_omitted(self):
        svc = PredictionService(MagicMock())
        event = {"title": "Who wins?"}
        outcome = {"id": "o1", "title": "Blue"}

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "predictions_history.json"
            with patch("src.services.prediction_service._get_predictions_file", return_value=p):
                svc._save_pending_bet("evt-1", "streamer_a", event, outcome, 100, "PERCENTAGE")
            hist = json.loads(p.read_text())

        self.assertNotIn("top_pct", hist[0])
        self.assertNotIn("second_pct", hist[0])
        self.assertNotIn("gap", hist[0])


if __name__ == "__main__":
    unittest.main()
