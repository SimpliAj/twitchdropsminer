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
