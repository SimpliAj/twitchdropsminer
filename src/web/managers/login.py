"""Login form manager for handling Twitch authentication."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from src.desktop.login_gate import login_gate
from src.i18n import _


if TYPE_CHECKING:
    from src.web.gui_manager import WebGUIManager
    from src.web.managers.broadcaster import WebSocketBroadcaster


class LoginFormManager:
    """Login status for the dashboard: logged in/out, or a
    login attempt that is waiting either in a local browser window or for the
    login helper."""

    def __init__(self, broadcaster: WebSocketBroadcaster, manager: WebGUIManager):
        self._broadcaster = broadcaster
        self._manager = manager
        self._status = _.t["login"]["status"]["logged_out"]
        self._user_id: int | None = None
        self._user_login: str | None = None
        self._login_helper: str | None = None

    def update(self, status: str, user_id: int | None, user_login: str | None = None):
        self._status = status
        self._user_id = user_id
        self._user_login = user_login
        self._login_helper = None
        payload: dict[str, Any] = {"status": status, "user_id": user_id, "user_login": user_login}
        if login_gate.pending:  # Windows tray build only
            payload["login_pending"] = True
        asyncio.create_task(self._broadcaster.emit("login_status", payload))

    async def start_browser_login_local(self) -> None:
        """TDM opened a browser window on this computer's own desktop."""
        # Only English.json is guaranteed to carry login.browser_login.
        text = _.t["login"].get("browser_login", {}).get(
            "on_real_display", "A browser window has opened on this computer -- log in to Twitch there."
        )
        # The window may be on a machine the user is not sitting at (dashboard
        # opened from a phone, TDM as a Windows service), so offer the helper too.
        await self._start_waiting(text, "local")

    async def start_browser_login_remote(self) -> None:
        """Headless host: the dashboard shows the login-helper command."""
        await self._start_waiting(_.t["login"]["status"]["required"], "remote")

    async def _start_waiting(self, status: str, mode: str) -> None:
        self._status = status
        self._user_id = None
        self._user_login = None
        self._login_helper = mode
        await self._broadcaster.emit("login_status", self.get_status())

    def get_status(self) -> dict[str, Any]:
        """Get current login status for client synchronization."""
        result: dict[str, Any] = {"status": self._status, "user_id": self._user_id, "user_login": self._user_login}
        if self._login_helper:
            result["login_helper"] = self._login_helper
        if login_gate.pending:  # Windows tray build only
            result["login_pending"] = True
        return result
