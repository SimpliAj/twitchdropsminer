from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from src.services.stream_selector import _deadline


def _campaign(window_end=None, days=10):
    return SimpleNamespace(ends_at=datetime.now(timezone.utc) + timedelta(days=days), window_end=window_end)


def test_open_daily_window_is_the_deadline():
    window = datetime.now(timezone.utc) + timedelta(hours=5)
    c = _campaign(window.strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert abs((_deadline(c) - window).total_seconds()) < 1


def test_normal_campaign_uses_its_end():
    c = _campaign()
    assert _deadline(c) == c.ends_at
    c = SimpleNamespace(ends_at=c.ends_at)  # plain DropsCampaign has no window_end
    assert _deadline(c) == c.ends_at


def test_past_or_late_window_is_ignored():
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    c = _campaign(past)
    assert _deadline(c) == c.ends_at
    late = (datetime.now(timezone.utc) + timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    c = _campaign(late)
    assert _deadline(c) == c.ends_at
