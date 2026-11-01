"""Optional reward-type ordering shared by drop selection and the wanted queue.

Off by default. When enabled, among drops that can currently be earned the
preferred reward types (default: game items > badges > emotes > other) come
first and the earlier deadline breaks ties. Game priority itself (selected
games first, then fallback games) is decided elsewhere and is not touched.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from src.models.campaign import DropsCampaign
    from src.models.drop import TimedDrop


DEFAULT_REWARD_ORDER: tuple[str, ...] = ("DIRECT_ENTITLEMENT", "BADGE", "EMOTE", "UNKNOWN")


class RewardPriority:
    TYPES = DEFAULT_REWARD_ORDER

    @classmethod
    def normalize(cls, value: Any) -> list[str]:
        """Return a valid ordering; an invalid value falls back to the default order."""
        if (
            isinstance(value, list)
            and len(value) == len(cls.TYPES)
            and all(isinstance(item, str) for item in value)
            and set(value) == set(cls.TYPES)
        ):
            return list(value)
        return list(cls.TYPES)

    @classmethod
    def is_valid(cls, value: Any) -> bool:
        return isinstance(value, list) and cls.normalize(value) == value

    def __init__(self, settings: Any):
        self.enabled: bool = getattr(settings, "reward_priority_enabled", False) is True
        self.order: list[str] = self.normalize(getattr(settings, "reward_priority", None))
        self.allowed: dict[str, bool] = getattr(settings, "mining_benefits", {})

    def type_rank(self, type_name: str) -> int:
        return self.order.index(type_name) if type_name in self.order else len(self.order)

    def drop_key(self, drop: TimedDrop) -> tuple[int, float]:
        ranks = [
            self.type_rank(b.type.name) for b in drop.benefits if b.is_wanted(self.allowed)
        ]
        return (min(ranks, default=len(self.order)), drop.ends_at.timestamp())

    def campaign_key(self, campaign: DropsCampaign) -> tuple[int, float]:
        now = datetime.now(timezone.utc)
        return min(
            (
                self.drop_key(drop)
                for drop in campaign.drops
                if not drop.is_claimed and drop.starts_at <= now < drop.ends_at and drop.can_earn()
            ),
            default=(len(self.order), float("inf")),
        )
