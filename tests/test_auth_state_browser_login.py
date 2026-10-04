import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from src.auth.auth_state import _AuthState


class TestBrowserLoginIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_browser_login_returns_access_token_from_manager(self):
        mock_twitch = MagicMock()
        mock_twitch.gui.login.start_browser_login = AsyncMock()
        auth_state = _AuthState(mock_twitch)

        # _browser_login() does `from src.auth.browser_login import
        # BrowserLoginManager` INSIDE the method (a local import, to avoid
        # a circular import between auth_state.py and browser_login.py) --
        # patch it at its origin (src.auth.browser_login), not at
        # src.auth.auth_state, since that name is never a module-level
        # attribute of auth_state.py. The local import re-resolves the
        # module's current attribute at call time, so patching the origin
        # before calling _browser_login() works correctly.
        with patch("src.auth.browser_login.BrowserLoginManager") as MockManager:
            instance = MockManager.return_value
            instance.start = AsyncMock(return_value=6990)
            instance.wait_for_cookie = AsyncMock(
                return_value={"auth-token": "real-token-value", "unique_id": "device-abc"}
            )
            instance.stop = AsyncMock()
            instance.captured_integrity_token = AsyncMock(return_value=None)

            token = await auth_state._browser_login()

            self.assertEqual(token, "real-token-value")
            instance.start.assert_awaited_once()
            instance.wait_for_cookie.assert_awaited_once()
            instance.stop.assert_awaited_once()

    async def test_browser_login_adopts_the_token_captured_live_during_login(self):
        # The login page's own navigation already triggers Twitch's
        # integrity check (see browser_login._capture_integrity_token) --
        # no separate acquisition needed right after a successful login.
        mock_twitch = MagicMock()
        mock_twitch.gui.login.start_browser_login = AsyncMock()
        auth_state = _AuthState(mock_twitch)
        expiry = datetime.now(timezone.utc) + timedelta(hours=4)

        with patch("src.auth.browser_login.BrowserLoginManager") as MockManager:
            instance = MockManager.return_value
            instance.start = AsyncMock(return_value=6990)
            instance.wait_for_cookie = AsyncMock(
                return_value={"auth-token": "real-token-value", "unique_id": "device-abc"}
            )
            instance.stop = AsyncMock()
            instance.captured_integrity_token = AsyncMock(return_value=("itok-live", expiry))

            await auth_state._browser_login()

        self.assertEqual(auth_state._integrity_token, "itok-live")
        self.assertEqual(auth_state._integrity_expires_at, expiry)

    async def test_browser_login_on_a_real_display_skips_the_websocket_path(self):
        # manager.start() returning None means the login window opened
        # directly on a real desktop display (see
        # BrowserLoginManager._detect_real_display) -- there is no noVNC
        # port to tell the dashboard about, so a different LoginForm
        # method has to be the one called.
        mock_twitch = MagicMock()
        mock_twitch.gui.login.start_browser_login = AsyncMock()
        mock_twitch.gui.login.start_browser_login_on_real_display = AsyncMock()
        auth_state = _AuthState(mock_twitch)

        with patch("src.auth.browser_login.BrowserLoginManager") as MockManager:
            instance = MockManager.return_value
            instance.start = AsyncMock(return_value=None)
            instance.wait_for_cookie = AsyncMock(
                return_value={"auth-token": "real-token-value", "unique_id": "device-abc"}
            )
            instance.stop = AsyncMock()
            instance.captured_integrity_token = AsyncMock(return_value=None)

            token = await auth_state._browser_login()

            self.assertEqual(token, "real-token-value")
            mock_twitch.gui.login.start_browser_login_on_real_display.assert_awaited_once()
            mock_twitch.gui.login.start_browser_login.assert_not_awaited()

    async def test_browser_login_stops_manager_even_on_failure(self):
        mock_twitch = MagicMock()
        mock_twitch.gui.login.start_browser_login = AsyncMock()
        auth_state = _AuthState(mock_twitch)

        with patch("src.auth.browser_login.BrowserLoginManager") as MockManager:
            instance = MockManager.return_value
            instance.start = AsyncMock(return_value=6990)
            instance.wait_for_cookie = AsyncMock(side_effect=RuntimeError("timed out"))
            instance.stop = AsyncMock()

            with self.assertRaises(RuntimeError):
                await auth_state._browser_login()

            instance.stop.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
