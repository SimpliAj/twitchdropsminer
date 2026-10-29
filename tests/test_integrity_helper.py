from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import AioHTTPTestCase

from src.auth.integrity_helper import create_app


class TestIntegrityHelperServer(AioHTTPTestCase):
    async def get_application(self):
        return create_app("s3cret-key-1234567890")

    async def test_rejects_a_wrong_key(self):
        with patch("src.auth.integrity_helper.mint_integrity_token_locally", new=AsyncMock()) as mint:
            response = await self.client.post("/mint", json={"auth_token": "t"}, headers={"Authorization": "Bearer nope"})
        self.assertEqual(response.status, 401)
        mint.assert_not_awaited()

    async def test_mints_for_the_given_session(self):
        expiry = datetime(2030, 1, 1, tzinfo=timezone.utc)
        mint = AsyncMock(return_value=("itok", expiry))
        with patch("src.auth.integrity_helper.mint_integrity_token_locally", new=mint):
            response = await self.client.post(
                "/mint", json={"auth_token": "t", "device_id": "d"},
                headers={"Authorization": "Bearer s3cret-key-1234567890"},
            )
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"token": "itok", "expires_at": expiry.timestamp()})
        mint.assert_awaited_once_with("t", "d")

    async def test_reports_a_failed_mint(self):
        with patch("src.auth.integrity_helper.mint_integrity_token_locally", new=AsyncMock(return_value=None)):
            response = await self.client.post(
                "/mint", json={"auth_token": "t"}, headers={"Authorization": "Bearer s3cret-key-1234567890"},
            )
        self.assertEqual(response.status, 502)
