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


def _make_twitch():
    twitch = MagicMock()
    twitch.gui.status.update = MagicMock()
    twitch.gui.inv.clear = MagicMock()
    twitch.gui.inv.add_campaign = AsyncMock()
    twitch.inventory = []
    twitch._drops = {}
    twitch._campaigns = {}
    twitch._mnt_triggers = deque()
    twitch._state = None
    return twitch


class TestCampaignDiscoveryMerge(unittest.IsolatedAsyncioTestCase):
    """
    Browser-based discovery (campaign_discovery.discover_campaigns_via_browser)
    must only ever ADD campaigns SMARTBOX's own Campaigns query missed, never
    override or duplicate what SMARTBOX already found -- see
    campaign_discovery.py's own docstring for why SMARTBOX sees a reduced
    catalog and why this widening exists.
    """

    async def test_browser_discovery_adds_campaigns_smartbox_missed(self):
        twitch = _make_twitch()

        inventory_response = {
            "data": {
                "currentUser": {
                    "inventory": {"dropCampaignsInProgress": [], "gameEventDrops": []}
                }
            }
        }
        # SMARTBOX only sees one campaign.
        campaigns_response = {
            "data": {"currentUser": {"dropCampaigns": [{"id": "smartbox-1", "status": "ACTIVE"}]}}
        }
        twitch.gql_request = AsyncMock(side_effect=[inventory_response, campaigns_response])

        # Browser discovery finds two more, one of which SMARTBOX already has.
        browser_campaigns = [
            {"id": "browser-1", "status": "ACTIVE"},
            {"id": "browser-2", "status": "UPCOMING"},
            {"id": "smartbox-1", "status": "ACTIVE", "poisoned": True},
        ]

        service = InventoryService(twitch)
        seen_ids: set[str] = set()

        def fetch_campaigns_side_effect(campaigns_chunk):
            for cid, data in campaigns_chunk:
                seen_ids.add(cid)
            return {cid: {**data, "game": {"id": "g1"}} for cid, data in campaigns_chunk}

        with (
            patch(
                "src.services.inventory_service.discover_campaigns_via_browser",
                new=AsyncMock(return_value=browser_campaigns),
            ),
            patch.object(
                InventoryService, "fetch_campaigns", side_effect=fetch_campaigns_side_effect
            ),
            patch("src.services.inventory_service.DropsCampaign", _FakeCampaign),
        ):
            await service._fetch_inventory()

        # All three unique campaigns made it through (smartbox-1 not duplicated).
        self.assertEqual(seen_ids, {"smartbox-1", "browser-1", "browser-2"})
        self.assertEqual(len(twitch.inventory), 3)

    async def test_browser_discovery_never_overrides_smartbox_data(self):
        twitch = _make_twitch()

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
                    "dropCampaigns": [{"id": "dup", "status": "ACTIVE", "source": "smartbox"}]
                }
            }
        }
        twitch.gql_request = AsyncMock(side_effect=[inventory_response, campaigns_response])
        browser_campaigns = [{"id": "dup", "status": "ACTIVE", "source": "browser"}]

        service = InventoryService(twitch)
        captured_chunks = []

        def fetch_campaigns_side_effect(campaigns_chunk):
            captured_chunks.extend(campaigns_chunk)
            return {cid: {**data, "game": {"id": "g1"}} for cid, data in campaigns_chunk}

        with (
            patch(
                "src.services.inventory_service.discover_campaigns_via_browser",
                new=AsyncMock(return_value=browser_campaigns),
            ),
            patch.object(
                InventoryService, "fetch_campaigns", side_effect=fetch_campaigns_side_effect
            ),
            patch("src.services.inventory_service.DropsCampaign", _FakeCampaign),
        ):
            await service._fetch_inventory()

        # SMARTBOX's own version of "dup" wins -- browser's copy is discarded.
        self.assertEqual(len(captured_chunks), 1)
        _, data = captured_chunks[0]
        self.assertEqual(data["source"], "smartbox")

    async def test_browser_discovery_failure_does_not_break_inventory_fetch(self):
        twitch = _make_twitch()

        inventory_response = {
            "data": {
                "currentUser": {
                    "inventory": {"dropCampaignsInProgress": [], "gameEventDrops": []}
                }
            }
        }
        campaigns_response = {
            "data": {"currentUser": {"dropCampaigns": [{"id": "smartbox-1", "status": "ACTIVE"}]}}
        }
        twitch.gql_request = AsyncMock(side_effect=[inventory_response, campaigns_response])

        service = InventoryService(twitch)

        with (
            patch(
                "src.services.inventory_service.discover_campaigns_via_browser",
                new=AsyncMock(side_effect=RuntimeError("browser crashed")),
            ),
            patch.object(
                InventoryService,
                "fetch_campaigns",
                side_effect=lambda chunk: {
                    cid: {**data, "game": {"id": "g1"}} for cid, data in chunk
                },
            ),
            patch("src.services.inventory_service.DropsCampaign", _FakeCampaign),
        ):
            # Must not raise even though discovery blew up.
            await service._fetch_inventory()

        self.assertEqual(len(twitch.inventory), 1)


if __name__ == "__main__":
    unittest.main()
