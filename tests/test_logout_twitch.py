import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from src.web import app as app_module


class TestLogoutTwitchEndpoint(unittest.TestCase):
    """
    Settings' "Log out of Twitch" button: removes only the saved Twitch
    cookie jar and restarts, so _AuthState._validate() finds no auth-token
    on the next boot and falls through to a fresh login -- the same
    mechanism /api/accounts/add already relies on for a brand-new account
    (an account dir with no cookies.jar).
    """

    def setUp(self):
        self._setup_patcher = patch("src.web.app._is_setup_done", return_value=True)
        self._setup_patcher.start()
        self.addCleanup(self._setup_patcher.stop)
        self._pw_patcher = patch("src.web.app._get_password", return_value="")
        self._pw_patcher.start()
        self.addCleanup(self._pw_patcher.stop)
        self._token_patcher = patch("src.web.app._get_bot_token", return_value="")
        self._token_patcher.start()
        self.addCleanup(self._token_patcher.stop)
        self.client = TestClient(app_module.app)

    def test_removes_the_cookie_jar_and_schedules_a_restart(self):
        mock_cookies_path = AsyncMock()  # any object with a sync .unlink is fine
        mock_cookies_path.unlink = lambda missing_ok=False: setattr(
            mock_cookies_path, "unlinked", True
        )
        with (
            patch("src.config.COOKIES_PATH", mock_cookies_path),
            patch("src.web.app._restart_self") as mock_restart,
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            response = self.client.post("/api/auth/logout-twitch")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["success"])
            self.assertTrue(getattr(mock_cookies_path, "unlinked", False))

            # The restart is fired via asyncio.create_task on TestClient's
            # own portal-thread event loop -- give that thread a moment to
            # actually run the (patched, instant) background task before
            # asserting on it from here.
            for _ in range(50):
                if mock_restart.called:
                    break
                time.sleep(0.01)
            mock_restart.assert_called_once()

    def test_is_a_noop_when_no_cookie_jar_exists(self):
        with (
            patch("src.config.COOKIES_PATH") as mock_cookies_path,
            patch("src.web.app._restart_self"),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_cookies_path.unlink.side_effect = FileNotFoundError()
            # Path.unlink(missing_ok=True) swallows FileNotFoundError itself;
            # emulate that contract on the mock directly.
            mock_cookies_path.unlink = lambda missing_ok=False: None
            response = self.client.post("/api/auth/logout-twitch")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])


if __name__ == "__main__":
    unittest.main()
