import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.auth import browser_login
from src.auth.auth_state import _AuthState
from src.auth.browser_login import BrowserLoginCancelled
from src.i18n import _
from src.web import app as app_module


def _fake_manager(port: int = 6993) -> MagicMock:
    """A stand-in for BrowserLoginManager exposing only the public surface
    the web layer is allowed to touch."""
    manager = MagicMock()
    manager.in_progress = True
    manager.websocket_port = port
    manager.cancel = MagicMock()
    manager.start = AsyncMock(return_value=port)
    manager.stop = AsyncMock()
    manager.inject_manual_cookies = AsyncMock()
    manager.captured_integrity_token = AsyncMock(return_value=None)
    return manager


class TestBrowserLoginCancelEndpoint(unittest.TestCase):
    def setUp(self):
        # PasswordAuthMiddleware redirects every request to /__setup until
        # first-time setup is marked done (see _is_setup_done) -- irrelevant
        # to what's under test here, so short-circuit it.
        self._setup_patcher = patch("src.web.app._is_setup_done", return_value=True)
        self._setup_patcher.start()
        self.addCleanup(self._setup_patcher.stop)
        # These endpoints are otherwise gated by whatever password/bot-token
        # happen to be configured on the machine running the suite -- pin
        # them to "unset" so the test doesn't depend on host state.
        self._pw_patcher = patch("src.web.app._get_password", return_value="")
        self._pw_patcher.start()
        self.addCleanup(self._pw_patcher.stop)
        self._token_patcher = patch("src.web.app._get_bot_token", return_value="")
        self._token_patcher.start()
        self.addCleanup(self._token_patcher.stop)
        self.client = TestClient(app_module.app)
        self.addCleanup(browser_login.set_active_manager, None)

    def test_start_endpoint_no_longer_exists(self):
        # The auth flow is the one and only owner of a login attempt; a
        # second entry point here would start a Chromium session nobody
        # polls a cookie for.
        response = self.client.post("/api/login/browser/start")
        self.assertEqual(response.status_code, 404)

    def test_cancel_is_a_noop_when_no_attempt_is_in_progress(self):
        browser_login.set_active_manager(None)
        response = self.client.post("/api/login/browser/cancel")
        self.assertEqual(response.status_code, 200)

    def test_cancel_cancels_the_active_manager(self):
        manager = _fake_manager()
        browser_login.set_active_manager(manager)
        response = self.client.post("/api/login/browser/cancel")
        self.assertEqual(response.status_code, 200)
        manager.cancel.assert_called_once()


