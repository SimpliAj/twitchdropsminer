"""Process-level helpers for the desktop (Windows and macOS) builds."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from src.config.paths import DATA_DIR, IS_FROZEN


logger = logging.getLogger("TwitchDrops")

# Called just before the process is replaced (e.g. to remove the tray icon, which Windows
# otherwise leaves behind as a dead "ghost" icon that does nothing when clicked).
_restart_hooks: list[Callable[[], None]] = []
_restart_started = threading.Event()

# Env var set on the replacement process during a restart: the PID of the process being replaced.
RESTART_PARENT_ENV = "TDM_RESTART_PARENT_PID"

# Set on the replacement process of an in-app restart: the user is at the PC (they asked for it),
# so it neither waits for them to open the dashboard nor opens a second dashboard window.
RESTARTED_ENV = "TDM_RESTARTED"

_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_SYNCHRONIZE = 0x00100000


@dataclass(frozen=True)
class DesktopOptions:
    """Desktop-only command line flags. Unknown flags (-v, --dump ...) are left alone."""

    minimized: bool = False  # started by Windows at login: tray only, no browser tab
    no_browser: bool = False
    no_tray: bool = False
    browser_tab: bool = False  # use a normal browser tab instead of a standalone app window

    @classmethod
    def parse(cls, argv: list[str] | None = None) -> DesktopOptions:
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--minimized", action="store_true")
        parser.add_argument("--no-browser", action="store_true")
        parser.add_argument("--no-tray", action="store_true")
        parser.add_argument("--browser-tab", action="store_true")
        known, _unknown = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
        return cls(known.minimized, known.no_browser, known.no_tray, known.browser_tab)

    @property
    def open_browser(self) -> bool:
        # Only the desktop build opens a tab by itself; source runs and Docker never did.
        return IS_FROZEN and not (self.minimized or self.no_browser)

    @property
    def use_tray(self) -> bool:
        # The tray is only worth having when there is no console to close: i.e. the
        # windowed Windows build. Source runs and Docker keep their old behaviour.
        return IS_FROZEN and sys.platform == "win32" and not self.no_tray


def ensure_std_streams() -> None:
    """Give windowed (no console) builds real stdout/stderr objects.

    PyInstaller's windowed mode leaves sys.stdout/sys.stderr as None, which crashes logging
    handlers and uvicorn's log formatter (it calls ``sys.stdout.isatty()``).
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115


def dashboard_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/"


def find_app_browser() -> str | None:
    """Edge (on every Windows 10/11) or Chrome, for a standalone app-style window."""
    if sys.platform == "darwin":
        for name in ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium"):
            for root in ("/Applications", os.path.expanduser("~/Applications")):
                path = os.path.join(root, f"{name}.app", "Contents", "MacOS", name)
                if os.path.exists(path):
                    return path
        return None
    if sys.platform != "win32":
        return None
    for vendor, app, exe in (("Microsoft", "Edge", "msedge.exe"), ("Google", "Chrome", "chrome.exe")):
        for env_name in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            base = os.environ.get(env_name)
            if base:
                path = os.path.join(base, vendor, app, "Application", exe)
                if os.path.exists(path):
                    return path
    return None


def open_dashboard(port: int, app_window: bool = False) -> subprocess.Popen | None:
    """Show the dashboard. With ``app_window`` (Windows build) it opens as its own window --
    no tabs or address bar -- using the Edge/Chrome already on the PC, in a profile of its own
    so the window is a process we can watch and Edge's session restore never reopens it. If no
    such browser is found, or it fails to start, the default browser is used (returns None)."""
    url = dashboard_url(port)
    note_startup(f"opening the dashboard at {url} (app window: {app_window})")
    if app_window:
        browser = find_app_browser()
        if browser:
            try:
                from src.auth.plain_chrome import clean_browser_env  # noqa: PLC0415

                return subprocess.Popen(  # noqa: S603
                    env=clean_browser_env(),
                    args=[
                        browser,
                        f"--app={url}",
                        f"--user-data-dir={DATA_DIR / 'dashboard-window'}",
                        "--no-first-run",
                        "--no-default-browser-check",
                    ]
                )
            except OSError:
                pass
    webbrowser.open(url)
    return None


class DashboardWindow:
    """Opens the dashboard window. Closing it (its X button) only closes the window: the miner
    keeps running in the tray. A second "Open dashboard" while the window is still open does
    not stack another one."""

    def __init__(self, port: int, app_window: bool = True) -> None:
        self._port = port
        self._app_window = app_window
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def open(self) -> None:
        with self._lock:
            if self.is_open:
                return
            self._proc = open_dashboard(self._port, self._app_window)


