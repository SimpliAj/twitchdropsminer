"""Start-up wiring of the Windows desktop build, kept out of ``src/__main__.py``.

Only used when running from the packaged app (``IS_FROZEN``); source and Docker runs never import
or call anything here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

from src.config.paths import IS_FROZEN

from .autostart import AutostartManager
from .instance import SingleInstanceGuard
from .login_gate import login_gate
from .runtime import (
    RESTARTED_ENV,
    DashboardWindow,
    DesktopOptions,
    configured_port,
    default_host,
    ensure_std_streams,
    note_startup,
    open_dashboard,
    open_dashboard_when_ready,
    port_in_use,
    register_restart_hook,
    request_quit,
    show_error,
    wait_for_parent_exit,
)


logger = logging.getLogger("TwitchDrops")


class DesktopLauncher:
    """Single-instance check before start-up, tray + dashboard window once the miner runs."""

    def __init__(self, options: DesktopOptions, restarted: bool, guard: SingleInstanceGuard) -> None:
        self.options = options
        self._restarted = restarted
        self._guard = guard  # holds the instance mutex for the life of the process
        self._tray: Any = None
        self._window: DashboardWindow | None = None

    @classmethod
    def prepare(cls) -> DesktopLauncher:
        """Before logging is configured: usable std streams, one instance per user session.

        Exits the process (after showing the running instance's dashboard) if one is running."""
        ensure_std_streams()  # windowed apps have no stdout/stderr; logging/uvicorn need them
        options = DesktopOptions.parse()
        wait_for_parent_exit()  # no-op unless we were just started by an in-app restart
        restarted = os.environ.pop(RESTARTED_ENV, "") == "1"  # the user asked for this restart
        port = configured_port()
        guard = SingleInstanceGuard(port)
        if not guard.acquire():
            note_startup(f"another instance already owns port {port}; showing its dashboard and exiting")
            if options.open_browser:
                open_dashboard(port, not options.browser_tab)
            sys.exit(0)
        if port_in_use(port):
            # Without this the web server would fail to start, which ends the whole app and, with no
            # console, looks like "it opened and vanished" -- while the window shows whatever
            # program owns the port (e.g. the Docker version of the miner).
            message = (
                f"Port {port} is already in use by another program (for example the Docker "
                "version of the miner).\n\nClose that program, or start this app with a "
                "different port by setting the TDM_PORT environment variable (for example 8081)."
            )
            note_startup(message.replace("\n\n", " "))
            show_error(message)
            sys.exit(1)
        return cls(options, restarted, guard)

    @property
    def host(self) -> str:
        return default_host()

    def start(self, client: Any, loop: asyncio.AbstractEventLoop, port: int) -> None:
        """Tray icon, run-at-login repair and the dashboard window, once the server is starting."""
        options = self.options
        logger.info(f"Process pid={os.getpid()} frozen={IS_FROZEN} args={sys.argv[1:]}")
        self._window = DashboardWindow(port, app_window=not options.browser_tab)
        if options.minimized:
            logger.info("Started minimized (Windows sign-in): staying in the tray, no window opened")
        AutostartManager().repair_if_stale()
        if options.use_tray:
            from .tray import TrayIcon

            login_gate.enabled = True  # a login prompt may now wait for the dashboard's button
            if options.minimized and not self._restarted:
                login_gate.arm()  # started by Windows at sign-in: no login window unasked

            def _cancel_login() -> None:
                from src.auth import browser_login

                login_gate.release()
                active_login = browser_login.get_active_manager()
                if active_login is not None:
                    active_login.cancel()

            self._tray = TrayIcon(
                open_dashboard=self._window.open,
                quit_app=lambda: request_quit(client.close, _cancel_login, loop),
                autostart=AutostartManager(),
            )
            self._tray.start()
            register_restart_hook(self._tray.stop)  # no ghost tray icon after an in-app restart
        if options.open_browser and not self._restarted:
            asyncio.create_task(open_dashboard_when_ready(port, opener=self._window.open))

    def stop(self) -> None:
        if self._tray is not None:
            self._tray.stop()
