from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from src.services.prediction_service import PredictionService


def _svc():
    return PredictionService(MagicMock())


def _cfg(strategy="SMART", gap=20, delay=30, pct=5, mx=5000, mn=1000):
    return {
        "bet_strategy": strategy, "bet_percentage_gap": gap, "bet_delay_seconds": delay,
        "bet_percentage": pct, "bet_max_points": mx, "bet_minimum_points": mn,
    }


def _event(window=120, age=0):
    created = datetime.now(timezone.utc) - timedelta(seconds=age)
    return {"prediction_window_seconds": window, "created_at": created.isoformat()}


def _o(i, pts, users):
    return {"id": i, "title": i, "total_points": pts, "total_users": users}


def test_delay_is_counted_back_from_lock():
    assert 89 < _svc()._effective_delay(_event(window=120), _cfg(delay=30)) <= 90


def test_delay_accounts_for_elapsed_time():
    assert 59 < _svc()._effective_delay(_event(window=120, age=30), _cfg(delay=30)) <= 60


def test_delay_never_later_than_3s_before_lock():
    d = _svc()._effective_delay(_event(window=60), _cfg(delay=0))
    assert 56 < d <= 57


def test_delay_short_window_bets_immediately():
    assert _svc()._effective_delay(_event(window=20), _cfg(delay=30)) == 0.5


def test_delay_missing_fields_falls_back_to_configured():
    assert _svc()._effective_delay({}, _cfg(delay=25)) == 25.0
    assert _svc()._effective_delay({"prediction_window_seconds": 60, "created_at": "bad"}, _cfg(delay=25)) == 25.0


def test_most_voted_picks_users_and_skips_empty():
    s = _svc()
    assert s._choose_outcome([_o("a", 900, 1), _o("b", 10, 5)], _cfg("MOST_VOTED"))[0]["id"] == "b"
    assert s._choose_outcome([_o("a", 0, 0), _o("b", 0, 0)], _cfg("MOST_VOTED"))[0] is None


def test_percentage_picks_points_and_skips_empty():
    s = _svc()
    assert s._choose_outcome([_o("a", 900, 1), _o("b", 10, 5)], _cfg("PERCENTAGE"))[0]["id"] == "a"
    assert s._choose_outcome([_o("a", 0, 0), _o("b", 0, 0)], _cfg("PERCENTAGE"))[0] is None


def test_high_odds_ignores_empty_outcome():
    s = _svc()
    assert s._choose_outcome([_o("a", 900, 3), _o("b", 100, 2), _o("c", 0, 0)], _cfg("HIGH_ODDS"))[0]["id"] == "b"
    assert s._choose_outcome([_o("a", 0, 0), _o("b", 0, 0)], _cfg("HIGH_ODDS"))[0] is None


def test_smart_bets_favourite_only_above_gap():
    s = _svc()
    assert s._choose_outcome([_o("a", 1, 70), _o("b", 1, 30)], _cfg(gap=20))[0]["id"] == "a"
    chosen, diag = s._choose_outcome([_o("a", 1, 55), _o("b", 1, 45)], _cfg(gap=20))
    assert chosen is None and diag["gap"] == 10


def test_smart_zero_votes_skips():
    assert _svc()._choose_outcome([_o("a", 0, 0), _o("b", 0, 0)], _cfg())[0] is None
    assert _svc()._choose_outcome([_o("a", 5, 10), _o("b", 0, 0)], _cfg(gap=20))[0]["id"] == "a"


def test_calc_amount_caps_and_balance():
    s = _svc()
    assert s._calc_amount(100_000, _cfg(pct=5, mx=5000)) == 5000
    assert s._calc_amount(10_000, _cfg(pct=5, mx=5000)) == 500
    assert s._calc_amount(50, _cfg(pct=5)) == 10      # Twitch minimum
    assert s._calc_amount(5, _cfg(pct=5)) == 0        # never more than the balance


# ---- results: kept topics, refunds ----
import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

from src.core.client import Twitch


def _hist(tmp_path, *entries):
    f = tmp_path / "predictions_history.json"
    f.write_text(json.dumps(list(entries)))
    return f


