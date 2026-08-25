import unittest
from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch

from src.exceptions import GQLException
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


class TestInventoryServiceChunkResilience(unittest.IsolatedAsyncioTestCase):
    """
    Regression test for the crash report where a single Twitch-side "service
    error" fetching DropCampaignDetails for one chunk of campaigns propagated
    all the way up through fetch_campaigns -> _fetch_inventory ->
    fetch_inventory -> client.py's _run, killing the whole miner
    (exit_status=1) instead of just losing detail for that one chunk.
    """

    async def test_one_failing_chunk_is_skipped_others_still_load(self):
        twitch = MagicMock()
        twitch.gui.status.update = MagicMock()
        twitch.gui.inv.clear = MagicMock()
        twitch.gui.inv.add_campaign = AsyncMock()
        twitch.inventory = []
        twitch._drops = {}
        twitch._campaigns = {}
        twitch._mnt_triggers = deque()
        twitch._state = None  # anything other than State.EXIT

        # 25 campaigns -> two chunks of 20 and 5 (src.utils.chunk splits by 20)
        available_campaigns = [{"id": f"c{i}", "status": "ACTIVE"} for i in range(25)]

        inventory_response = {
            "data": {
                "currentUser": {
                    "inventory": {
                        "dropCampaignsInProgress": [],
                        "gameEventDrops": [],
                    }
                }
            }
        }
        campaigns_response = {
            "data": {"currentUser": {"dropCampaigns": available_campaigns}}
        }
        twitch.gql_request = AsyncMock(side_effect=[inventory_response, campaigns_response])

        def fetch_campaigns_side_effect(campaigns_chunk):
            ids = [cid for cid, _ in campaigns_chunk]
            if "c0" in ids:
                # Simulate the persistent "service error" for this chunk that
                # gql_client's own retries couldn't recover from.
                raise GQLException([{"message": "service error"}])
            return {cid: {**data, "game": {"id": "g1"}} for cid, data in campaigns_chunk}

        service = InventoryService(twitch)

        with patch.object(
            InventoryService, "fetch_campaigns", side_effect=fetch_campaigns_side_effect
        ), patch("src.services.inventory_service.DropsCampaign", _FakeCampaign):
            # Should NOT raise, despite one chunk's fetch_campaigns() failing.
            await service._fetch_inventory()

        # Only the surviving chunk's 5 campaigns made it into the inventory.
        self.assertEqual(len(twitch.inventory), 5)
        self.assertEqual({c.id for c in twitch.inventory}, {f"c{i}" for i in range(20, 25)})
        twitch.gui.inv.add_campaign.assert_awaited()

    async def test_fetch_inventory_reraises_when_all_chunks_fail_but_reschedules_maintenance(self):
        twitch = MagicMock()
        twitch.gui.status.update = MagicMock()
        twitch._mnt_task = None
        twitch._maintenance_service.run_maintenance_task = AsyncMock()

        service = InventoryService(twitch)

        with (
            patch.object(InventoryService, "_fetch_inventory", side_effect=GQLException("boom")),
            patch("asyncio.create_task", side_effect=lambda coro: coro.close() or MagicMock()),
            self.assertRaises(GQLException),
        ):
            await service.fetch_inventory()

        # The maintenance heartbeat must still be rescheduled even though the
        # fetch itself failed (see fetch_inventory's try/finally).
        self.assertIsNotNone(twitch._mnt_task)


if __name__ == "__main__":
    unittest.main()
