import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from src.config.settings import Settings, default_settings
from src.models.campaign import DropsCampaign
from src.services.stream_selector import StreamSelector
from src.web.app import SettingsUpdate
from src.web.managers.settings import SettingsManager


def _campaign_data(campaign_id, game_name="Test Game", with_drop=True):
    now = datetime.now(timezone.utc)
    data = {
        "id": campaign_id,
        "name": f"Campaign {campaign_id}",
        "game": {
            "id": "1",
            "name": game_name,
            "displayName": game_name,
            "boxArtURL": "https://example.test/game-{width}x{height}.jpg",
        },
        "self": {"isAccountConnected": True},
        "accountLinkURL": "https://example.test/link",
        "startAt": (now - timedelta(hours=1)).isoformat(),
        "endAt": (now + timedelta(days=1)).isoformat(),
        "status": "ACTIVE",
        "allow": {"channels": [], "isEnabled": True},
        "timeBasedDrops": [],
    }
    if with_drop:
        data["timeBasedDrops"] = [
            {
                "id": f"{campaign_id}-drop-1",
                "name": "Drop One",
                "benefitEdges": [
                    {
                        "benefit": {
                            "id": f"{campaign_id}-benefit-1",
                            "name": "Badge",
                            "distributionType": "BADGE",
                            "imageAssetURL": "http://img",
                        }
                    }
                ],
                "startAt": (now - timedelta(hours=1)).isoformat(),
                "endAt": (now + timedelta(days=1)).isoformat(),
                "requiredMinutesWatched": 30,
                "preconditionDrops": [],
            }
        ]
    return data


def _make_real_campaign(campaign_id, game_name="Test Game", ignored_ids=None):
    twitch = MagicMock()
    twitch.settings.ignored_campaign_ids = ignored_ids or []
    return DropsCampaign(twitch, _campaign_data(campaign_id, game_name), {})


class TestIgnoredCampaignsDefault(unittest.TestCase):
    def test_default_is_empty_list(self):
        self.assertEqual(default_settings["ignored_campaign_ids"], [])

    def test_settings_dataclass_declares_field(self):
        self.assertIn("ignored_campaign_ids", Settings.__annotations__)


class TestDropsCampaignIgnoredProperty(unittest.TestCase):
    def test_not_ignored_by_default(self):
        campaign = _make_real_campaign("camp-1")
        self.assertFalse(campaign.ignored)
        self.assertTrue(campaign.can_earn())
        self.assertTrue(campaign.can_earn_within(datetime.now(timezone.utc) + timedelta(hours=1)))

    def test_ignored_campaign_cannot_earn(self):
        campaign = _make_real_campaign("camp-1", ignored_ids=["camp-1"])
        self.assertTrue(campaign.ignored)
        self.assertFalse(campaign.can_earn())
        self.assertFalse(campaign.can_earn_within(datetime.now(timezone.utc) + timedelta(hours=1)))

    def test_only_the_ignored_campaign_id_is_affected(self):
        other = _make_real_campaign("camp-2", ignored_ids=["camp-1"])
        self.assertFalse(other.ignored)
        self.assertTrue(other.can_earn())

    def test_missing_settings_attribute_defaults_to_not_ignored(self):
        # A settings stand-in without ignored_campaign_ids at all shouldn't blow up.
        twitch = MagicMock()
        del twitch.settings.ignored_campaign_ids
        campaign = DropsCampaign(twitch, _campaign_data("camp-1"), {})
        self.assertFalse(campaign.ignored)


class TestStreamSelectorRespectsIgnoredCampaigns(unittest.TestCase):
    def test_ignored_campaign_excluded_but_game_with_other_campaign_stays(self):
        ignored = _make_real_campaign("camp-ignored", "Shared Game", ignored_ids=["camp-ignored"])
        kept = _make_real_campaign("camp-kept", "Shared Game", ignored_ids=["camp-ignored"])

        settings = MagicMock()
        settings.games_to_watch = ["Shared Game"]
        settings.preferred_games = []
        settings.mining_benefits = {"BADGE": True}
        settings.drop_name_blacklist = []
        settings.blacklisted_drop_ids = []

        tree = StreamSelector().get_wanted_game_tree(settings, [ignored, kept])

        self.assertEqual(len(tree), 1)
        campaign_ids = {c["id"] for c in tree[0]["campaigns"]}
        self.assertEqual(campaign_ids, {"camp-kept"})

    def test_game_drops_out_entirely_when_its_only_campaign_is_ignored(self):
        ignored = _make_real_campaign("camp-1", "Solo Game", ignored_ids=["camp-1"])

        settings = MagicMock()
        settings.games_to_watch = ["Solo Game"]
        settings.preferred_games = []
        settings.mining_benefits = {"BADGE": True}
        settings.drop_name_blacklist = []
        settings.blacklisted_drop_ids = []

        tree = StreamSelector().get_wanted_game_tree(settings, [ignored])

        self.assertEqual(tree, [])


class TestIgnoredCampaignIdsSettingsWiring(unittest.TestCase):
    def test_settings_update_model_accepts_ignored_campaign_ids(self):
        model = SettingsUpdate(ignored_campaign_ids=["camp-1", "camp-2"])
        self.assertEqual(model.ignored_campaign_ids, ["camp-1", "camp-2"])

    def test_settings_manager_persists_ignored_campaign_ids(self):
        mock_broadcaster = MagicMock()
        mock_broadcaster.emit = AsyncMock()
        mock_settings = MagicMock()
        mock_console = MagicMock()

        with unittest.mock.patch("asyncio.create_task"):
            manager = SettingsManager(mock_broadcaster, mock_settings, mock_console)
            manager.update_settings({"ignored_campaign_ids": ["camp-1"]})

        self.assertEqual(mock_settings.ignored_campaign_ids, ["camp-1"])
        mock_console.print.assert_any_call(
            "Setting changed: ignored_campaign_ids = ['camp-1']"
        )


if __name__ == "__main__":
    unittest.main()
