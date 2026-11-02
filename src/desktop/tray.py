"""System-tray icon for the Windows build (pystray, runs in its own thread)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from src.config.paths import WEB_DIR

from .autostart import AutostartError, AutostartManager


logger = logging.getLogger("TwitchDrops")


class TrayIcon:
    """Tray menu: open the dashboard, toggle run-at-login, quit.

    All callbacks arrive on pystray's thread, so ``quit`` must be thread-safe (the caller hands
    in ``loop.call_soon_threadsafe(client.close)``-style callables).
    """

    def __init__(
        self,
        open_dashboard: Callable[[], None],
        quit_app: Callable[[], None],
        autostart: AutostartManager,
        tooltip: str = "Twitch Drops Miner",
    ) -> None:
        self._open_dashboard = open_dashboard
        self._quit_app = quit_app
        self._autostart = autostart
        self._tooltip = tooltip
        self._icon: Any = None

    @staticmethod
    def _load_image() -> Any:
        from PIL import Image  # noqa: PLC0415

        candidate = Path(WEB_DIR) / "favicon.png"
        if candidate.exists():
            return Image.open(candidate).convert("RGBA")
        return Image.new("RGBA", (64, 64), (145, 70, 255, 255))  # Twitch purple fallback

    def _toggle_autostart(self, _icon: Any, _item: Any) -> None:
        try:
            self._autostart.set_enabled(not self._autostart.is_enabled())
        except AutostartError as exc:
            logger.warning(f"Tray: could not change run-at-startup: {exc}")

    def _on_quit(self, icon: Any, _item: Any) -> None:
        icon.visible = False
        self._quit_app()
        icon.stop()

    def start(self) -> bool:
        """Show the icon in the background. False if the tray could not be created."""
        try:
            import pystray  # noqa: PLC0415

            menu = pystray.Menu(
                pystray.MenuItem(
                    "Open dashboard", lambda *_: self._open_dashboard(), default=True
                ),
                pystray.MenuItem(
                    "Start with Windows",
                    self._toggle_autostart,
                    checked=lambda _item: self._autostart.is_enabled(),
                    visible=self._autostart.supported,
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._on_quit),
            )
            self._icon = pystray.Icon("TwitchDropsMiner", self._load_image(), self._tooltip, menu)
            self._icon.run_detached()
            return True
        except Exception as exc:  # no tray available must never stop the miner
            logger.warning(f"System tray unavailable: {exc}")
            self._icon = None
            return False

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                logger.debug("Tray icon stop failed", exc_info=True)
            self._icon = None
