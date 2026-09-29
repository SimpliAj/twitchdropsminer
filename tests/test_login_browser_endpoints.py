import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.web import app as app_module


class TestBrowserLoginEndpoints(unittest.TestCase):
    def setUp(self):
        # PasswordAuthMiddleware redirects every request to /__setup until
        # first-time setup is marked done (see _is_setup_done) -- irrelevant
        # to what's under test here, so short-circuit it.
        self._setup_patcher = patch("src.web.app._is_setup_done", return_value=True)
        self._setup_patcher.start()
        self.addCleanup(self._setup_patcher.stop)

        self.client = TestClient(app_module.app)
        app_module.gui_manager = MagicMock()
        app_module.gui_manager.login = MagicMock()
        # The real endpoint awaits gui_manager.login.start_browser_login(...)
        # -- a plain MagicMock call result isn't awaitable, so this specific
        # attribute needs to be an AsyncMock.
        app_module.gui_manager.login.start_browser_login = AsyncMock()
        self._manager_patcher = patch("src.web.app._browser_login_manager", None)
        self._manager_patcher.start()
        self.addCleanup(self._manager_patcher.stop)

    def test_start_rejects_when_gui_not_initialized(self):
        app_module.gui_manager = None
        response = self.client.post("/api/login/browser/start")
        self.assertEqual(response.status_code, 503)

    def test_start_rejects_a_second_concurrent_attempt(self):
        with patch("src.web.app.BrowserLoginManager") as MockManager:
            instance = MockManager.return_value
            instance.in_progress = False
            instance.start = AsyncMock(return_value=6990)

            first = self.client.post("/api/login/browser/start")
            self.assertEqual(first.status_code, 200)

            instance.in_progress = True
            second = self.client.post("/api/login/browser/start")
            self.assertEqual(second.status_code, 409)

    def test_cancel_calls_manager_cancel(self):
        with patch("src.web.app.BrowserLoginManager") as MockManager:
            instance = MockManager.return_value
            instance.in_progress = False
            instance.start = AsyncMock(return_value=6990)
            instance.cancel = MagicMock()

            self.client.post("/api/login/browser/start")
            response = self.client.post("/api/login/browser/cancel")
            self.assertEqual(response.status_code, 200)
            instance.cancel.assert_called_once()


class TestBrowserLoginWebSocketAuth(unittest.TestCase):
    """
    Regression coverage for a reviewer-flagged Critical finding: unlike every
    HTTP route, this WebSocket is not covered by PasswordAuthMiddleware (a
    BaseHTTPMiddleware, which never runs for scope["type"] == "websocket"),
    so the route itself must reject a mismatched Origin and an unauthenticated
    caller before ever accepting the connection or touching the in-progress
    browser-login session it would otherwise proxy into unconditionally.
    """

    def setUp(self):
        self._setup_patcher = patch("src.web.app._is_setup_done", return_value=True)
        self._setup_patcher.start()
        self.addCleanup(self._setup_patcher.stop)
        self.client = TestClient(app_module.app)

    def test_rejects_missing_or_mismatched_origin(self):
        with (
            self.assertRaises(WebSocketDisconnect) as ctx,
            self.client.websocket_connect("/api/login/browser/ws"),
        ):
            pass
        self.assertEqual(ctx.exception.code, 1008)

    def test_rejects_when_password_set_and_no_valid_session_or_token(self):
        with (
            patch("src.web.app._get_password", return_value="hunter2"),
            self.assertRaises(WebSocketDisconnect) as ctx,
            self.client.websocket_connect(
                "/api/login/browser/ws",
                headers={"origin": "http://testserver", "host": "testserver"},
            ),
        ):
            pass
        self.assertEqual(ctx.exception.code, 1008)

    def test_allows_matching_origin_with_no_password_set_through_to_the_in_progress_gate(self):
        # No password configured and origin matches host -- auth passes, so
        # the connection reaches the next gate (in-progress check) instead
        # of being rejected for Origin/auth reasons (1008).
        with (
            patch("src.web.app._browser_login_manager", None),
            self.assertRaises(WebSocketDisconnect) as ctx,
            self.client.websocket_connect(
                "/api/login/browser/ws",
                headers={"origin": "http://testserver", "host": "testserver"},
            ),
        ):
            pass
        self.assertEqual(ctx.exception.code, 4404)


if __name__ == "__main__":
    unittest.main()