def _bet(event_id="e1", channel="chan", result="PENDING"):
    return {"event_id": event_id, "ts": datetime.now(timezone.utc).isoformat(), "channel": channel,
            "title": "t", "outcome_chosen": "A", "outcome_id": "o1", "points_bet": 100,
            "result": result, "points_won": 0}


def _chan(cid=7, name="chan"):
    ch = MagicMock()
    ch.id = cid
    ch.name = name
    ch._login = name
    return ch


def test_kept_topics_cover_open_bets_only_and_expire():
    s = _svc()
    s._awaiting["e1"] = (_chan(7), time.time() + 100)
    s._awaiting["e2"] = (_chan(8), time.time() - 1)
    assert s.kept_prediction_topics() == {"predictions-channel-v1.7"}
    assert "e2" not in s._awaiting


def test_kept_topics_are_capped():
    s = _svc()
    for i in range(20):
        s._awaiting[f"e{i}"] = (_chan(i), time.time() + 100)
    assert len(s.kept_prediction_topics()) == 5


def test_clear_idle_watch_keeps_open_bet_topics():
    tw = Twitch.__new__(Twitch)
    tw.websocket = MagicMock()
    tw._prediction_service = _svc()
    tw._prediction_service._awaiting["e1"] = (_chan(7), time.time() + 100)
    tw._idle_topic_ids = ["predictions-channel-v1.7", "predictions-channel-v1.8"]
    tw._idle_channels_set = set()
    Twitch._clear_idle_watch(tw)
    tw.websocket.remove_topics.assert_called_once_with(["predictions-channel-v1.8"])
    assert tw._idle_topic_ids == ["predictions-channel-v1.7"]


def test_offline_keeps_awaited_bet_pending(tmp_path):
    f = _hist(tmp_path, _bet("e1"), _bet("e2"))
    s = _svc()
    s._awaiting["e1"] = (_chan(), time.time() + 100)
    with patch("src.services.prediction_service._get_predictions_file", return_value=f):
        s.mark_channel_stale_pending("chan")
    assert [e["result"] for e in json.loads(f.read_text())] == ["PENDING", "UNKNOWN"]


def test_result_arrives_after_channel_was_left(tmp_path):
    f = _hist(tmp_path, _bet("e1"))
    s = _svc()
    s._twitch.settings.make_predictions = True
    s._twitch.settings.prediction_channels = []
    s._twitch.channels = {}
    s._twitch._idle_channels_set = set()
    s._twitch.gui._broadcaster.emit = AsyncMock()
    s._twitch.settings.discord_webhook_points = ""
    s._awaiting["e1"] = (_chan(7), time.time() + 100)
    msg = {"type": "event-updated", "data": {"event": {
        "id": "e1", "status": "RESOLVED", "winning_outcome_id": "o1",
        "outcomes": [_o("o1", 100, 1), _o("o2", 100, 1)]}}}
    with patch("src.services.prediction_service._get_predictions_file", return_value=f), \
         patch.object(s, "_get_channel_settings", return_value={"make_predictions": True}):
        asyncio.run(s.process_prediction(7, msg))
    e = json.loads(f.read_text())[0]
    assert (e["result"], e["points_won"]) == ("WIN", 200)
    assert "e1" not in s._awaiting


def test_canceled_prediction_is_refunded(tmp_path):
    f = _hist(tmp_path, _bet("e1"))
    s = _svc()
    s._twitch.gui._broadcaster.emit = AsyncMock()
    s._twitch.settings.discord_webhook_points = ""
    s._awaiting["e1"] = (_chan(), time.time() + 100)
    with patch("src.services.prediction_service._get_predictions_file", return_value=f):
        asyncio.run(s._record_result("e1", {"status": "CANCELED", "outcomes": []}, "chan"))
    e = json.loads(f.read_text())[0]
    assert (e["result"], e["points_won"]) == ("REFUNDED", 100)
    assert s._twitch.gui._broadcaster.emit.call_args[0][1]["result"] == "REFUNDED"
