import unittest
from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch

from src.services.inventory_service import InventoryService


class _FakeCampaign:
    """Stand-in for DropsCampaign that only exposes what _fetch_inventory needs."""

    def __init__(self, twitch, data, claimed_benefits):
        self.id = data["id"]
        self.data = data
        self.active = True
        self.upcoming = False
        self.eligible = True
        self.starts_at = 0
        self.ends_at = 0
        self.drops: list = []
        self.time_triggers: list = []

    def can_earn_within(self, dt):
        return False


def _responses_for(campaign_ids: list[str]) -> tuple[dict, dict]:
    inventory_response = {
        "data": {
            "currentUser": {
                "inventory": {"dropCampaignsInProgress": [], "gameEventDrops": []}
            }
        }
    }
    campaigns_response = {
        "data": {
            "currentUser": {
                "dropCampaigns": [{"id": cid, "status": "ACTIVE"} for cid in campaign_ids]
            }
        }
    }
    return inventory_response, campaigns_response


class TestInventoryCampaignsDoesNotLeak(unittest.IsolatedAsyncioTestCase):
    """
    Regression test: Twitch._campaigns was populated every inventory fetch
    (src/services/inventory_service.py) but, unlike its siblings _drops/
    inventory/_mnt_triggers cleared in the same method (and unlike shutdown(),
    which also clears those but not _campaigns), it was never cleared. Over a
    long-running miner (days/weeks, hourly inventory reloads per the
    maintenance task) this dict grew forever, holding a full DropsCampaign
    object - with its drops and allowed_channels - for every campaign ID ever
    seen for the life of the process. This is a plausible contributor to the
    "suspected memory leak" reported after several days of continuous
    operation (GitHub issue #7).
    """

    async def test_campaigns_from_previous_fetch_are_not_retained(self):
        twitch = MagicMock()
        twitch.gui.status.update = MagicMock()
        twitch.gui.inv.clear = MagicMock()
        twitch.gui.inv.add_campaign = AsyncMock()
        twitch.inventory = []
        twitch._drops = {}
        twitch._campaigns = {}
        twitch._mnt_triggers = deque()
        twitch._state = None

        service = InventoryService(twitch)

        with patch("src.services.inventory_service.DropsCampaign", _FakeCampaign):
            # First fetch cycle: campaigns c0..c2 are live.
            twitch.gql_request = AsyncMock(side_effect=_responses_for(["c0", "c1", "c2"]))
            with patch.object(
                InventoryService,
                "fetch_campaigns",
                side_effect=lambda chunk: {
                    cid: {**data, "game": {"id": "g1"}} for cid, data in chunk
                },
            ):
                await service._fetch_inventory()

            self.assertEqual(set(twitch._campaigns.keys()), {"c0", "c1", "c2"})

            # Second fetch cycle (e.g. the hourly maintenance reload): c0/c1
            # have expired and rotated out, only c3 is live now.
            twitch.gql_request = AsyncMock(side_effect=_responses_for(["c3"]))
            with patch.object(
                InventoryService,
                "fetch_campaigns",
                side_effect=lambda chunk: {
                    cid: {**data, "game": {"id": "g1"}} for cid, data in chunk
                },
            ):
                await service._fetch_inventory()

        # Only the current cycle's campaign should remain - the previous
        # cycle's entries must not linger forever.
        self.assertEqual(set(twitch._campaigns.keys()), {"c3"})


if __name__ == "__main__":
    unittest.main()
