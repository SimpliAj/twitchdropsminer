import unittest
from unittest.mock import AsyncMock, MagicMock

from src.config.settings import Settings, default_settings
from src.models.campaign import DropsCampaign
from src.models.drop import TimedDrop
from src.models.game import Game
from src.services.stream_selector import StreamSelector
from src.web.app import SettingsUpdate
from src.web.managers.settings import SettingsManager


class TestBlacklistedDropIdsDefault(unittest.TestCase):
    def test_default_is_empty_list(self):
        self.assertEqual(default_settings["blacklisted_drop_ids"], [])

    def test_settings_dataclass_declares_field(self):
        self.assertIn("blacklisted_drop_ids", Settings.__annotations__)


def _make_drop(drop_id, name, benefit_wanted=True):
    drop = MagicMock(spec=TimedDrop)
    drop.id = drop_id
    drop.name = name
    drop.is_claimed = False
    drop.required_minutes = 30
    drop._base_can_earn.return_value = True
    benefit = MagicMock()
    benefit.name = f"{name} benefit"
    benefit.image_url = "http://img"
    benefit.is_wanted.return_value = benefit_wanted
    drop.benefits = [benefit]
    return drop


def _make_campaign(campaign_id, game_name, drops):
    campaign = MagicMock(spec=DropsCampaign)
    campaign.id = campaign_id
    campaign.name = f"Campaign {campaign_id}"
    campaign.campaign_url = f"http://camp/{campaign_id}"
    campaign.game = Game({"id": 1, "name": game_name, "boxArtURL": "http://box"})
    campaign.can_earn_within.return_value = True
    campaign.drops = drops
    return campaign


class TestBlacklistedDropIds(unittest.TestCase):
    def setUp(self):
        self.selector = StreamSelector()
        self.settings = MagicMock()
        self.settings.games_to_watch = ["Game1"]
        self.settings.preferred_games = []
        self.settings.mining_benefits = {"BADGE": True}
        self.settings.drop_name_blacklist = []
        self.settings.blacklisted_drop_ids = []

    def test_drop_with_blacklisted_id_is_excluded(self):
        good = _make_drop("drop_good", "Good Drop")
        bad = _make_drop("drop_bad", "Bad Looping Drop")
        campaign = _make_campaign("c1", "Game1", [good, bad])
        self.settings.blacklisted_drop_ids = ["drop_bad"]

        tree = self.selector.get_wanted_game_tree(self.settings, [campaign])

        self.assertEqual(len(tree), 1)
        drop_names = [d["name"] for d in tree[0]["campaigns"][0]["drops"]]
        self.assertEqual(drop_names, ["Good Drop"])

    def test_other_drops_in_same_campaign_still_mined(self):
        d1 = _make_drop("drop_1", "Drop One")
        d2 = _make_drop("drop_2", "Drop Two")
        d3 = _make_drop("drop_3", "Drop Three")
        campaign = _make_campaign("c1", "Game1", [d1, d2, d3])
        self.settings.blacklisted_drop_ids = ["drop_2"]

        tree = self.selector.get_wanted_game_tree(self.settings, [campaign])

        drop_names = {d["name"] for d in tree[0]["campaigns"][0]["drops"]}
        self.assertEqual(drop_names, {"Drop One", "Drop Three"})

    def test_empty_blacklist_excludes_nothing(self):
        d1 = _make_drop("drop_1", "Drop One")
        campaign = _make_campaign("c1", "Game1", [d1])
        self.settings.blacklisted_drop_ids = []

        tree = self.selector.get_wanted_game_tree(self.settings, [campaign])

        self.assertEqual(len(tree[0]["campaigns"][0]["drops"]), 1)

    def test_name_blacklist_and_id_blacklist_both_apply(self):
        by_keyword = _make_drop("drop_kw", "JP Exclusive Drop")
        by_id = _make_drop("drop_id", "Normal Drop")
        keep = _make_drop("drop_keep", "Another Drop")
        campaign = _make_campaign("c1", "Game1", [by_keyword, by_id, keep])
        self.settings.drop_name_blacklist = ["JP"]
        self.settings.blacklisted_drop_ids = ["drop_id"]

        tree = self.selector.get_wanted_game_tree(self.settings, [campaign])

        drop_names = {d["name"] for d in tree[0]["campaigns"][0]["drops"]}
        self.assertEqual(drop_names, {"Another Drop"})


class TestBlacklistedDropIdsSettingsWiring(unittest.TestCase):
    def test_settings_update_model_accepts_blacklisted_drop_ids(self):
        model = SettingsUpdate(blacklisted_drop_ids=["drop_1", "drop_2"])
        self.assertEqual(model.blacklisted_drop_ids, ["drop_1", "drop_2"])

    def test_settings_manager_persists_blacklisted_drop_ids(self):
        mock_broadcaster = MagicMock()
        mock_broadcaster.emit = AsyncMock()
        mock_settings = MagicMock()
        mock_console = MagicMock()

        with unittest.mock.patch("asyncio.create_task"):
            manager = SettingsManager(mock_broadcaster, mock_settings, mock_console)
            manager.update_settings({"blacklisted_drop_ids": ["drop_bad"]})

        self.assertEqual(mock_settings.blacklisted_drop_ids, ["drop_bad"])
        mock_console.print.assert_any_call(
            "Setting changed: blacklisted_drop_ids = ['drop_bad']"
        )


if __name__ == "__main__":
    unittest.main()