class TestManualCookieLoginEndpoint(unittest.TestCase):
    """
    Fallback for a datacenter IP getting flagged by Twitch's own bot/
    integrity check regardless of anything this app does: log in on a
    real device instead, then hand the resulting cookies to the still-open
    server-side session via this endpoint.
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
        self.addCleanup(browser_login.set_active_manager, None)

    def test_rejects_a_missing_auth_token(self):
        browser_login.set_active_manager(_fake_manager())
        response = self.client.post("/api/login/browser/manual-cookies", json={"auth_token": "  "})
        self.assertEqual(response.status_code, 400)

    def test_rejects_when_no_login_is_in_progress(self):
        browser_login.set_active_manager(None)
        response = self.client.post(
            "/api/login/browser/manual-cookies", json={"auth_token": "abc123"}
        )
        self.assertEqual(response.status_code, 409)

    def test_forwards_cookies_to_the_active_manager(self):
        manager = _fake_manager()
        browser_login.set_active_manager(manager)
        response = self.client.post(
            "/api/login/browser/manual-cookies",
            json={"auth_token": " abc123 ", "unique_id": " device-xyz "},
        )
        self.assertEqual(response.status_code, 200)
        manager.inject_manual_cookies.assert_awaited_once_with("abc123", "device-xyz")

    def test_unique_id_defaults_to_empty(self):
        manager = _fake_manager()
        browser_login.set_active_manager(manager)
        response = self.client.post(
            "/api/login/browser/manual-cookies", json={"auth_token": "abc123"}
        )
        self.assertEqual(response.status_code, 200)
        manager.inject_manual_cookies.assert_awaited_once_with("abc123", "")


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
        # Pin to "no password/token configured" by default -- individual
        # tests override with their own patch when they need a password
        # set, rather than depending on whatever the host machine has.
        self._pw_patcher = patch("src.web.app._get_password", return_value="")
        self._pw_patcher.start()
        self.addCleanup(self._pw_patcher.stop)
        self._token_patcher = patch("src.web.app._get_bot_token", return_value="")
        self._token_patcher.start()
        self.addCleanup(self._token_patcher.stop)
        self.client = TestClient(app_module.app)
        self.addCleanup(browser_login.set_active_manager, None)

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
        browser_login.set_active_manager(None)
        with (
            self.assertRaises(WebSocketDisconnect) as ctx,
            self.client.websocket_connect(
                "/api/login/browser/ws",
                headers={"origin": "http://testserver", "host": "testserver"},
            ),
        ):
            pass
        self.assertEqual(ctx.exception.code, 4404)

    def test_closes_4404_when_the_active_manager_has_no_live_session(self):
        # websocket_port is None once stop() has run -- the route must not
        # reach into ._session and crash on the TOCTOU window.
        manager = _fake_manager()
        manager.websocket_port = None
        browser_login.set_active_manager(manager)
        with (
            self.assertRaises(WebSocketDisconnect) as ctx,
            self.client.websocket_connect(
                "/api/login/browser/ws",
                headers={"origin": "http://testserver", "host": "testserver"},
            ),
        ):
            pass
        self.assertEqual(ctx.exception.code, 4404)


class TestBrowserLoginManagerIsSharedAcrossLayers(unittest.IsolatedAsyncioTestCase):
    """
    The regression this whole file exists to prevent: the auth flow used to
    drive a LOCAL BrowserLoginManager while src/web/app.py held a separate
    module-level one that only its (now deleted) /start endpoint ever
    assigned. The result was a feature that could never work end-to-end --
    the noVNC panel opened, the WS route found no manager and closed 4404,
    and Cancel cancelled nothing while the real Chromium ran on for the full
    10-minute timeout. These tests pin the single-owner invariant.
    """

    async def asyncSetUp(self):
        self.addCleanup(browser_login.set_active_manager, None)

    async def test_auth_flow_publishes_its_manager_and_clears_it_afterwards(self):
        mock_twitch = MagicMock()
        mock_twitch.gui.login.start_browser_login = AsyncMock()
        auth_state = _AuthState(mock_twitch)

        manager = _fake_manager()
        observed: list[object] = []
        parked = asyncio.Event()
        release = asyncio.Event()

        async def wait_for_cookie(*args, **kwargs):
            observed.append(browser_login.get_active_manager())
            parked.set()
            await release.wait()
            raise BrowserLoginCancelled()

        manager.wait_for_cookie = AsyncMock(side_effect=wait_for_cookie)

        self.assertIsNone(browser_login.get_active_manager())

        with patch("src.auth.browser_login.BrowserLoginManager", return_value=manager):
            task = asyncio.ensure_future(auth_state._browser_login())
            await asyncio.wait_for(parked.wait(), timeout=5)

            # While the auth flow is awaiting the cookie, the module-level
            # accessor every other layer reads returns THAT manager.
            self.assertIs(browser_login.get_active_manager(), manager)
            self.assertIs(observed[0], manager)

            release.set()
            # The retry loop sleeps before offering a fresh browser; stop it
            # there. The finally block has already run by then.
            await asyncio.sleep(0)
            for _ in range(50):
                if browser_login.get_active_manager() is None:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        manager.stop.assert_awaited()
        self.assertIsNone(browser_login.get_active_manager())

    async def test_cancel_route_and_ws_route_act_on_the_auth_flows_manager(self):
        mock_twitch = MagicMock()
        mock_twitch.gui.login.start_browser_login = AsyncMock()
        auth_state = _AuthState(mock_twitch)

        manager = _fake_manager(port=6993)
        parked = asyncio.Event()
        release = asyncio.Event()

        async def wait_for_cookie(*args, **kwargs):
            parked.set()
            await release.wait()
            raise BrowserLoginCancelled()

        manager.wait_for_cookie = AsyncMock(side_effect=wait_for_cookie)

        mock_reader = AsyncMock()
        mock_reader.read = AsyncMock(return_value=b"")
        mock_writer = MagicMock()
        mock_writer.wait_closed = AsyncMock()
        open_connection = AsyncMock(return_value=(mock_reader, mock_writer))

        with (
            patch("src.auth.browser_login.BrowserLoginManager", return_value=manager),
            patch("src.web.app._is_setup_done", return_value=True),
            patch("src.web.app._get_password", return_value=""),
            patch("src.web.app._get_bot_token", return_value=""),
        ):
            task = asyncio.ensure_future(auth_state._browser_login())
            await asyncio.wait_for(parked.wait(), timeout=5)

            # TestClient drives the app on its own portal thread; the auth
            # flow stays parked on `release` in this loop meanwhile.
            client = TestClient(app_module.app)

            with patch("asyncio.open_connection", open_connection):
                with client.websocket_connect(
                    "/api/login/browser/ws",
                    headers={"origin": "http://testserver", "host": "testserver"},
                ):
                    pass

            response = client.post("/api/login/browser/cancel")
            self.assertEqual(response.status_code, 200)

            # The cancel route reached the very instance the auth flow is
            # awaiting -- not a second, unrelated manager.
            manager.cancel.assert_called_once()
            # ...and the WS proxy dialled that same instance's port.
            open_connection.assert_awaited_once()
            self.assertEqual(open_connection.await_args.args[1], 6993)
            mock_writer.close.assert_called_once()
            mock_writer.wait_closed.assert_awaited_once()

            release.set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


class TestLoginFormManagerBrowserLoginStatus(unittest.IsolatedAsyncioTestCase):
    """
    start_browser_login() used to set the websocket port and THEN call
    update(), which unconditionally clears it again -- so get_status() (the
    payload behind /api/status and the socket-connect snapshot) never carried
    browser_login_ready and only a dashboard that happened to already be
    connected the instant the Socket.IO event fired ever showed the panel.
    On a fresh install nobody is connected yet at that point.
    """

    async def test_get_status_reports_browser_login_ready_after_start(self):
        from src.web.managers.login import LoginFormManager

        broadcaster = MagicMock()
        broadcaster.emit = AsyncMock()
        manager = LoginFormManager(broadcaster, MagicMock())

        self.assertNotIn("browser_login_ready", manager.get_status())

        await manager.start_browser_login(6990)

        status = manager.get_status()
        self.assertEqual(
            status["browser_login_ready"], {"websocket_path": "/api/login/browser/ws"}
        )
        self.assertIsNone(status["user_id"])
        # ...and the "login required" status update still landed.
        self.assertEqual(status["status"], _.t["login"]["status"]["required"])

        # Let update()'s fire-and-forget broadcast task run before the loop
        # closes, so the test doesn't leak a pending task warning.
        await asyncio.sleep(0)

    async def test_a_later_status_update_clears_the_panel_again(self):
        from src.web.managers.login import LoginFormManager

        broadcaster = MagicMock()
        broadcaster.emit = AsyncMock()
        manager = LoginFormManager(broadcaster, MagicMock())

        await manager.start_browser_login(6990)
        manager.update(_.t["login"]["status"]["logged_in"], 12345, "someone")
        self.assertNotIn("browser_login_ready", manager.get_status())
        await asyncio.sleep(0)


class TestBrowserLoginFailuresDoNotKillTheApp(unittest.IsolatedAsyncioTestCase):
    """
    _browser_login() had a try/finally but no except, so a timeout, a cancel
    or a missing system dependency propagated all the way into __main__'s
    generic `except Exception` handler and exited the process. It must
    instead surface as dashboard status and offer a fresh login browser.
    """

    def setUp(self):
        self.addCleanup(browser_login.set_active_manager, None)

    async def _run_until_second_attempt(self, first_exception):
        mock_twitch = MagicMock()
        mock_twitch.gui.login.start_browser_login = AsyncMock()
        mock_twitch.gui.login.update = MagicMock()
        auth_state = _AuthState(mock_twitch)

        attempts = []

        async def wait_for_cookie(*args, **kwargs):
            attempts.append(len(attempts))
            if len(attempts) == 1:
                raise first_exception
            return {"auth-token": "token-from-retry", "unique_id": "device-abc"}

        def make_manager():
            manager = _fake_manager()
            manager.wait_for_cookie = AsyncMock(side_effect=wait_for_cookie)
            return manager

        with (
            patch("src.auth.browser_login.BrowserLoginManager", side_effect=make_manager),
            patch("src.auth.auth_state.BROWSER_LOGIN_RETRY_DELAY_SEC", 0),
        ):
            token = await asyncio.wait_for(auth_state._browser_login(), timeout=5)

        return token, attempts, mock_twitch.gui.login.update

    async def test_timeout_is_retried_with_a_fresh_browser(self):
        from src.auth.browser_login import BrowserLoginTimeout

        token, attempts, update = await self._run_until_second_attempt(BrowserLoginTimeout())
        self.assertEqual(token, "token-from-retry")
        self.assertEqual(len(attempts), 2)
        self.assertIn(
            _.t["login"]["status"]["timed_out"],
            [call.args[0] for call in update.call_args_list],
        )

    async def test_cancel_is_retried_with_a_fresh_browser(self):
        token, attempts, update = await self._run_until_second_attempt(BrowserLoginCancelled())
        self.assertEqual(token, "token-from-retry")
        self.assertIn(
            _.t["login"]["status"]["cancelled"],
            [call.args[0] for call in update.call_args_list],
        )

    async def test_missing_system_dependencies_are_retried_not_fatal(self):
        from src.auth.browser_login import BrowserLoginUnavailable

        mock_twitch = MagicMock()
        mock_twitch.gui.login.start_browser_login = AsyncMock()
        mock_twitch.gui.login.update = MagicMock()
        auth_state = _AuthState(mock_twitch)

        calls = []

        def make_manager():
            manager = _fake_manager()

            async def start():
                calls.append(1)
                if len(calls) == 1:
                    raise BrowserLoginUnavailable("no chromium binary")
                return 6993

            manager.start = AsyncMock(side_effect=start)
            manager.wait_for_cookie = AsyncMock(
                return_value={"auth-token": "token-after-install", "unique_id": "device-abc"}
            )
            return manager

        with (
            patch("src.auth.browser_login.BrowserLoginManager", side_effect=make_manager),
            patch("src.auth.auth_state.BROWSER_LOGIN_RETRY_DELAY_SEC", 0),
        ):
            token = await asyncio.wait_for(auth_state._browser_login(), timeout=5)

        self.assertEqual(token, "token-after-install")
        self.assertEqual(len(calls), 2)
        self.assertIn(
            _.t["login"]["status"]["unavailable"],
            [call.args[0] for call in mock_twitch.gui.login.update.call_args_list],
        )


if __name__ == "__main__":
    unittest.main()
