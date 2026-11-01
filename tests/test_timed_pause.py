import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.client import Twitch
from src.services.pause_timer import pause_deadline, seconds_until


NOW = datetime(2026, 11, 1, 22, 30, tzinfo=timezone(timedelta(hours=1)))


def test_deadlines():
    assert pause_deadline("10m", NOW) == NOW + timedelta(minutes=10)
    assert pause_deadline("1h", NOW) == NOW + timedelta(hours=1)
    assert pause_deadline("indefinite", NOW) is None
    assert pause_deadline("midnight", NOW) == datetime(2026, 11, 2, 0, 0, tzinfo=NOW.tzinfo)


def test_midnight_just_after_midnight_is_next_day():
    now = datetime(2026, 11, 2, 0, 0, 5, tzinfo=NOW.tzinfo)
    assert pause_deadline("midnight", now) == datetime(2026, 11, 3, 0, 0, tzinfo=NOW.tzinfo)


def test_invalid_duration():
    with pytest.raises(ValueError):
        pause_deadline("2h", NOW)


def test_seconds_until_never_negative():
    assert seconds_until(NOW - timedelta(seconds=5), NOW) == 0.0
    assert seconds_until(NOW + timedelta(seconds=5), NOW) == 5.0


def _fake_twitch():
    t = SimpleNamespace(
        _is_paused=False, _pause_source=None, _pause_until=None, _pause_timer=None,
        _user_override=False, gui=MagicMock(), change_state=MagicMock(),
        _scheduler_service=MagicMock(),
    )
    t.gui.broadcast_pause_state = AsyncMock()
    for name in ("pause", "resume", "is_paused", "pause_until", "_cancel_pause_timer", "_pause_timer_run"):
        setattr(t, name, getattr(Twitch, name).__get__(t))
    return t


def test_timed_pause_auto_resumes_and_hands_back_to_scheduler():
    asyncio.run(_test_timed_pause_auto_resumes_and_hands_back_to_scheduler())


async def _test_timed_pause_auto_resumes_and_hands_back_to_scheduler():
    t = _fake_twitch()
    t.pause("user", until=datetime.now().astimezone() + timedelta(milliseconds=50))
    assert t.is_paused() and t.pause_until() is not None and t._user_override
    await asyncio.sleep(0.2)
    assert not t.is_paused() and t.pause_until() is None
    assert t._user_override is False
    t._scheduler_service.trigger_check.assert_called_once()


def test_manual_resume_cancels_timer():
    asyncio.run(_test_manual_resume_cancels_timer())


async def _test_manual_resume_cancels_timer():
    t = _fake_twitch()
    t.pause("user", until=datetime.now().astimezone() + timedelta(milliseconds=50))
    t.resume(user_override=True)
    await asyncio.sleep(0.15)
    assert t._user_override is True  # timer did not fire and reset it
    t._scheduler_service.trigger_check.assert_not_called()


def test_indefinite_pause_has_no_deadline():
    asyncio.run(_test_indefinite_pause_has_no_deadline())


async def _test_indefinite_pause_has_no_deadline():
    t = _fake_twitch()
    t.pause("user")
    assert t.is_paused() and t.pause_until() is None and t._pause_timer is None


def test_repause_replaces_timer():
    asyncio.run(_test_repause_replaces_timer())


async def _test_repause_replaces_timer():
    t = _fake_twitch()
    t.pause("user", until=datetime.now().astimezone() + timedelta(milliseconds=50))
    t.pause("user")  # switch to until-resumed
    await asyncio.sleep(0.15)
    assert t.is_paused()
