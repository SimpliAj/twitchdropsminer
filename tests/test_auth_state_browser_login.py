import unittest
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

            token = await auth_state._browser_login()

            self.assertEqual(token, "real-token-value")
            instance.start.assert_awaited_once()
            instance.wait_for_cookie.assert_awaited_once()
            instance.stop.assert_awaited_once()

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
