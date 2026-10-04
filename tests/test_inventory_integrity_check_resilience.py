import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.exceptions import GQLException
from src.services.inventory_service import InventoryService


class TestInventoryIntegrityCheckResilience(unittest.IsolatedAsyncioTestCase):
    """
    Regression test for the crash reported live right after a successful
    real-browser login: the very first inventory fetch's "Campaigns" GQL
    query failed Twitch's integrity check (ClientType.WEB needs a real
    Client-Integrity token -- see _AuthState._ensure_integrity_token --
    which hadn't been acquired yet), and that GQLException propagated all
    the way up through fetch_inventory -> client.py's _run, killing the
    whole miner outright on every single occurrence, including on restart.
    """

    def _integrity_check_failed_exception(self) -> GQLException:
        return GQLException(
            [{
                "message": "failed integrity check",
                "path": ["currentUser", "dropCampaigns"],
                "extensions": {"code": "IntegrityCheckFailed"},
            }]
        )

    async def test_integrity_check_failure_does_not_crash_and_reschedules_maintenance(self):
        twitch = MagicMock()
        twitch.gui.status.update = MagicMock()
        twitch._mnt_task = None
        twitch._maintenance_service.run_maintenance_task = AsyncMock()

        service = InventoryService(twitch)

        with (
            patch.object(
                InventoryService,
                "_fetch_inventory",
                side_effect=self._integrity_check_failed_exception(),
            ),
            patch("asyncio.create_task", side_effect=lambda coro: coro.close() or MagicMock()),
        ):
            await service.fetch_inventory()  # must NOT raise

        # The maintenance heartbeat is still rescheduled, so the next
        # periodic reload gets a chance to retry once a token is acquired.
        self.assertIsNotNone(twitch._mnt_task)

    async def test_a_different_gql_error_still_raises_unaffected(self):
        # Only the specific IntegrityCheckFailed case is swallowed -- any
        # other GQLException must keep crashing loudly, same as before this
        # fix (e.g. the all-chunks-failed case already covered by
        # test_inventory_service_chunk_resilience.py).
        twitch = MagicMock()
        twitch.gui.status.update = MagicMock()
        twitch._mnt_task = None
        twitch._maintenance_service.run_maintenance_task = AsyncMock()

        service = InventoryService(twitch)

        with (
            patch.object(
                InventoryService, "_fetch_inventory", side_effect=GQLException("service error")
            ),
            patch("asyncio.create_task", side_effect=lambda coro: coro.close() or MagicMock()),
            self.assertRaises(GQLException),
        ):
            await service.fetch_inventory()


if __name__ == "__main__":
    unittest.main()
