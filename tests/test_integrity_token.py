import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from src.auth import integrity
from src.auth.auth_state import _AuthState


def _auth_state_with_identity():
    mock_twitch = MagicMock()
    auth_state = _AuthState(mock_twitch)
    auth_state.access_token = "token123"
    auth_state.device_id = "device-abc"
    return auth_state


class TestEnsureIntegrityToken(unittest.IsolatedAsyncioTestCase):
    """
    ClientType.WEB -- the real browser session's own identity, used for
    every GQL request since login stopped being device-code -- gets hard-
    rejected with "failed integrity check" on dropCampaigns specifically
    (reported live: a fresh, successful login immediately followed by a
    fatal GQLException on the very first inventory fetch). This is the
    regression test for the fix: a Client-Integrity token, cached and
    renewed, attached to headers() when available.
    """

    async def test_does_nothing_before_login_completes(self):
        auth_state = _AuthState(MagicMock())  # no access_token/device_id set
        with patch("src.auth.integrity.acquire", new=AsyncMock()) as mock_acquire:
            await auth_state._ensure_integrity_token()
        mock_acquire.assert_not_awaited()

    async def test_acquires_and_caches_a_token(self):
        auth_state = _auth_state_with_identity()
        expires_at = datetime.now(timezone.utc) + timedelta(hours=4)
        with patch(
            "src.auth.integrity.acquire",
            new=AsyncMock(return_value=("itok-1", expires_at)),
        ) as mock_acquire:
            await auth_state._ensure_integrity_token()

        mock_acquire.assert_awaited_once_with("token123", "device-abc")
        self.assertEqual(auth_state._integrity_token, "itok-1")
        self.assertEqual(auth_state._integrity_expires_at, expires_at)

    async def test_does_not_reacquire_while_still_fresh(self):
        auth_state = _auth_state_with_identity()
        auth_state._integrity_token = "itok-existing"
        auth_state._integrity_expires_at = datetime.now(timezone.utc) + timedelta(hours=4)
        with patch("src.auth.integrity.acquire", new=AsyncMock()) as mock_acquire:
            await auth_state._ensure_integrity_token()
        mock_acquire.assert_not_awaited()
        self.assertEqual(auth_state._integrity_token, "itok-existing")

    async def test_reacquires_once_inside_the_renew_margin(self):
        auth_state = _auth_state_with_identity()
        auth_state._integrity_token = "itok-stale"
        # Inside RENEW_MARGIN (15min) of expiring -- due for renewal.
        auth_state._integrity_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        new_expiry = datetime.now(timezone.utc) + timedelta(hours=4)
        with patch(
            "src.auth.integrity.acquire",
            new=AsyncMock(return_value=("itok-fresh", new_expiry)),
        ) as mock_acquire:
            await auth_state._ensure_integrity_token()
        mock_acquire.assert_awaited_once()
        self.assertEqual(auth_state._integrity_token, "itok-fresh")

    async def test_failure_sets_a_cooldown_and_does_not_raise(self):
        auth_state = _auth_state_with_identity()
        with patch("src.auth.integrity.acquire", new=AsyncMock(return_value=None)):
            await auth_state._ensure_integrity_token()  # must not raise
        self.assertIsNone(auth_state._integrity_token)
        self.assertIsNotNone(auth_state._integrity_failed_until)

    async def test_does_not_hammer_acquisition_during_the_failure_cooldown(self):
        auth_state = _auth_state_with_identity()
        auth_state._integrity_failed_until = datetime.now(timezone.utc) + timedelta(minutes=4)
        with patch("src.auth.integrity.acquire", new=AsyncMock()) as mock_acquire:
            await auth_state._ensure_integrity_token()
        mock_acquire.assert_not_awaited()

    async def test_retries_again_once_the_cooldown_has_passed(self):
        auth_state = _auth_state_with_identity()
        auth_state._integrity_failed_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=4)
        with patch(
            "src.auth.integrity.acquire",
            new=AsyncMock(return_value=("itok-2", expires_at)),
        ) as mock_acquire:
            await auth_state._ensure_integrity_token()
        mock_acquire.assert_awaited_once()
        self.assertEqual(auth_state._integrity_token, "itok-2")


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


class TestChromiumPath(unittest.TestCase):
    """
    Resolving a Chromium binary for the integrity-token subprocess (a
    separate concern from the login browser, but ideally the same binary):
    explicit env var wins, then Playwright's own already-installed
    Chromium (avoids a second ~280MB system package), then the old
    /usr/bin/chromium default.
    """

    def test_explicit_env_var_wins(self):
        with patch.dict("os.environ", {"TDM_CHROMIUM_PATH": "/custom/chrome"}, clear=True):
            self.assertEqual(integrity.chromium_path(), "/custom/chrome")

    def test_falls_back_to_playwrights_bundled_chromium(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            patch(
                "src.auth.integrity.glob.glob",
                return_value=[
                    "/home/user/.cache/ms-playwright/chromium-1200/chrome-linux64/chrome",
                    "/home/user/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome",
                ],
            ),
        ):
            self.assertEqual(
                integrity.chromium_path(),
                "/home/user/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome",
            )

    def test_falls_back_to_system_chromium_when_nothing_else_is_found(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("src.auth.integrity.glob.glob", return_value=[]),
        ):
            self.assertEqual(integrity.chromium_path(), "/usr/bin/chromium")


if __name__ == "__main__":
    unittest.main()