async def open_dashboard_when_ready(
    port: int, timeout: float = 30.0, opener: Callable[[], None] | None = None
) -> None:
    """Open the dashboard once the web server accepts connections (not before: the browser
    would otherwise land on a 'connection refused' page while the miner is still booting)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.5)
            continue
        writer.close()
        (opener or (lambda: open_dashboard(port)))()
        return


def note_startup(text: str) -> None:
    """Append a line to ``<data dir>/logs/startup.log`` (best effort), for start-up problems that
    happen before the normal log exists. Windows app only."""
    try:
        logs = DATA_DIR / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / "startup.log", "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} {text}\n")
    except OSError:
        pass


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """True if something already accepts connections on host:port."""
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def show_error(text: str, title: str = "Twitch Drops Miner") -> None:
    """Message box for a windowed app with no console to print to (Windows only, best effort)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes  # noqa: PLC0415

        ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)  # MB_ICONERROR
    except Exception:
        pass


def configured_port() -> int:
    """The port the miner serves on: TDM_PORT, else PORT, else 8080 (as in src/__main__.py)."""
    return int(os.environ.get("TDM_PORT") or os.environ.get("PORT") or 8080)


def default_host() -> str:
    """Bind address. The desktop build is local-only unless TDM_HOST says otherwise."""
    configured = (os.environ.get("TDM_HOST") or "").strip()
    if configured:
        return configured
    return "127.0.0.1" if IS_FROZEN else "0.0.0.0"  # noqa: S104  (Docker needs all interfaces)


def wait_for_parent_exit(timeout: float = 20.0) -> None:
    """After a restart, wait for the old process to release the port and the instance mutex."""
    raw = os.environ.pop(RESTART_PARENT_ENV, "")
    if not raw.isdigit() or sys.platform != "win32":
        return
    import ctypes  # noqa: PLC0415

    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(_SYNCHRONIZE, False, int(raw))
    if not handle:
        return  # already gone
    try:
        kernel32.WaitForSingleObject(handle, int(timeout * 1000))
    finally:
        kernel32.CloseHandle(handle)


def register_restart_hook(hook: Callable[[], None]) -> None:
    _restart_hooks.append(hook)


def request_quit(
    close: Callable[[], None],
    cancel_login: Callable[[], None],
    loop: asyncio.AbstractEventLoop,
    force_after: float = 10.0,
    exit_fn: Callable[[int], Any] = os._exit,
) -> threading.Timer:
    """Quit from the tray: ask the miner to stop and abort a pending/retrying Twitch login, then
    make sure the process really goes away. The graceful path normally ends the process on its
    own; the timer is the guarantee that Quit never silently does nothing."""

    def _graceful() -> None:
        close()
        cancel_login()

    loop.call_soon_threadsafe(_graceful)
    timer = threading.Timer(force_after, exit_fn, args=(0,))
    timer.daemon = True
    timer.start()
    return timer


def restart_process() -> None:
    """Replace this process with a fresh copy of itself.

    ``os.execv`` is unreliable on Windows (it starts a new process and exits the old one while
    both may still hold the port), so there the replacement is spawned detached and told to wait
    for this PID to disappear before it starts. Elsewhere the in-place re-exec is unchanged.
    """
    if IS_FROZEN and sys.platform == "darwin":
        # The bundle's executable replaces this process in place (same PID, so the port and the
        # instance lock are released by the exec itself); the bootloader must start from scratch.
        env = {**os.environ, RESTARTED_ENV: "1", "PYINSTALLER_RESET_ENVIRONMENT": "1"}
        logger.info(f"Restarting: re-executing {sys.executable}")
        os.execve(sys.executable, [sys.executable, *sys.argv[1:]], env)  # noqa: S606
        return
    if not IS_FROZEN or sys.platform != "win32":
        os.execv(sys.executable, [sys.executable] + sys.argv)  # noqa: S606
        return
    if _restart_started.is_set():
        logger.warning("Restart already in progress; ignoring a second request")
        return
    _restart_started.set()
    for hook in _restart_hooks:
        try:
            hook()
        except Exception:
            logger.debug("Restart hook failed", exc_info=True)
    args = [sys.executable] if IS_FROZEN else [sys.executable, *sys.argv[:1]]
    args += sys.argv[1:]
    env = {
        **os.environ,
        RESTART_PARENT_ENV: str(os.getpid()),
        RESTARTED_ENV: "1",
        "PYINSTALLER_RESET_ENVIRONMENT": "1",  # the copy is a new app, not a child of this one
    }
    # The packaged exe has no console to detach from, and a detached parent made every browser
    # it started inherit that odd state; only a console/source run needs DETACHED_PROCESS.
    flags = _CREATE_NEW_PROCESS_GROUP | (0 if IS_FROZEN else _DETACHED_PROCESS)
    logger.info(f"Restarting: pid {os.getpid()} starts a replacement ({args})")
    try:
        subprocess.Popen(  # noqa: S603
            args,
            env=env,
            creationflags=flags,
            close_fds=True,
        )
    except OSError:
        _restart_started.clear()
        logger.exception("Could not start the replacement process; staying up")
        raise
    time.sleep(0.2)
    os._exit(0)
