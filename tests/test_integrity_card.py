import json
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unused_port
from fastapi.testclient import TestClient

from src.auth import browser_login
from src.auth.auth_state import _AuthState
from src.config.settings import default_settings
from src.web import app as app_module
from src.web.managers.settings import SettingsManager

KEY = "k-9f3a-very-secret-helper-key"


class FakeSettings:
    def __init__(self):
        self.__dict__.update(json.loads(json.dumps(default_settings)))

    def save(self):
        pass


class IntegrityCardCase(unittest.TestCase):
    def setUp(self):
        self.emit = AsyncMock()
        self.settings = FakeSettings()
        manager = SettingsManager(MagicMock(emit=self.emit), self.settings, MagicMock())
        self.gui = MagicMock(settings=manager)
        self.auth = _AuthState(MagicMock())
        self.auth.access_token, self.auth.device_id = "twitch-session", "dev"
        self.twitch = MagicMock(_auth_state=self.auth)
        self.bot_token = ""
        for target, value in (
            ("_is_setup_done", True), ("_get_password", ""),
        ):
            p = patch(f"src.web.app.{target}", return_value=value)
            p.start()
            self.addCleanup(p.stop)
        for p in (
            patch("src.web.app._get_bot_token", side_effect=lambda: self.bot_token),
            patch.object(app_module, "gui_manager", self.gui),
            patch.object(app_module, "twitch_client", self.twitch),
            patch.dict("os.environ", {}, clear=False),
        ):
            p.start()
            self.addCleanup(p.stop)
        for var in (browser_login.INTEGRITY_HELPER_URL_ENV, browser_login.INTEGRITY_HELPER_KEY_ENV):
            import os
            os.environ.pop(var, None)
        self.client = TestClient(app_module.app, client=("127.0.0.1", 50000))

    def save(self, **fields):
        return self.client.post("/api/settings", json=fields)


