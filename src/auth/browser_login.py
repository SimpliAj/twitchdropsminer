"""Server-side real-browser login for Twitch, replacing OAuth device-code auth.

See docs/superpowers/specs/2026-09-29-server-side-browser-login-design.md
for the full design rationale. In short: device-code login's only two
still-functional client IDs (MOBILE_WEB, SMARTBOX) either get hard-rejected
by the GQL gateway or mint a token Twitch scopes to a degraded campaign-
visibility tier (issues #15-#18) -- intrinsic to how the token was minted,
not fixable by any header/identity trick. A real www.twitch.tv/login
session, captured via an actual browser, does not have this problem: it's
the same kind of session a real logged-in user browsing Twitch normally
gets.

This module owns exactly one login attempt's lifecycle at a time: launch a
virtual X display (Xvfb), a real (non-headless) Chromium inside it via
Playwright, an x11vnc server exposing that display, and a websockify
bridge so a browser-based noVNC client can drive it interactively. The
caller (src/auth/auth_state.py's _browser_login) awaits wait_for_cookie()
for the resulting session cookie, then always calls stop() regardless of
outcome.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import time
from dataclasses import dataclass, field

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

logger = logging.getLogger("TwitchDrops")

LOGIN_URL = "https://www.twitch.tv/login"
COOKIE_DOMAIN = "twitch.tv"
COOKIE_NAME = "auth-token"
DEVICE_ID_COOKIE_NAME = "unique_id"
DEFAULT_TIMEOUT_SEC = 600  # 10 minutes
COOKIE_POLL_INTERVAL_SEC = 1.5
CHROMIUM_LAUNCH_TIMEOUT_SEC = 30

# Reserved ranges for this feature's own display/port numbers, used both to
# pick a free one at start() and to recognize this app's own leftover
# processes in sweep_orphaned_processes(). Only one login attempt is ever
# allowed at a time by design, but the range is wider than 1 so a slow-to-
# exit previous session doesn't block a new one from picking a free slot.
DISPLAY_NUMBER_RANGE_START = 90
DISPLAY_NUMBER_RANGE_SIZE = 10
VNC_PORT_RANGE_START = 5990
WEBSOCKIFY_PORT_RANGE_START = 6990


class BrowserLoginTimeout(Exception):
    """No successful login cookie appeared within the timeout."""


class BrowserLoginCancelled(Exception):
    """The login attempt was explicitly cancelled."""


class BrowserLoginUnavailable(Exception):
    """Required system dependencies (Xvfb/Chromium/x11vnc/websockify) are
    missing or failed to start."""


@dataclass
class _BrowserLoginSession:
    """Internal handle for one in-progress login attempt."""

    display_number: int
    websocket_port: int
    playwright: Playwright
    browser: Browser
    context: BrowserContext
    xvfb_process: asyncio.subprocess.Process
    x11vnc_process: asyncio.subprocess.Process
    websockify_process: asyncio.subprocess.Process
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)


class BrowserLoginManager:
    """Owns the lifecycle of a single real-browser Twitch login attempt.

    Callers are responsible for serializing attempts (see _AuthState's own
    lock) -- this class raises RuntimeError on a second concurrent start()
    but does not itself queue or lock.
    """

    def __init__(self) -> None:
        self._session: _BrowserLoginSession | None = None

    @property
    def in_progress(self) -> bool:
        return self._session is not None

    async def start(self) -> int:
        """Start a new login attempt: virtual display, real Chromium
        navigated to the Twitch login page, VNC + websocket bridge.

        Returns the local websockify TCP port the caller should proxy (see
        src/web/app.py's WS /api/login/browser/ws) for a noVNC client.

        Raises RuntimeError if a session is already in progress, or
        BrowserLoginUnavailable if any required process/binary fails to
        start.
        """
        if self._session is not None:
            raise RuntimeError("A browser login session is already in progress")

        display_number = _find_free_display_number()
        offset = display_number - DISPLAY_NUMBER_RANGE_START
        vnc_port = VNC_PORT_RANGE_START + offset
        websocket_port = WEBSOCKIFY_PORT_RANGE_START + offset

        xvfb_process = await _start_tagged_process(
            ["Xvfb", f":{display_number}", "-screen", "0", "1280x800x24"]
        )
        try:
            await _wait_for_display(display_number)
        except Exception as exc:
            _terminate_process_group(xvfb_process)
            raise BrowserLoginUnavailable(f"Xvfb display :{display_number} never came up: {exc}") from exc

        env = dict(os.environ)
        env["DISPLAY"] = f":{display_number}"

        try:
            playwright = await async_playwright().start()
        except Exception as exc:
            _terminate_process_group(xvfb_process)
            raise BrowserLoginUnavailable(f"Failed to start Playwright: {exc}") from exc

        try:
            browser = await asyncio.wait_for(
                playwright.chromium.launch(
                    headless=False,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                    env=env,
                ),
                timeout=CHROMIUM_LAUNCH_TIMEOUT_SEC,
            )
        except Exception as exc:
            await playwright.stop()
            _terminate_process_group(xvfb_process)
            raise BrowserLoginUnavailable(f"Failed to launch Chromium: {exc}") from exc

        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        except Exception as exc:
            await browser.close()
            await playwright.stop()
            _terminate_process_group(xvfb_process)
            raise BrowserLoginUnavailable(f"Failed to navigate to Twitch login page: {exc}") from exc

        try:
            x11vnc_process = await _start_tagged_process(
                [
                    "x11vnc",
                    "-display", f":{display_number}",
                    "-rfbport", str(vnc_port),
                    "-localhost", "-nopw", "-forever", "-shared", "-quiet",
                ]
            )
        except Exception as exc:
            await browser.close()
            await playwright.stop()
            _terminate_process_group(xvfb_process)
            raise BrowserLoginUnavailable(f"Failed to start x11vnc: {exc}") from exc

        try:
            websockify_process = await _start_tagged_process(
                ["websockify", f"127.0.0.1:{websocket_port}", f"127.0.0.1:{vnc_port}"]
            )
        except Exception as exc:
            _terminate_process_group(x11vnc_process)
            await browser.close()
            await playwright.stop()
            _terminate_process_group(xvfb_process)
            raise BrowserLoginUnavailable(f"Failed to start websockify: {exc}") from exc

        self._session = _BrowserLoginSession(
            display_number=display_number,
            websocket_port=websocket_port,
            playwright=playwright,
            browser=browser,
            context=context,
            xvfb_process=xvfb_process,
            x11vnc_process=x11vnc_process,
            websockify_process=websockify_process,
        )
        logger.info(f"Browser login session started on display :{display_number}, ws port {websocket_port}")
        return websocket_port

    async def wait_for_cookie(self, timeout: float = DEFAULT_TIMEOUT_SEC) -> dict[str, str]:
        """Poll the live browser context's cookies until a real auth-token
        cookie appears. Does NOT tear down the session on any outcome --
        the caller must call stop() afterward regardless.

        Raises BrowserLoginTimeout if no cookie appears in time, or
        BrowserLoginCancelled if cancel() was called meanwhile.
        """
        if self._session is None:
            raise RuntimeError("start() must be called before wait_for_cookie()")
        session = self._session
        deadline = time.monotonic() + timeout
        while True:
            if session.cancelled.is_set():
                raise BrowserLoginCancelled()
            cookies = await session.context.cookies()
            found = {c["name"]: c["value"] for c in cookies if c["domain"].endswith(COOKIE_DOMAIN)}
            if COOKIE_NAME in found:
                return {
                    "auth-token": found[COOKIE_NAME],
                    "unique_id": found.get(DEVICE_ID_COOKIE_NAME, ""),
                }
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrowserLoginTimeout()
            try:
                await asyncio.wait_for(
                    session.cancelled.wait(), timeout=min(COOKIE_POLL_INTERVAL_SEC, remaining)
                )
                raise BrowserLoginCancelled()
            except asyncio.TimeoutError:
                continue

    def cancel(self) -> None:
        """Signal the in-progress attempt (if any) to stop waiting."""
        if self._session is not None:
            self._session.cancelled.set()

    async def stop(self) -> None:
        """Tear down every process/resource for the current session, if
        any. Safe to call multiple times, and after a failed start()."""
        session = self._session
        self._session = None
        if session is None:
            return
        try:
            await session.browser.close()
        except Exception as exc:
            logger.warning(f"Error closing browser login's browser: {exc}")
        try:
            await session.playwright.stop()
        except Exception as exc:
            logger.warning(f"Error stopping browser login's playwright: {exc}")
        for proc in (session.websockify_process, session.x11vnc_process, session.xvfb_process):
            _terminate_process_group(proc)
        logger.info(f"Browser login session on display :{session.display_number} torn down")


def _find_free_display_number() -> int:
    """Pick the first display number in our reserved range with no X lock
    file present. Not perfectly race-free against a concurrent start, but
    this app only ever runs one attempt at a time by design."""
    for offset in range(DISPLAY_NUMBER_RANGE_SIZE):
        candidate = DISPLAY_NUMBER_RANGE_START + offset
        if not os.path.exists(f"/tmp/.X{candidate}-lock"):
            return candidate
    raise BrowserLoginUnavailable("No free virtual display number in the reserved range")


async def _wait_for_display(display_number: int, timeout: float = 5.0) -> None:
    """Block until Xvfb's X11 socket for this display accepts connections."""
    deadline = time.monotonic() + timeout
    socket_path = f"/tmp/.X11-unix/X{display_number}"
    while time.monotonic() < deadline:
        if os.path.exists(socket_path):
            return
        await asyncio.sleep(0.1)
    raise TimeoutError(f"Xvfb socket {socket_path} never appeared")


async def _start_tagged_process(argv: list[str]) -> asyncio.subprocess.Process:
    """Start a subprocess in its own process group (so stop() can kill any
    children it spawns too)."""
    return await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )


