"""Desktop integration for the Windows build: run-at-login, tray icon, single instance."""

from __future__ import annotations

from .autostart import AutostartError, AutostartManager
from .instance import SingleInstanceGuard
from .launcher import DesktopLauncher
from .login_gate import LoginGate, login_gate
from .runtime import (
    DashboardWindow,
    DesktopOptions,
    default_host,
    ensure_std_streams,
    open_dashboard,
    open_dashboard_when_ready,
    register_restart_hook,
    request_quit,
    restart_process,
    wait_for_parent_exit,
)


__all__ = [
    "AutostartError",
    "AutostartManager",
    "DashboardWindow",
    "DesktopLauncher",
    "DesktopOptions",
    "LoginGate",
    "SingleInstanceGuard",
    "default_host",
    "ensure_std_streams",
    "login_gate",
    "open_dashboard",
    "open_dashboard_when_ready",
    "register_restart_hook",
    "request_quit",
    "restart_process",
    "wait_for_parent_exit",
]
