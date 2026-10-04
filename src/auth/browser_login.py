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
import base64
import json
import logging
import os
import re
import signal
import time
import zoneinfo
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

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
    """Internal handle for one in-progress login attempt.

    websocket_port/xvfb_process/x11vnc_process are None when the attempt
    is running on a real, already-available desktop display instead of a
    virtual one we spun up ourselves (see _detect_real_display) -- there's
    nothing to proxy into a noVNC client because the login window is
    directly visible on the host's own screen, not any process we need to
    track for teardown.
    """

    display_number: int
    websocket_port: int | None
    playwright: Playwright
    browser: Browser
    context: BrowserContext
    xvfb_process: asyncio.subprocess.Process | None
    x11vnc_process: asyncio.subprocess.Process | None
    # Started right before the login page navigation (see _capture_integrity_
    # token); by the time an interactive login actually succeeds, the user
    # has taken far longer than this task's own short internal timeout, so
    # it's always done by then -- awaited once via
    # BrowserLoginManager.captured_integrity_token.
    integrity_capture_task: asyncio.Task | None = None
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

    async def start(self) -> int | None:
        """Start a new login attempt.

        If a real desktop display is already available (a home/NAS
        deployment with DISPLAY passed through, not a headless VPS -- see
        _detect_real_display), pops up an ordinary visible Chromium window
        there directly: no Xvfb/x11vnc/noVNC involved, the user just looks
        at their own screen. Otherwise falls back to a virtual display
        proxied to the dashboard over noVNC, same as always.

        Returns the local raw-VNC TCP port the caller should proxy (see
        src/web/app.py's WS /api/login/browser/ws) for a noVNC client, or
        None when the login window opened on a real display instead --
        there is nothing to proxy.

        Raises RuntimeError if a session is already in progress, or
        BrowserLoginUnavailable if any required process/binary fails to
        start.
        """
        if self._session is not None:
            raise RuntimeError("A browser login session is already in progress")

        real_display = _detect_real_display()
        if real_display is not None:
            logger.info(f"Real desktop display :{real_display} detected; opening Chromium there directly")
            return await self._start_on_display(real_display, xvfb_process=None)
        display_number = _find_free_display_number()
        xvfb_process = await self._spawn_xvfb_on(display_number)
        return await self._start_on_display(display_number, xvfb_process=xvfb_process)

    async def _spawn_xvfb_on(self, display_number: int) -> asyncio.subprocess.Process:
        xvfb_process = await _start_tagged_process(
            ["Xvfb", f":{display_number}", "-screen", "0", "1280x800x24"]
        )
        try:
            await _wait_for_display(display_number)
        except Exception as exc:
            _terminate_process_group(xvfb_process)
            # Without this, a display number this call claimed (see
            # _find_free_display_number's caller) stays permanently burned
            # on every failure -- confirmed live: ten straight failures
            # (one per reserved slot) exhausts the whole range, and every
            # subsequent attempt then fails immediately with "No free
            # virtual display number in the reserved range", forever,
            # until the process is restarted.
            _release_display_number(display_number)
            raise BrowserLoginUnavailable(f"Xvfb display :{display_number} never came up: {exc}") from exc
        return xvfb_process

    async def _start_on_display(
        self, display_number: int, xvfb_process: asyncio.subprocess.Process | None
    ) -> int | None:
        """Shared by both start() paths from here on: launch Chromium
        against `display_number` (already up either way -- a real desktop
        or the Xvfb _spawn_xvfb_on already waited for) and, only when
        xvfb_process is not None (the virtual-display path), also start
        x11vnc and report its port. `xvfb_process` doubles as the flag for
        which path this is."""
        env = dict(os.environ)
        env["DISPLAY"] = f":{display_number}"

        def cleanup_xvfb() -> None:
            if xvfb_process is not None:
                _terminate_process_group(xvfb_process)
                # Same leak as _spawn_xvfb_on's own failure path (see its
                # comment): every one of these downstream failure branches
                # (Playwright/Chromium/navigation/x11vnc) also claimed this
                # display number and must release it too, or it's
                # permanently gone the same way.
                _release_display_number(display_number)

        try:
            playwright = await async_playwright().start()
        except Exception as exc:
            cleanup_xvfb()
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
            cleanup_xvfb()
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
            # OS-level TZ change on a running process). Harmless on a real
            # desktop display too, where it's already correct anyway.
            # Validated first (_resolve_timezone_id) -- an invalid value
            # passed straight through used to make every single retry fail
            # identically forever, since Playwright rejects a bad
            # timezone_id at new_page() and the env var never changes.
            context = await browser.new_context(timezone_id=_resolve_timezone_id())
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
            # Attached before goto, not after -- Twitch's own JS fires this
            # on a normal page load (confirmed live), and a response that
            # arrives before the listener exists is simply missed.
            integrity_capture_task = asyncio.ensure_future(_capture_integrity_token(page))
            await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        except Exception as exc:
            await browser.close()
            await playwright.stop()
            cleanup_xvfb()
            raise BrowserLoginUnavailable(f"Failed to navigate to Twitch login page: {exc}") from exc

        vnc_port: int | None = None
        x11vnc_process: asyncio.subprocess.Process | None = None
        if xvfb_process is not None:
            offset = display_number - DISPLAY_NUMBER_RANGE_START
            vnc_port = VNC_PORT_RANGE_START + offset
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
                cleanup_xvfb()
                raise BrowserLoginUnavailable(f"Failed to start x11vnc: {exc}") from exc

        self._session = _BrowserLoginSession(
            display_number=display_number,
            websocket_port=vnc_port,
            playwright=playwright,
            browser=browser,
            context=context,
            xvfb_process=xvfb_process,
            x11vnc_process=x11vnc_process,
            integrity_capture_task=integrity_capture_task,
        )
        if vnc_port is not None:
            logger.info(f"Browser login session started on display :{display_number}, vnc port {vnc_port}")
        else:
            logger.info(f"Browser login session started on real display :{display_number}")
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

    async def captured_integrity_token(self) -> tuple[str, datetime] | None:
        """The Client-Integrity token captured from this session's own
        login-page navigation (see _capture_integrity_token), if one
        arrived -- call this once, after wait_for_cookie() has already
        succeeded (so the capture task, started right before the login
        navigation, has had the entire interactive login's duration to
        finish its own short internal timeout). None if no session is
        active, nothing was captured, or capture itself failed for any
        reason -- never raises."""
        session = self._session
        if session is None or session.integrity_capture_task is None:
            return None
        try:
            return await session.integrity_capture_task
        except Exception as exc:
            logger.warning(f"Integrity token capture task failed: {exc}")
            return None

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
        # leaking. Both are None on a real-display session (see
        # _detect_real_display) -- there's nothing of ours to kill, and
        # critically, _release_display_number must NOT run below either:
        # that display is the host's own real desktop, not one we created,
        # and deleting its live X11 socket/lock would break it.
        for proc in (session.x11vnc_process, session.xvfb_process):
            if proc is not None:
                await _terminate_process_group_gracefully(proc)
        if session.xvfb_process is not None:
            _release_display_number(session.display_number)
        logger.info(f"Browser login session on display :{session.display_number} torn down")


