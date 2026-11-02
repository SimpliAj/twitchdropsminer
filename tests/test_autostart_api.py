import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from src.desktop.autostart import AutostartError
from src.web import app as web_app


def _manager(supported=True, status=None, set_enabled_error=None):
    manager = MagicMock()
    manager.supported = supported
    manager.status.return_value = status or {"supported": supported, "enabled": False}
    if set_enabled_error:
        manager.set_enabled.side_effect = set_enabled_error
    return manager


class AutostartApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_reports_manager_status(self):
        manager = _manager(status={"supported": True, "enabled": True})
        with patch.object(web_app, "AutostartManager", return_value=manager):
            self.assertEqual(await web_app.get_autostart(), {"supported": True, "enabled": True})

    async def test_post_enables_and_returns_new_status(self):
        manager = _manager(status={"supported": True, "enabled": True})
        with patch.object(web_app, "AutostartManager", return_value=manager):
            result = await web_app.set_autostart(web_app.AutostartRequest(enabled=True))
        manager.set_enabled.assert_called_once_with(True)
        self.assertTrue(result["enabled"])

    async def test_post_is_rejected_off_windows(self):
        manager = _manager(supported=False)
        with patch.object(web_app, "AutostartManager", return_value=manager):
            with self.assertRaises(HTTPException) as ctx:
                await web_app.set_autostart(web_app.AutostartRequest(enabled=True))
        self.assertEqual(ctx.exception.status_code, 400)
        manager.set_enabled.assert_not_called()

    async def test_registry_failure_becomes_a_500_with_the_reason(self):
        manager = _manager(set_enabled_error=AutostartError("denied"))
        with patch.object(web_app, "AutostartManager", return_value=manager):
            with self.assertRaises(HTTPException) as ctx:
                await web_app.set_autostart(web_app.AutostartRequest(enabled=False))
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertIn("denied", ctx.exception.detail)

    async def test_login_request_is_refused_outside_the_tray_app(self):
        with patch.object(web_app.login_gate, "enabled", False):
            with self.assertRaises(HTTPException) as ctx:
                await web_app.request_browser_login()
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_login_request_releases_a_held_back_login(self):
        with (
            patch.object(web_app.login_gate, "enabled", True),
            patch.object(type(web_app.login_gate), "pending", True, create=True),
            patch.object(web_app.login_gate, "release") as release,
        ):
            self.assertEqual(await web_app.request_browser_login(), {"success": True, "pending": False})
        release.assert_called_once()

    async def test_login_request_does_nothing_when_no_login_is_waiting(self):
        with (
            patch.object(web_app.login_gate, "enabled", True),
            patch.object(type(web_app.login_gate), "pending", False, create=True),
            patch.object(web_app.login_gate, "release") as release,
        ):
            self.assertEqual(await web_app.request_browser_login(), {"success": False, "pending": False})
        release.assert_not_called()

    async def test_self_update_explains_how_to_update_the_packaged_app(self):
        with patch.object(web_app, "IS_FROZEN", True), patch.object(web_app, "_is_docker", return_value=False):
            result = await web_app.self_update()
        self.assertFalse(result["success"])
        self.assertTrue(result["frozen"])
        self.assertIn("releases", result["log"])


class AutostartFrontendTests(unittest.TestCase):
    def test_card_uses_safe_dom_and_is_hidden_until_supported(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        html = (root / "web" / "index.html").read_text(encoding="utf-8")
        js = (root / "web" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="autostart-card"', html)
        self.assertIn("display:none", html.split('id="autostart-card"')[1].split(">")[0])
        body = js.split("async function initAutostartCard()")[1].split("async function dumpDiagnostics")[0]
        self.assertNotIn("innerHTML", body)  # translated text must use textContent


if __name__ == "__main__":
    unittest.main()
