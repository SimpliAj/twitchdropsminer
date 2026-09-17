import unittest
from unittest.mock import MagicMock

from src.models.campaign import DropsCampaign
from src.models.game import Game


class TestSpecialEventsChannelEligibility(unittest.TestCase):
    """
    Regression test (ported from upstream rangermix/TwitchDropsMiner #105,
    "Fix Special Events and IRL campaign mining"): a channel that is a live,
    ACL-listed participant of a Special Events/IRL campaign must count as
    eligible even when Channel.game doesn't match the campaign's own game
    (these campaigns intentionally span multiple streamed categories).
    """

    def _campaign(self, game_id):
        # eligible/ignored/active are computed properties on the real class
        # with no setter -- a plain namespace object with the same attribute
        # names exercises _base_can_earn's actual logic without fighting that.
        campaign = MagicMock()
        campaign.eligible = True
        campaign.ignored = False
        campaign.active = True
        campaign.game = Game({"id": game_id, "name": "Special Events"})
        campaign._base_can_earn = DropsCampaign._base_can_earn.__get__(campaign)
        return campaign

    def _channel(self, *, online, game):
        channel = MagicMock()
        channel.online = online
        channel.game = game
        return channel

    def test_special_campaign_accepts_acl_channel_with_different_game(self):
        campaign = self._campaign(509663)  # a SPECIAL_GAME_IDS entry
        channel = self._channel(online=True, game=MagicMock())
        campaign.allowed_channels = [channel]

        self.assertTrue(campaign._base_can_earn(channel))

    def test_special_campaign_rejects_offline_acl_channel(self):
        campaign = self._campaign(509663)
        channel = self._channel(online=False, game=None)
        campaign.allowed_channels = [channel]

        self.assertFalse(campaign._base_can_earn(channel))

    def test_non_special_campaign_still_requires_matching_game(self):
        campaign = self._campaign(12345)  # not in SPECIAL_GAME_IDS
        channel = self._channel(online=True, game=MagicMock())
        campaign.allowed_channels = [channel]

        self.assertFalse(campaign._base_can_earn(channel))

    def test_game_is_special(self):
        self.assertTrue(Game({"id": 509663, "name": "x"}).is_special())
        self.assertTrue(Game({"id": 509672, "name": "x"}).is_special())
        self.assertFalse(Game({"id": 1, "name": "x"}).is_special())


if __name__ == "__main__":
    unittest.main()
