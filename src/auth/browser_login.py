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
Playwright, and an x11vnc server exposing that display so a browser-based
noVNC client can drive it interactively. src/web/app.py's WS route speaks
the noVNC/WebSocket side to the browser itself and relays raw bytes to/from
x11vnc's plain VNC port directly -- no separate websockify process sits in
between (websockify's own job, WS<->raw-TCP framing, is that route's job
too; chaining a second one in front of x11vnc only added a redundant hop
that expects its own WS handshake and drops a raw TCP client instantly).
The caller (src/auth/auth_state.py's _browser_login) awaits wait_for_cookie()
for the resulting session cookie, then always calls stop() regardless of
outcome.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
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


# The single in-progress login attempt's manager, or None when no login is
# running. There is at most one attempt system-wide at a time (_AuthState
# serializes them behind its own lock around _browser_login()), and every
# consumer -- the auth flow that drives it, the WS proxy route that streams
# its screen, the cancel route that aborts it -- must see the SAME instance,
# or the feature silently half-works. Set/cleared exclusively by
# _AuthState._browser_login(); read via get_active_manager().
_active_manager: BrowserLoginManager | None = None


def get_active_manager() -> BrowserLoginManager | None:
    """The currently in-progress login session's manager, if any. There is
    at most one login attempt system-wide at a time (see _AuthState's own
    lock around _browser_login())."""
    return _active_manager


def set_active_manager(manager: BrowserLoginManager | None) -> None:
    """Publish (or clear) the one in-progress login attempt's manager."""
    global _active_manager
    _active_manager = manager


class BrowserLoginTimeout(Exception):
    """No successful login cookie appeared within the timeout."""


class BrowserLoginCancelled(Exception):
    """The login attempt was explicitly cancelled."""


class BrowserLoginUnavailable(Exception):
    """Required system dependencies (Xvfb/Chromium/x11vnc) are missing or
    failed to start."""


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

    @property
    def websocket_port(self) -> int | None:
        """x11vnc's local raw-VNC TCP port for the in-progress session, or
        None if no session is active -- named for what src/web/app.py's WS
        route does with it (proxies it to a noVNC WebSocket client), not
        for the protocol spoken on the port itself, which is plain VNC.
        Reading this instead of reaching into ._session avoids a TOCTOU
        crash when stop() lands between an in_progress check and the port
        read."""
        session = self._session
        return session.websocket_port if session is not None else None

    async def start(self) -> int:
        """Start a new login attempt: virtual display, real Chromium
        navigated to the Twitch login page, VNC server.

        Returns the local raw-VNC TCP port the caller should proxy (see
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
            # A container/VPS defaults to UTC (or whatever the host happens
            # to run), which mismatches the real timezone of wherever the
            # account normally logs in from -- rangermix's team hit this
            # exact "Your browser is not currently supported" rejection
            # with their own containerized Twitch login and documented TZ
            # as the fix (github.com/rangermix/TwitchDropsMiner issue
            # #148). Reuse the same TZ env var Docker users already set
            # for the whole container for this reason, passed through
            # Playwright's own timezone_id (reliably changes what the
            # page's JS sees, unlike hoping Chromium's ICU/V8 picks up an
            # OS-level TZ change on a running process).
            context = await browser.new_context(timezone_id=os.environ.get("TZ") or None)
            # Playwright's CDP automation flag makes navigator.webdriver
            # true regardless of headless/headful, which is what actually
            # triggers Twitch's "Your browser is not currently supported"
            # banner on the real login form (confirmed live: with this,
            # the same login page renders normally). This is cosmetic --
            # it just presents as an ordinary Chromium tab to the page's
            # own JS, the same as a real browser -- not a change to
            # Twitch's server-side login flow itself.
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
            )
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

        self._session = _BrowserLoginSession(
            display_number=display_number,
            websocket_port=vnc_port,
            playwright=playwright,
            browser=browser,
            context=context,
            xvfb_process=xvfb_process,
            x11vnc_process=x11vnc_process,
        )
        logger.info(f"Browser login session started on display :{display_number}, vnc port {vnc_port}")
        return vnc_port

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

    async def inject_manual_cookies(self, auth_token: str, unique_id: str) -> None:
        """Supply the login cookies directly instead of waiting for them to
        appear from the in-browser session -- for a user who logged in on
        their own device (a real residential IP/browser, which Twitch's
        integrity check treats as an ordinary login, unlike a datacenter
        VPS) and copied the resulting cookies here. wait_for_cookie()'s
        poll loop picks these up on its next tick exactly as if the
        in-browser login had just completed itself, so nothing else about
        the success path (token validation, persistence, teardown) changes.

        Raises RuntimeError if no session is in progress.
        """
        if self._session is None:
            raise RuntimeError("No browser login session is in progress")
        cookies = [{"name": COOKIE_NAME, "value": auth_token, "domain": f".{COOKIE_DOMAIN}", "path": "/"}]
        if unique_id:
            cookies.append(
                {"name": DEVICE_ID_COOKIE_NAME, "value": unique_id, "domain": f".{COOKIE_DOMAIN}", "path": "/"}
            )
        await self._session.context.add_cookies(cookies)

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
        # Graceful (SIGTERM, brief grace period) before SIGKILL -- x11vnc
        # needs the chance to release its SysV shm segment on exit, see
        # _terminate_process_group_gracefully's docstring. Xvfb torn down
        # the same way for consistency, though it isn't the one observed
        # leaking.
        for proc in (session.x11vnc_process, session.xvfb_process):
            await _terminate_process_group_gracefully(proc)
        _release_display_number(session.display_number)
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


def _release_display_number(display_number: int) -> None:
    """Remove the X lock file and socket for a display whose Xvfb is gone.

    _terminate_process_group() SIGKILLs Xvfb, which therefore never gets to
    clean up its own /tmp/.X<n>-lock, and /tmp survives container restarts --
    so without this every attempt (successful, cancelled or timed out) would
    permanently burn one of the DISPLAY_NUMBER_RANGE_SIZE reserved slots that
    _find_free_display_number() picks from, eventually making login
    impossible. Best-effort: never raises."""
    for path in (f"/tmp/.X{display_number}-lock", f"/tmp/.X11-unix/X{display_number}"):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning(f"Could not remove stale X file {path}: {exc}")


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


async def _terminate_process_group_gracefully(
    proc: asyncio.subprocess.Process, grace_sec: float = 2.0
) -> None:
    """Like _terminate_process_group, but SIGTERM first with a short grace
    period before SIGKILL. x11vnc (and Xvfb) own SysV shared-memory
    segments (the X MIT-SHM extension) that only get released by their own
    exit-time cleanup -- unlike regular memory/fds, the kernel does NOT
    reclaim these on SIGKILL, so a hard kill leaks one segment per
    session, invisibly, until the system-wide shmmni cap (often 4096) is
    hit and EVERY future x11vnc start fails with 'shmget: No space left on
    device' (reproduced live: after enough restarts tonight, this is
    exactly what silently broke the next login attempt). Best-effort --
    never raises."""
    if proc.returncode is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
        await asyncio.wait_for(proc.wait(), timeout=grace_sec)
        return
    except asyncio.TimeoutError:
        pass
    except (ProcessLookupError, PermissionError):
        return
    except Exception as exc:
        logger.warning(f"Error sending SIGTERM to process group for pid {proc.pid}: {exc}")
    _terminate_process_group(proc)


async def sweep_orphaned_processes() -> int:
    """Kill any Xvfb/x11vnc processes left over from a previous run (crash
    or deploy mid-login), recognized by the reserved display/port ranges
    this module always uses. Safe to call on every app startup, including
    a clean one (finds nothing, returns 0).

    Returns the number of processes killed.
    """
    display_prefix = DISPLAY_NUMBER_RANGE_START // 10
    vnc_port_prefix = VNC_PORT_RANGE_START // 10
    last_digit = DISPLAY_NUMBER_RANGE_SIZE - 1
    patterns = [
        rf"Xvfb :{display_prefix}[0-{last_digit}]\b",
        rf"x11vnc .*-rfbport {vnc_port_prefix}[0-{last_digit}]\b",
    ]
    # Display numbers whose Xvfb we killed: their /tmp/.X<n>-lock outlives the
    # SIGKILL and would otherwise keep _find_free_display_number() from ever
    # reusing the slot (see _release_display_number).
    swept_displays: set[int] = set()
    killed = 0
    for pattern in patterns:
        proc = await asyncio.create_subprocess_exec(
            # -a prints "<pid> <full command line>", so an Xvfb match also
            # tells us which display number to free below.
            "pgrep", "-af", pattern,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        for line in stdout.decode().splitlines():
            pid_text, _, cmdline = line.strip().partition(" ")
            if not pid_text.isdigit():
                continue
            pid = int(pid_text)
            try:
                # SIGTERM first, same reasoning as
                # _terminate_process_group_gracefully: x11vnc needs the
                # chance to release its SysV shm segment on exit, or it
                # leaks until the system-wide shmmni cap is hit and every
                # future x11vnc start fails outright.
                os.kill(pid, signal.SIGTERM)
                for _ in range(10):
                    await asyncio.sleep(0.2)
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                else:
                    os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                continue
            killed += 1
            logger.info(f"Killed orphaned browser-login process pid {pid} (matched {pattern!r})")
            display_match = re.search(rf"Xvfb :({display_prefix}[0-{last_digit}])\b", cmdline)
            if display_match is not None:
                swept_displays.add(int(display_match.group(1)))
    for display_number in swept_displays:
        _release_display_number(display_number)
    return killed
