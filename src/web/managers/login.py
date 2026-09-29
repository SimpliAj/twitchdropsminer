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

    def update(self, status: str, user_id: int | None, user_login: str | None = None):
        self._status = status
        self._user_id = user_id
        self._user_login = user_login
        self._browser_login_websocket_port = None
        asyncio.create_task(
            self._broadcaster.emit(
                "login_status", {"status": status, "user_id": user_id, "user_login": user_login}
            )
        )

    async def start_browser_login(self, websocket_port: int) -> None:
        """Tell connected dashboards a real-browser login session is ready
        to be viewed/interacted with, at the given local websockify port
        (the actual browser-facing WebSocket path is /api/login/browser/ws,
        see src/web/app.py -- this port is only used server-side to proxy
        into it).
        """
        self._browser_login_websocket_port = websocket_port
        self.update(_.t["login"]["status"]["required"], None)
        await self._broadcaster.emit("browser_login_ready", {"websocket_path": "/api/login/browser/ws"})

    def get_status(self) -> dict[str, Any]:
        """Get current login status for client synchronization."""
        result: dict[str, Any] = {"status": self._status, "user_id": self._user_id, "user_login": self._user_login}
        if self._browser_login_websocket_port is not None:
            result["browser_login_ready"] = {"websocket_path": "/api/login/browser/ws"}
        return result
