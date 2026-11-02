import asyncio
import unittest
from unittest.mock import MagicMock, patch

from src.desktop.login_gate import LoginGate
from src.web.managers import login as login_module
from src.web.managers.login import LoginFormManager


class LoginPayloadTests(unittest.IsolatedAsyncioTestCase):
    def _manager(self):
        broadcaster = MagicMock()

        async def emit(*_args, **_kwargs):
            return None

        broadcaster.emit.side_effect = emit
        with patch.object(login_module, "_", MagicMock(t={"login": {"status": {"logged_out": "out"}}})):
            return LoginFormManager(broadcaster, MagicMock()), broadcaster

    async def test_payload_is_unchanged_outside_the_windows_tray_app(self):
        manager, broadcaster = self._manager()
        with patch.object(login_module, "login_gate", LoginGate()):  # not enabled: Docker/Linux/source
            manager.update("Not logged in", None)
            await asyncio.sleep(0)
            self.assertNotIn("login_pending", broadcaster.emit.call_args.args[1])
            self.assertNotIn("login_pending", manager.get_status())

    async def test_pending_login_is_flagged_in_the_tray_app(self):
        manager, broadcaster = self._manager()
        gate = LoginGate()
        gate.enabled = True
        gate.arm()
        with patch.object(login_module, "login_gate", gate):
            manager.update("Not logged in", None)
            await asyncio.sleep(0)
            self.assertTrue(broadcaster.emit.call_args.args[1]["login_pending"])
            self.assertTrue(manager.get_status()["login_pending"])


if __name__ == "__main__":
    unittest.main()
