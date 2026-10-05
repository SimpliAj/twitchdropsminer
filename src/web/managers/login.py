"""Login form manager for handling Twitch authentication."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from src.i18n import _


if TYPE_CHECKING:
    from src.web.gui_manager import WebGUIManager
    from src.web.managers.broadcaster import WebSocketBroadcaster


class LoginFormManager:
    """Manages the real-browser login flow's UI state in the web interface.

    Coordinates between the web client (which embeds a noVNC view of the
    server-side browser -- see src/auth/browser_login.py) and _AuthState's
    _browser_login(), which drives that browser and waits for the resulting
    session cookie.
    """

    def __init__(self, broadcaster: WebSocketBroadcaster, manager: WebGUIManager):
        self._broadcaster = broadcaster
        self._manager = manager
        self._status = _.t["login"]["status"]["logged_out"]
        self._user_id: int | None = None
        self._user_login: str | None = None
        self._browser_login_websocket_port: int | None = None
        self._device_code: dict[str, str] | None = None

    def update(self, status: str, user_id: int | None, user_login: str | None = None):
        self._status = status
        self._user_id = user_id
        self._user_login = user_login
        self._browser_login_websocket_port = None
        self._device_code = None
        asyncio.create_task(
            self._broadcaster.emit(
                "login_status", {"status": status, "user_id": user_id, "user_login": user_login}
            )
        )

    async def start_browser_login(self, websocket_port: int) -> None:
        """Tell connected dashboards a real-browser login session is ready
        to be viewed/interacted with, at the given local x11vnc port (the
        actual browser-facing WebSocket path is /api/login/browser/ws,
        see src/web/app.py -- this port is only used server-side to proxy
        into it).
        """
        # update() clears _browser_login_websocket_port (every other status
        # transition means the login panel is gone), so the port has to be
        # set AFTER it -- otherwise get_status() never reports
        # browser_login_ready and only a dashboard that happened to already
        # be connected when the Socket.IO event fired would ever show the
        # panel. On a fresh install nobody is connected yet at this point.
        self.update(_.t["login"]["status"]["required"], None)
        self._browser_login_websocket_port = websocket_port
        await self._broadcaster.emit("browser_login_ready", {"websocket_path": "/api/login/browser/ws"})

    async def ask_enter_code(self, url: str, code: str) -> None:
        """Show the device-code login: the user enters `code` at `url` on any device.

        One login_status event carries the code itself -- a separate event raced
        with the plain status update and the dashboard could end up hiding it."""
        self._status = _.t["login"]["status"]["waiting_auth"]
        self._user_id = None
        self._user_login = None
        self._browser_login_websocket_port = None
        self._device_code = {"url": url, "code": code}
        await self._broadcaster.emit(
            "login_status",
            {"status": self._status, "user_id": None, "user_login": None, "device_code": self._device_code},
        )

    async def start_browser_login_on_real_display(self) -> None:
        """Like start_browser_login, but for a login window that opened
        directly on the host's own real desktop display (see
        BrowserLoginManager._detect_real_display) instead of a virtual one
        proxied over noVNC -- there's no panel to show, just a status
        telling the user where to look."""
        self.update(_.t["login"]["browser_login"]["on_real_display"], None)

    def get_status(self) -> dict[str, Any]:
        """Get current login status for client synchronization."""
        result: dict[str, Any] = {"status": self._status, "user_id": self._user_id, "user_login": self._user_login}
        if self._browser_login_websocket_port is not None:
            result["browser_login_ready"] = {"websocket_path": "/api/login/browser/ws"}
        if self._device_code is not None:
            result["device_code"] = self._device_code
        return result
