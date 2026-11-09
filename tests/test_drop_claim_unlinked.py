import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from src.models.drop import TimedDrop


def _drop(kind="DIRECT_ENTITLEMENT", linked=False, assumed=False):
    now = datetime.now(timezone.utc)
    twitch = MagicMock()
    twitch.settings.blacklisted_drop_ids = []
    twitch.settings.drop_name_blacklist = []
    twitch._claim_finalized_drops = set()
    twitch.gql_request = AsyncMock(
        return_value={"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}}
    )
    campaign = MagicMock(linked=linked, link_assumed=assumed, ignored=False)
    campaign._twitch = twitch
    campaign.ends_at = now + timedelta(days=1)
    benefit = {
        "benefit": {"id": "b", "name": "B", "distributionType": kind, "imageAssetURL": "http://x/y.png"}
    }
    data = {
        "id": "d1",
        "name": "Drop",
        "benefitEdges": [benefit],
        "startAt": (now - timedelta(days=1)).isoformat(),
        "endAt": (now + timedelta(days=1)).isoformat(),
        "preconditionDrops": None,
        "requiredMinutesWatched": 10,
        "self": {"dropInstanceID": "inst", "isClaimed": False, "currentMinutesWatched": 5},
    }
    return TimedDrop(campaign, data, {}), twitch


class TestUnlinkedClaim(unittest.IsolatedAsyncioTestCase):
    async def test_unlinked_game_reward_is_skipped(self):
        drop, twitch = _drop()
        self.assertFalse(drop.can_claim)
        self.assertFalse(await drop.claim())
        twitch.gql_request.assert_not_called()

    async def test_unlinked_badge_and_emote_are_claimed(self):
        for kind in ("BADGE", "EMOTE"):
            drop, twitch = _drop(kind)
            self.assertTrue(drop.can_claim)
            self.assertTrue(await drop.claim())
            twitch.gql_request.assert_called_once()

    async def test_assumed_link_is_claimed(self):
        drop, twitch = _drop(assumed=True)
        self.assertTrue(await drop.claim())

    async def test_claimable_once_linked(self):
        drop, _ = _drop()
        drop.campaign.linked = True
        self.assertTrue(drop.can_claim)