def _resolve_timezone_id() -> str | None:
    """The TZ env var, validated against the IANA tz database, or None if
    it's unset/empty/malformed.

    A bad value here used to be fatal and permanent: Playwright rejects an
    invalid timezone_id at new_page() time, which previously surfaced as
    BrowserLoginUnavailable on every single retry forever (the value never
    changes, so neither does the outcome) -- reported live as "{Europe/
    Berlin}" (literal braces and all) from a host whose deployment config
    had mangled the env var. Validating here means a malformed TZ degrades
    to "no override" (UTC) instead of permanently blocking login.
    """
    tz = os.environ.get("TZ", "").strip()
    if not tz:
        return None
    try:
        zoneinfo.ZoneInfo(tz)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        logger.warning(
            f"TZ env var {tz!r} is not a valid IANA timezone -- ignoring it for the "
            "login browser (login will proceed without a timezone override)"
        )
        return None
    return tz


INTEGRITY_CAPTURE_TIMEOUT_SEC = 15.0
INTEGRITY_DEFAULT_TTL = timedelta(hours=4)


def _decode_jwt_expiry(token: str) -> datetime:
    """The Client-Integrity token's own real expiry (its JWT "exp" claim),
    or a conservative default if it isn't a decodable JWT for any reason.
    No signature verification -- we only need the expiry we were already
    handed by Twitch over a connection we made ourselves, not to validate
    authenticity of something a third party gave us."""
    try:
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        if "exp" in payload:
            return datetime.fromtimestamp(payload["exp"], timezone.utc)
    except Exception:
        pass
    return datetime.now(timezone.utc) + INTEGRITY_DEFAULT_TTL


async def _capture_integrity_token(page) -> tuple[str, datetime] | None:
    """Watch `page` for the Client-Integrity token Twitch's own JS mints by
    POSTing to gql.twitch.tv/integrity -- confirmed live to fire on a
    normal twitch.tv page load, real browser or not. Call this BEFORE
    navigating (goto), not after, or the response may already have come
    and gone. Returns (token, expiry) or None if nothing arrived within
    INTEGRITY_CAPTURE_TIMEOUT_SEC.
    """
    captured: dict[str, str] = {}

    async def on_response(response) -> None:
        if "token" in captured:
            return
        if "gql.twitch.tv" not in response.url or not response.url.rstrip("/").endswith("/integrity"):
            return
        try:
            data = await response.json()
        except Exception:
            return
        token = data.get("token")
        if token:
            captured["token"] = token

    page.on("response", on_response)
    try:
        deadline = time.monotonic() + INTEGRITY_CAPTURE_TIMEOUT_SEC
        while "token" not in captured and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
    finally:
        page.remove_listener("response", on_response)

    token = captured.get("token")
    if not token:
        return None
    return token, _decode_jwt_expiry(token)