class TestHelperSettings(IntegrityCardCase):
    def test_key_never_leaves_the_server(self):
        response = self.save(integrity_helper_url="https://h.example.com", integrity_helper_key=KEY)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.settings.integrity_helper_key, KEY)
        for body in (response.text, self.client.get("/api/settings").text):
            self.assertNotIn(KEY, body)
            self.assertNotIn("integrity_helper_key\"", body)
        self.assertTrue(self.client.get("/api/settings").json()["integrity_helper_key_set"])
        self.assertTrue(self.emit.called)
        for call in self.emit.call_args_list:
            self.assertNotIn(KEY, json.dumps(call.args))

    def test_empty_key_keeps_the_saved_key_and_clearing_the_url_drops_it(self):
        self.save(integrity_helper_url="https://h.example.com", integrity_helper_key=KEY)
        self.save(integrity_helper_url="https://other.example.com", integrity_helper_key="")
        self.assertEqual(self.settings.integrity_helper_key, KEY)
        self.save(integrity_helper_url="")
        self.assertEqual((self.settings.integrity_helper_url, self.settings.integrity_helper_key), ("", ""))
        self.assertFalse(self.client.get("/api/settings").json()["integrity_helper_key_set"])

    def test_unsafe_plain_http_is_refused(self):
        response = self.save(integrity_helper_url="http://8.8.8.8:8099", integrity_helper_key=KEY)
        self.assertEqual(response.status_code, 400)
        self.assertIn("https", response.json()["detail"])
        self.assertEqual(self.settings.integrity_helper_url, "")

    def test_saving_a_helper_drops_the_cached_token(self):
        self.auth._integrity_token = "old"
        self.save(integrity_helper_url="http://192.168.0.5:8099", integrity_helper_key=KEY)
        self.assertIsNone(self.auth._integrity_token)

    def test_unrelated_save_keeps_the_cached_token(self):
        self.auth._integrity_token = "old"
        self.save(dark_mode=True)
        self.assertEqual(self.auth._integrity_token, "old")

    def test_bot_and_fleet_headers_cannot_change_the_helper(self):
        self.bot_token = "bot-secret"
        headers = {"X-Bot-Token": "bot-secret"}
        for h in (headers, {"X-Fleet-Password": "x"}):
            response = self.client.post("/api/settings", json={"integrity_helper_url": "https://evil.example"}, headers=h)
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.settings.integrity_helper_url, "")
        self.assertEqual(self.client.post("/api/settings", json={"dark_mode": True}, headers=headers).status_code, 200)

    def test_new_address_needs_its_own_key(self):
        self.assertEqual(self.save(integrity_helper_url="http://192.168.0.5:8099", integrity_helper_key="k" * 20).status_code, 200)
        self.assertEqual(self.save(integrity_helper_url="http://192.168.0.6:8099").status_code, 400)

    def test_no_password_over_the_network_is_refused(self):
        remote = TestClient(app_module.app, client=("192.168.0.9", 50000))
        r = remote.post("/api/settings", json={"integrity_helper_url": "http://192.168.0.5:8099"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(remote.post("/api/integrity/test-helper", json={"url": "", "key": ""}).status_code, 403)

    def test_env_wins_over_saved_settings(self):
        self.save(integrity_helper_url="https://saved.example.com", integrity_helper_key=KEY)
        self.assertEqual(browser_login.resolve_integrity_helper("https://saved.example.com", KEY),
                         ("https://saved.example.com", KEY, False))
        with patch.dict("os.environ", {"TDM_INTEGRITY_HELPER_URL": "http://10.0.0.2:8099", "TDM_INTEGRITY_HELPER_KEY": "envkey"}):
            self.assertEqual(browser_login.resolve_integrity_helper("https://saved.example.com", KEY),
                             ("http://10.0.0.2:8099", "envkey", True))
            self.assertTrue(self.client.get("/api/integrity/status").json()["helper_from_env"])


class TestAcquireUsesSettings(unittest.IsolatedAsyncioTestCase):
    async def test_saved_helper_is_used_and_env_overrides_it(self):
        expiry = datetime(2030, 1, 1, tzinfo=timezone.utc)
        via = AsyncMock(return_value=("t", expiry))
        with patch("src.auth.browser_login._mint_via_helper", new=via), patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("TDM_INTEGRITY_HELPER_URL", None)
            await browser_login.acquire_integrity_token("a", "d", "https://saved.example.com", KEY)
            via.assert_awaited_with("https://saved.example.com", "a", "d", KEY)
            with patch.dict("os.environ", {"TDM_INTEGRITY_HELPER_URL": "https://env.example.com", "TDM_INTEGRITY_HELPER_KEY": "e"}):
                await browser_login.acquire_integrity_token("a", "d", "https://saved.example.com", KEY)
            via.assert_awaited_with("https://env.example.com", "a", "d", "e")


class TestHelperTestEndpoint(IntegrityCardCase):
    def test_reports_each_failure_plainly_and_never_returns_the_token(self):
        expiry = datetime(2030, 1, 1, tzinfo=timezone.utc)
        cases = [
            ((None, "unauthorized"), False, "refused the key"),
            ((None, "unreachable"), False, "Could not reach"),
            ((None, "unsafe"), False, "https"),
            ((None, "http_502"), False, "HTTP 502"),
            ((("SECRET-TOKEN", expiry), "ok"), True, "Connected"),
        ]
        for result, ok, fragment in cases:
            with patch("src.auth.browser_login.request_helper_mint", new=AsyncMock(return_value=result)) as mint:
                response = self.client.post("/api/integrity/test-helper", json={"url": "https://h.example.com", "key": KEY})
            body = response.json()
            self.assertEqual(body["ok"], ok, fragment)
            self.assertIn(fragment, body["message"])
            self.assertNotIn("SECRET-TOKEN", response.text)
            mint.assert_awaited_once_with("https://h.example.com", KEY, "twitch-session", "dev")
        self.assertIsNone(self.auth._integrity_token)

    def test_saved_key_only_goes_to_the_saved_url(self):
        self.save(integrity_helper_url="https://h.example.com", integrity_helper_key=KEY)
        mint = AsyncMock(return_value=(None, "unauthorized"))
        with patch("src.auth.browser_login.request_helper_mint", new=mint):
            self.client.post("/api/integrity/test-helper", json={"url": "https://h.example.com", "key": ""})
            self.client.post("/api/integrity/test-helper", json={"url": "https://elsewhere.example.com", "key": ""})
        self.assertEqual([c.args[1] for c in mint.await_args_list], [KEY, ""])

    def test_refused_for_bot_and_fleet_headers_and_needs_a_session(self):
        self.bot_token = "bot-secret"
        with patch("src.auth.browser_login.request_helper_mint", new=AsyncMock()) as mint:
            for h in ({"X-Bot-Token": "bot-secret"}, {"X-Fleet-Password": "x"}):
                response = self.client.post("/api/integrity/test-helper", json={"url": "https://h.example.com"}, headers=h)
                self.assertEqual(response.status_code, 403)
            mint.assert_not_awaited()
        del self.auth.access_token
        response = self.client.post("/api/integrity/test-helper", json={"url": "https://h.example.com"})
        self.assertFalse(response.json()["ok"])


class TestRequestHelperMint(AioHTTPTestCase):
    async def get_application(self):
        async def mint(request):
            if request.headers.get("Authorization") != "Bearer good":
                return web.json_response({}, status=401)
            return web.json_response({"token": "itok", "expires_at": 1893456000})

        app = web.Application()
        app.router.add_post("/mint", mint)
        return app

    async def test_reasons(self):
        url = f"http://127.0.0.1:{self.server.port}"
        self.assertEqual((await browser_login.request_helper_mint(url, "bad", "t", "d"))[1], "unauthorized")
        result, reason = await browser_login.request_helper_mint(url, "good", "t", "d")
        self.assertEqual((result[0], reason), ("itok", "ok"))
        closed = f"http://127.0.0.1:{unused_port()}"
        self.assertEqual((await browser_login.request_helper_mint(closed, "good", "t", "d"))[1], "unreachable")
        self.assertEqual((await browser_login.request_helper_mint("http://8.8.8.8", "good", "t", "d"))[1], "unsafe")


class TestIntegrityStatus(IntegrityCardCase):
    def status(self, machine="x86_64", platform="linux"):
        with patch("platform.machine", return_value=machine), patch.object(app_module.sys, "platform", platform):
            return self.client.get("/api/integrity/status").json()

    def test_states(self):
        self.assertEqual(self.status()["state"], "none")
        self.auth._integrity_token = "t"
        self.auth._integrity_minted_at = datetime.now(timezone.utc)
        self.assertEqual(self.status()["state"], "ok")
        self.auth.reject_integrity_token()
        rejected = self.status()
        self.assertEqual(rejected["state"], "rejected")
        self.assertTrue(rejected["rejected_at"])
        self.assertTrue(rejected["next_attempt_at"])  # quickly rejected -> cooldown
        self.auth._adopt_integrity_token(("fresh", datetime(2030, 1, 1, tzinfo=timezone.utc)))
        self.assertEqual(self.status()["state"], "ok")

    def test_arm_hint_logic(self):
        self.assertFalse(self.status("x86_64")["arm_hint"])
        for machine in ("aarch64", "arm64", "armv7l"):
            self.assertTrue(self.status(machine)["arm_hint"], machine)
        self.assertFalse(self.status("arm64", "darwin")["arm_hint"])
        self.save(integrity_helper_url="https://h.example.com", integrity_helper_key=KEY)
        info = self.status("aarch64")
        self.assertFalse(info["arm_hint"])
        self.assertEqual(info["source"], "helper")
        self.assertNotIn(KEY, json.dumps(info))
