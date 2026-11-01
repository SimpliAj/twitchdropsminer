import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.config.reward_priority import RewardPriority
from src.models.campaign import DropsCampaign
from tests.test_wanted_tree_benefit_category_sort import _make_benefit, _make_campaign
from src.services.stream_selector import StreamSelector

ALL = {"BADGE": True, "EMOTE": True, "UNKNOWN": True, "DIRECT_ENTITLEMENT": True}
NOW = datetime.now(timezone.utc)


def _settings(enabled=True, order=None, benefits=None):
    return SimpleNamespace(
        reward_priority_enabled=enabled,
        reward_priority=order or ["DIRECT_ENTITLEMENT", "BADGE", "EMOTE", "UNKNOWN"],
        mining_benefits=benefits or ALL,
    )


def _drop(name, types, ends_in_h, remaining=10, earnable=True, claimed=False):
    d = MagicMock()
    d.id = name
    d.benefits = [_make_benefit(t, f"{name}-{t}") for t in types]
    d.ends_at = NOW + timedelta(hours=ends_in_h)
    d.starts_at = NOW - timedelta(hours=1)
    d.can_earn.return_value = earnable
    d.is_claimed = claimed
    d.remaining_minutes = remaining
    d.excluded = False
    return d


class TestRewardPriority(unittest.TestCase):
    def test_normalize(self):
        default = list(RewardPriority.TYPES)
        self.assertEqual(RewardPriority.normalize(None), default)
        self.assertEqual(RewardPriority.normalize(["BADGE"]), default)
        self.assertEqual(RewardPriority.normalize(["BADGE", "BADGE", "EMOTE", "UNKNOWN"]), default)
        custom = ["EMOTE", "BADGE", "UNKNOWN", "DIRECT_ENTITLEMENT"]
        self.assertEqual(RewardPriority.normalize(custom), custom)
        self.assertTrue(RewardPriority.is_valid(custom))
        self.assertFalse(RewardPriority.is_valid(["x"]))

    def test_disabled_by_default(self):
        self.assertFalse(RewardPriority(SimpleNamespace()).enabled)
        self.assertFalse(RewardPriority(_settings(enabled=False)).enabled)
        self.assertFalse(RewardPriority(MagicMock()).enabled)

    def test_type_order_then_deadline(self):
        rp = RewardPriority(_settings())
        emote = _drop("e", ["EMOTE"], 1)
        badge_late = _drop("b2", ["BADGE"], 10)
        badge_early = _drop("b1", ["BADGE"], 5)
        item = _drop("i", ["DIRECT_ENTITLEMENT"], 50)
        ordered = sorted([emote, badge_late, badge_early, item], key=rp.drop_key)
        self.assertEqual([d.id for d in ordered], ["i", "b1", "b2", "e"])

    def test_disallowed_benefit_ignored(self):
        rp = RewardPriority(_settings(benefits={**ALL, "DIRECT_ENTITLEMENT": False}))
        both = _drop("x", ["DIRECT_ENTITLEMENT", "EMOTE"], 1)
        self.assertEqual(rp.drop_key(both)[0], rp.order.index("EMOTE"))

    def test_custom_order(self):
        rp = RewardPriority(_settings(order=["EMOTE", "BADGE", "UNKNOWN", "DIRECT_ENTITLEMENT"]))
        ordered = sorted([_drop("i", ["DIRECT_ENTITLEMENT"], 1), _drop("e", ["EMOTE"], 9)], key=rp.drop_key)
        self.assertEqual([d.id for d in ordered], ["e", "i"])

    def test_campaign_key_only_considers_earnable_unclaimed(self):
        rp = RewardPriority(_settings())
        camp = MagicMock()
        camp.drops = [
            _drop("i", ["DIRECT_ENTITLEMENT"], 5, earnable=False),
            _drop("c", ["DIRECT_ENTITLEMENT"], 5, claimed=True),
            _drop("e", ["EMOTE"], 5),
        ]
        self.assertEqual(rp.campaign_key(camp)[0], rp.order.index("EMOTE"))
        camp.drops = []
        self.assertEqual(rp.campaign_key(camp)[0], len(rp.order))

    def test_first_drop_uses_priority_only_when_enabled(self):
        for enabled, expected in ((True, "item"), (False, "emote")):
            camp = MagicMock()
            camp._twitch.settings = _settings(enabled=enabled)
            camp.drops = [
                _drop("emote", ["EMOTE"], 5, remaining=5),
                _drop("item", ["DIRECT_ENTITLEMENT"], 5, remaining=50),
            ]
            self.assertEqual(DropsCampaign.first_drop.fget(camp).id, expected)


class TestWantedTreeWithRewardPriority(unittest.TestCase):
    def _tree(self, **kw):
        s = _settings(**kw)
        s.games_to_watch = ["GameA", "GameB"]
        s.preferred_games = []
        s.drop_name_blacklist = []
        s.blacklisted_drop_ids = []
        s.priority_list_only = False
        campaigns = [
            _make_campaign("c1", "GameA", ["DIRECT_ENTITLEMENT"]),
            _make_campaign("c2", "GameB", ["BADGE"]),
            _make_campaign("c3", "Fallback", ["BADGE"]),
        ]
        for c in campaigns:
            c.ends_at = NOW + timedelta(hours=3)
        return [g["game_name"] for g in StreamSelector().get_wanted_game_tree(s, campaigns)]

    def test_off_keeps_builtin_badge_first(self):
        self.assertEqual(self._tree(enabled=False), ["GameB", "GameA", "Fallback"])

    def test_on_item_first_and_fallback_stays_last(self):
        self.assertEqual(self._tree(enabled=True), ["GameA", "GameB", "Fallback"])