def _terminate_process_group(proc: asyncio.subprocess.Process) -> None:
    """Kill a process and its whole process group (see _start_tagged_process's
    start_new_session=True). Best-effort -- never raises."""
    if proc.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
    except Exception as exc:
        logger.warning(f"Error terminating process group for pid {proc.pid}: {exc}")


async def sweep_orphaned_processes() -> int:
    """Kill any Xvfb/x11vnc/websockify processes left over from a previous
    run (crash or deploy mid-login), recognized by the reserved
    display/port ranges this module always uses. Safe to call on every
    app startup, including a clean one (finds nothing, returns 0).

    Returns the number of processes killed.
    """
    vnc_port_prefix = VNC_PORT_RANGE_START // 10
    websockify_port_prefix = WEBSOCKIFY_PORT_RANGE_START // 10
    patterns = [
        rf"Xvfb :(9[0-{DISPLAY_NUMBER_RANGE_SIZE - 1}])\b",
        rf"x11vnc .*-rfbport {vnc_port_prefix}[0-{DISPLAY_NUMBER_RANGE_SIZE - 1}]\b",
        rf"websockify {websockify_port_prefix}[0-{DISPLAY_NUMBER_RANGE_SIZE - 1}]\b",
    ]
    killed = 0
    for pattern in patterns:
        proc = await asyncio.create_subprocess_exec(
            "pgrep", "-f", pattern,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        for line in stdout.decode().splitlines():
            line = line.strip()
            if not line.isdigit():
                continue
            pid = int(line)
            try:
                os.kill(pid, signal.SIGKILL)
                killed += 1
                logger.info(f"Killed orphaned browser-login process pid {pid} (matched {pattern!r})")
            except ProcessLookupError:
                pass
    return killed
