import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from src.exceptions import GQLException
from src.services.inventory_service import InventoryService
from src.services.public_catalog import fetch_public_campaigns, parse_catalog


NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def _payload(updated="2026-10-05T11:59:00Z"):
    return {
        "lastUpdatedAt": updated,
        "data": [
            {"rewards": [{"id": "a", "status": "ACTIVE"}, {"id": "b", "status": "EXPIRED"}]},
            {"rewards": [{"id": "c", "status": "UPCOMING"}, {"id": "a", "status": "ACTIVE"}]},
            {},
        ],
    }


class TestParseCatalog(unittest.TestCase):
    def test_keeps_active_and_upcoming_deduped(self):
        ids = [c["id"] for c in parse_catalog(_payload(), NOW)]
        self.assertEqual(sorted(ids), ["a", "c"])

    def test_stale_feed_rejected(self):
        self.assertEqual(parse_catalog(_payload("2026-10-05T10:00:00Z"), NOW), [])

    def test_garbage_rejected(self):
        self.assertEqual(parse_catalog({"nope": 1}, NOW), [])
        self.assertEqual(parse_catalog(None, NOW), [])


class TestFetch(unittest.IsolatedAsyncioTestCase):
    async def test_network_error_returns_empty(self):
        session = MagicMock()
        session.get.side_effect = OSError("down")
        self.assertEqual(await fetch_public_campaigns(session), [])


class TestInventoryFallback(unittest.IsolatedAsyncioTestCase):
    def _twitch(self, campaigns_exc):
        twitch = MagicMock()
        twitch.gui.status.update = MagicMock()
        inv = {"data": {"currentUser": {"inventory": {"dropCampaignsInProgress": [], "gameEventDrops": []}}}}
        twitch.gql_request = AsyncMock(side_effect=[inv, campaigns_exc])
        twitch.get_session = AsyncMock(return_value=MagicMock())
        twitch._mnt_triggers = []
        twitch._drops, twitch._campaigns = {}, {}
        return twitch

    async def test_integrity_failure_uses_public_feed(self):
        twitch = self._twitch(GQLException("IntegrityCheckFailed"))
        service = InventoryService(twitch)
        service.fetch_campaigns = AsyncMock(return_value={})
        with (
            patch("src.services.inventory_service.fetch_public_campaigns",
                  AsyncMock(return_value=[{"id": "a", "status": "ACTIVE"}])),
            patch("src.services.inventory_service.discover_campaigns_via_browser",
                  AsyncMock(return_value=[])),
        ):
            await service._fetch_inventory()
        (chunk_arg,), _kw = service.fetch_campaigns.call_args
        self.assertEqual([k for k, _v in chunk_arg], ["a"])

    async def test_other_gql_error_still_raises(self):
        service = InventoryService(self._twitch(GQLException("service error")))
        with self.assertRaises(GQLException):
            await service._fetch_inventory()
