import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.auth.auth_state import BrowserLoginRequested, DeviceCodeRequested, _AuthState
from src.config import ClientType
from src.services.inventory_service import InventoryService


class TestLoginSwitching(unittest.IsolatedAsyncioTestCase):
    def _auth(self):
        twitch = MagicMock()
        return _AuthState(twitch), twitch

    async def test_device_code_request_switches_identity(self):
        auth, twitch = self._auth()
        auth._device_code_requested = True
        auth._device_code_login = AsyncMock(return_value="tok")
        self.assertEqual(await auth._acquire_token(), "tok")
        twitch.set_client_type.assert_called_with(ClientType.SMARTBOX)

    async def test_browser_login_uses_web_identity(self):
        auth, twitch = self._auth()
        auth._device_code_requested = False
        auth._browser_login = AsyncMock(return_value="tok")
        self.assertEqual(await auth._acquire_token(), "tok")
        twitch.set_client_type.assert_called_with(ClientType.WEB)

    async def test_browser_to_device_code_and_back(self):
        auth, twitch = self._auth()
        calls = []

        async def browser():
            calls.append("browser")
            if len(calls) == 1:
                auth._device_code_requested = True
                raise DeviceCodeRequested()
            return "web-tok"

        async def device():
            calls.append("device")
            auth._device_code_requested = False  # user pressed "back"
            raise BrowserLoginRequested()

        auth._browser_login = browser
        auth._device_code_login = device
        self.assertEqual(await auth._acquire_token(), "web-tok")
        self.assertEqual(calls, ["browser", "device", "browser"])

    async def test_cancel_event_aborts_wait(self):
        auth, _ = self._auth()
        auth.request_browser_login()
        with self.assertRaises(BrowserLoginRequested):
            await auth._sleep_or_switch(5)

    async def test_env_var_selects_device_code(self):
        with patch.dict("os.environ", {"TDM_LOGIN_METHOD": "device_code"}):
            auth, _ = self._auth()
        self.assertTrue(auth._device_code_requested)

    async def test_integrity_skipped_for_smartbox(self):
        auth, twitch = self._auth()
        twitch._client_type = ClientType.SMARTBOX
        auth.access_token, auth.device_id = "t", "d"
        with patch("src.auth.browser_login.acquire_integrity_token", AsyncMock()) as acq:
            await auth._ensure_integrity_token()
        acq.assert_not_called()


class TestSmartboxFeedMerge(unittest.IsolatedAsyncioTestCase):
    async def test_feed_added_to_reduced_list(self):
        twitch = MagicMock()
        twitch.gui.status.update = MagicMock()
        twitch._client_type = ClientType.SMARTBOX
        twitch.settings.proxy = ""
        inv = {"data": {"currentUser": {"inventory": {"dropCampaignsInProgress": [], "gameEventDrops": []}}}}
        camp = {"data": {"currentUser": {"dropCampaigns": [{"id": "a", "status": "ACTIVE"}]}}}
        twitch.gql_request = AsyncMock(side_effect=[inv, camp])
        twitch.get_session = AsyncMock(return_value=MagicMock())
        twitch._mnt_triggers = []
        twitch._drops, twitch._campaigns = {}, {}
        service = InventoryService(twitch)
        service.fetch_campaigns = AsyncMock(return_value={})
        with (
            patch("src.services.inventory_service.fetch_public_campaigns",
                  AsyncMock(return_value=[{"id": "a", "status": "ACTIVE"}, {"id": "b", "status": "ACTIVE"}])),
            patch("src.services.inventory_service.discover_campaigns_via_browser", AsyncMock(return_value=[])),
        ):
            await service._fetch_inventory()
        (chunk_arg,), _kw = service.fetch_campaigns.call_args
        self.assertEqual(sorted(k for k, _v in chunk_arg), ["a", "b"])