async def acquire_integrity_token(auth_token: str, device_id: str) -> tuple[str, datetime] | None:
    """Mint a fresh Client-Integrity token using a real, throwaway Chromium
    session authenticated with the already-logged-in account's own
    cookies -- no interactive login needed, just the same launch
    configuration BrowserLoginManager.start() already uses successfully
    for the real thing.

    Replaces the old streamlink-based approach (formerly src/auth/
    integrity.py): that one's headful Chromium never opened its own CDP
    debug port in this environment, confirmed by direct testing, even
    after patching --no-sandbox into its launch args. This module's own
    Playwright-based Chromium launch is the one already proven to work
    reliably here (it's what the real-browser login itself runs on), so
    reuse that instead of a separate toolchain for the identical "drive a
    real browser past Twitch's integrity check" problem.

    Best-effort: returns None on any failure (never raises) -- the caller
    (_AuthState._ensure_integrity_token) already treats a missing token as
    "proceed without the header, try again later", exactly as it did for
    the old approach's failures.
    """
    real_display = _detect_real_display()
    xvfb_process: asyncio.subprocess.Process | None = None
    display_number: int
    if real_display is not None:
        display_number = real_display
    else:
        display_number = _find_free_display_number()
        try:
            xvfb_process = await _start_tagged_process(
                ["Xvfb", f":{display_number}", "-screen", "0", "1280x800x24"]
            )
            await _wait_for_display(display_number)
        except Exception as exc:
            logger.warning(f"Integrity token acquisition: Xvfb display :{display_number} never came up: {exc}")
            if xvfb_process is not None:
                _terminate_process_group(xvfb_process)
            return None

    env = dict(os.environ)
    env["DISPLAY"] = f":{display_number}"
    playwright: Playwright | None = None
    browser: Browser | None = None
    try:
        try:
            playwright = await async_playwright().start()
            browser = await asyncio.wait_for(
                playwright.chromium.launch(
                    headless=False,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                    env=env,
                ),
                timeout=CHROMIUM_LAUNCH_TIMEOUT_SEC,
            )
        except Exception as exc:
            logger.warning(f"Integrity token acquisition: failed to launch Chromium: {exc}")
            return None

        try:
            context = await browser.new_context(timezone_id=_resolve_timezone_id())
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
            )
            cookies = [{"name": COOKIE_NAME, "value": auth_token, "domain": f".{COOKIE_DOMAIN}", "path": "/"}]
            if device_id:
                cookies.append(
                    {"name": DEVICE_ID_COOKIE_NAME, "value": device_id, "domain": f".{COOKIE_DOMAIN}", "path": "/"}
                )
            await context.add_cookies(cookies)
            page = await context.new_page()
            capture_task = asyncio.ensure_future(_capture_integrity_token(page))
            await page.goto("https://www.twitch.tv/drops/campaigns", wait_until="domcontentloaded")
            return await capture_task
        except Exception as exc:
            logger.warning(f"Integrity token acquisition: failed during page capture: {exc}")
            return None
    finally:
        if browser is not None:
            await browser.close()
        if playwright is not None:
            await playwright.stop()
        if xvfb_process is not None:
            await _terminate_process_group_gracefully(xvfb_process)
            _release_display_number(display_number)


def _detect_real_display() -> int | None:
    """If a real desktop display is already available -- a home/NAS
    deployment with a desktop environment and DISPLAY passed through to
    the container (or running directly on a machine with one), not a
    headless VPS -- return its number so start() can pop up an ordinary
    visible Chromium window there directly, skipping Xvfb/x11vnc/noVNC
    entirely (the same real-browser-on-a-real-device signal rangermix's
    now-abandoned desktop helper was built around, but with nothing extra
    to download or run -- it's already the same machine). None if nothing
    usable is found, which is the common case on a VPS.
    """
    display_env = os.environ.get("DISPLAY", "").strip()
    if not display_env.startswith(":"):
        return None
    try:
        number = int(display_env[1:].split(".", 1)[0])
    except ValueError:
        return None
    if DISPLAY_NUMBER_RANGE_START <= number < DISPLAY_NUMBER_RANGE_START + DISPLAY_NUMBER_RANGE_SIZE:
        # One of our own reserved virtual displays (e.g. a crashed previous
        # attempt's DISPLAY leaking into this process's environment) --
        # never treat that as a real desktop.
        return None
    if not os.path.exists(f"/tmp/.X11-unix/X{number}"):
        return None
    return number


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
