"""Helpers for timed pauses (10 minutes, 1 hour, until local midnight)."""
from __future__ import annotations

from datetime import datetime, timedelta


PAUSE_DURATIONS: tuple[str, ...] = ("10m", "1h", "midnight", "indefinite")


def pause_deadline(duration: str, now: datetime | None = None) -> datetime | None:
    """Return the local wall-clock moment a pause of `duration` ends, or None for 'until resumed'."""
    now = now or datetime.now().astimezone()
    if duration == "10m":
        return now + timedelta(minutes=10)
    if duration == "1h":
        return now + timedelta(hours=1)
    if duration == "midnight":
        tomorrow = (now + timedelta(days=1)).date()
        return datetime.combine(tomorrow, datetime.min.time(), tzinfo=now.tzinfo) if now.tzinfo else (
            datetime.combine(tomorrow, datetime.min.time())
        )
    if duration == "indefinite":
        return None
    raise ValueError(f"Unknown pause duration: {duration}")


def seconds_until(deadline: datetime, now: datetime | None = None) -> float:
    now = now or datetime.now().astimezone()
    return max(0.0, (deadline - now).total_seconds())
