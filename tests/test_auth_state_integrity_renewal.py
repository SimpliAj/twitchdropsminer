import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from src.auth.auth_state import _AuthState


def _auth_state_with_identity():
    auth_state = _AuthState(MagicMock())
    auth_state.access_token = "token123"
    auth_state.device_id = "device-abc"
    return auth_state


class TestEnsureIntegrityTokenRenewal(unittest.IsolatedAsyncioTestCase):
    """
    _ensure_integrity_token is now the RENEWAL path only -- a fresh login
    already adopts whatever browser_login._capture_integrity_token caught
    live (see test_auth_state_browser_login.py). This covers what happens
    once that one expires: browser_login.acquire_integrity_token() mints a
    fresh one the same way (real, throwaway Chromium, no interactive login
    needed), replacing the old streamlink-based approach.
    """

    async def test_does_nothing_before_login_completes(self):
        auth_state = _AuthState(MagicMock())  # no access_token/device_id set
        with patch(
            "src.auth.browser_login.acquire_integrity_token", new=AsyncMock()
        ) as mock_acquire:
            await auth_state._ensure_integrity_token()
        mock_acquire.assert_not_awaited()

    async def test_acquires_and_caches_a_token(self):
        auth_state = _auth_state_with_identity()
        expiry = datetime.now(timezone.utc) + timedelta(hours=4)
        with patch(
            "src.auth.browser_login.acquire_integrity_token",
            new=AsyncMock(return_value=("itok-1", expiry)),
        ) as mock_acquire:
            await auth_state._ensure_integrity_token()

        mock_acquire.assert_awaited_once_with("token123", "device-abc")
        self.assertEqual(auth_state._integrity_token, "itok-1")
        self.assertEqual(auth_state._integrity_expires_at, expiry)

    async def test_does_not_reacquire_while_still_fresh(self):
        auth_state = _auth_state_with_identity()
        auth_state._integrity_token = "itok-existing"
        auth_state._integrity_expires_at = datetime.now(timezone.utc) + timedelta(hours=4)
        with patch(
            "src.auth.browser_login.acquire_integrity_token", new=AsyncMock()
        ) as mock_acquire:
            await auth_state._ensure_integrity_token()
        mock_acquire.assert_not_awaited()
        self.assertEqual(auth_state._integrity_token, "itok-existing")

    async def test_reacquires_once_inside_the_renew_margin(self):
        auth_state = _auth_state_with_identity()
        auth_state._integrity_token = "itok-stale"
        auth_state._integrity_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        new_expiry = datetime.now(timezone.utc) + timedelta(hours=4)
        with patch(
            "src.auth.browser_login.acquire_integrity_token",
            new=AsyncMock(return_value=("itok-fresh", new_expiry)),
        ) as mock_acquire:
            await auth_state._ensure_integrity_token()
        mock_acquire.assert_awaited_once()
        self.assertEqual(auth_state._integrity_token, "itok-fresh")

    async def test_failure_sets_a_cooldown_and_does_not_raise(self):
        auth_state = _auth_state_with_identity()
        with patch(
            "src.auth.browser_login.acquire_integrity_token", new=AsyncMock(return_value=None)
        ):
            await auth_state._ensure_integrity_token()  # must not raise
        self.assertIsNone(auth_state._integrity_token)
        self.assertIsNotNone(auth_state._integrity_failed_until)

    async def test_does_not_hammer_acquisition_during_the_failure_cooldown(self):
        auth_state = _auth_state_with_identity()
        auth_state._integrity_failed_until = datetime.now(timezone.utc) + timedelta(minutes=4)
        with patch(
            "src.auth.browser_login.acquire_integrity_token", new=AsyncMock()
        ) as mock_acquire:
            await auth_state._ensure_integrity_token()
        mock_acquire.assert_not_awaited()

    async def test_retries_again_once_the_cooldown_has_passed(self):
        auth_state = _auth_state_with_identity()
        auth_state._integrity_failed_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        expiry = datetime.now(timezone.utc) + timedelta(hours=4)
        with patch(
            "src.auth.browser_login.acquire_integrity_token",
            new=AsyncMock(return_value=("itok-2", expiry)),
        ) as mock_acquire:
            await auth_state._ensure_integrity_token()
        mock_acquire.assert_awaited_once()
        self.assertEqual(auth_state._integrity_token, "itok-2")

    async def test_a_successful_reacquisition_clears_a_stale_failure_cooldown(self):
        auth_state = _auth_state_with_identity()
        auth_state._integrity_failed_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        expiry = datetime.now(timezone.utc) + timedelta(hours=4)
        with patch(
            "src.auth.browser_login.acquire_integrity_token",
            new=AsyncMock(return_value=("itok-3", expiry)),
        ):
            await auth_state._ensure_integrity_token()
        self.assertIsNone(auth_state._integrity_failed_until)


class TestHeadersIncludeClientIntegrity(unittest.TestCase):
    def test_gql_headers_include_the_cached_token_when_present(self):
        auth_state = _auth_state_with_identity()
        auth_state._integrity_token = "itok-live"
        auth_state._twitch._client_type = MagicMock(CLIENT_ID="kimne78kx3ncx6brgo4mv6wki5h1ko")

        headers = auth_state.headers(gql=True)

        self.assertEqual(headers["Client-Integrity"], "itok-live")

    def test_gql_headers_omit_it_when_no_token_is_cached_yet(self):
        auth_state = _auth_state_with_identity()
        auth_state._twitch._client_type = MagicMock(CLIENT_ID="kimne78kx3ncx6brgo4mv6wki5h1ko")

        headers = auth_state.headers(gql=True)

        self.assertNotIn("Client-Integrity", headers)

    def test_non_gql_headers_never_include_it_even_if_cached(self):
        auth_state = _auth_state_with_identity()
        auth_state._integrity_token = "itok-live"
        auth_state._twitch._client_type = MagicMock(CLIENT_ID="kimne78kx3ncx6brgo4mv6wki5h1ko")

        headers = auth_state.headers(gql=False)

        self.assertNotIn("Client-Integrity", headers)
        self.assertNotIn("Authorization", headers)


if __name__ == "__main__":
    unittest.main()
