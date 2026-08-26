"""
The "Wanted Drop Queue" (Mining Benefits) list used to be ordered only by the
games_to_watch order or by end date. oNeVPlus requested a combined sort:
group games by benefit category first (badge, emote, other, item), then within
each category keep the existing order (manual games_to_watch order, or
end-date order if the user sorted games_to_watch that way).
"""
import unittest
from unittest.mock import MagicMock

from src.models.benefit import Benefit
from src.models.campaign import DropsCampaign
from src.models.drop import TimedDrop
from src.models.game import Game
from src.services.stream_selector import StreamSelector


def _make_benefit(distribution_type: str, name: str = "Benefit") -> Benefit:
    return Benefit({
        "benefit": {
            "id": name,
            "name": name,
            "distributionType": distribution_type,
            "imageAssetURL": "http://img",
        }
    })


def _make_drop(name: str, benefit_types: list[str]) -> MagicMock:
    drop = MagicMock(spec=TimedDrop)
    drop.id = name
    drop.name = name
    drop.is_claimed = False
    drop.required_minutes = 30
    drop._base_can_earn.return_value = True
    drop.benefits = [_make_benefit(t, f"{name}-{t}") for t in benefit_types]
    return drop


def _make_campaign(campaign_id: str, game_name: str, benefit_types: list[str]) -> MagicMock:
    campaign = MagicMock(spec=DropsCampaign)
    campaign.id = campaign_id
    campaign.name = f"Campaign {campaign_id}"
    campaign.campaign_url = f"http://camp/{campaign_id}"
    campaign.game = Game({"id": hash(game_name) % 1000, "name": game_name, "boxArtURL": "http://box"})
    campaign.can_earn_within.return_value = True
    campaign.drops = [_make_drop(f"{campaign_id}-drop", benefit_types)]
    return campaign


class TestWantedTreeBenefitCategorySort(unittest.TestCase):
    def setUp(self):
        self.selector = StreamSelector()
        self.settings = MagicMock()
        self.settings.preferred_games = []
        self.settings.drop_name_blacklist = []
        self.settings.blacklisted_drop_ids = []
        self.settings.mining_benefits = {
            "BADGE": True, "EMOTE": True, "UNKNOWN": True, "DIRECT_ENTITLEMENT": True,
        }

    def test_groups_by_category_badge_emote_other_item(self):
        # games_to_watch order is A, B, C — but category grouping should
        # reorder to Badge(B), Emote(A), Other(D), Item(C).
        self.settings.games_to_watch = ["GameA", "GameB", "GameC", "GameD"]
        campaigns = [
            _make_campaign("c1", "GameA", ["EMOTE"]),
            _make_campaign("c2", "GameB", ["BADGE"]),
            _make_campaign("c3", "GameC", ["DIRECT_ENTITLEMENT"]),
            _make_campaign("c4", "GameD", ["UNKNOWN"]),
        ]

        result = self.selector.get_wanted_game_tree(self.settings, campaigns)

        self.assertEqual(
            [g["game_name"] for g in result],
            ["GameB", "GameA", "GameD", "GameC"],
        )

    def test_preserves_games_to_watch_order_within_same_category(self):
        # Both games have BADGE drops; games_to_watch order (B before A) must
        # be preserved as the secondary sort key.
        self.settings.games_to_watch = ["GameB", "GameA"]
        campaigns = [
            _make_campaign("c1", "GameA", ["BADGE"]),
            _make_campaign("c2", "GameB", ["BADGE"]),
        ]

        result = self.selector.get_wanted_game_tree(self.settings, campaigns)

        self.assertEqual([g["game_name"] for g in result], ["GameB", "GameA"])

    def test_game_with_multiple_benefit_types_uses_best_category(self):
        # A game with both an EMOTE and a BADGE drop should be grouped under
        # badge (the higher-priority category), not emote.
        self.settings.games_to_watch = ["GameA", "GameB"]
        campaign_a = _make_campaign("c1", "GameA", ["EMOTE"])
        campaign_a.drops.append(_make_drop("c1-drop2", ["BADGE"]))
        campaigns = [
            campaign_a,
            _make_campaign("c2", "GameB", ["BADGE"]),
        ]

        result = self.selector.get_wanted_game_tree(self.settings, campaigns)

        # Both are now "badge" category, so games_to_watch order (A before B)
        # is preserved rather than emote/badge splitting them apart.
        self.assertEqual([g["game_name"] for g in result], ["GameA", "GameB"])

    def test_benefit_dict_includes_type_field(self):
        self.settings.games_to_watch = ["GameA"]
        campaigns = [_make_campaign("c1", "GameA", ["BADGE"])]

        result = self.selector.get_wanted_game_tree(self.settings, campaigns)

        benefit = result[0]["campaigns"][0]["drops"][0]["benefits"][0]
        self.assertEqual(benefit["type"], "BADGE")


if __name__ == "__main__":
    unittest.main()
