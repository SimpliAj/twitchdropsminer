"""Twitch login and Client-Integrity minting with a plain (non-Playwright) Chrome.

Login (BrowserLoginManager): on a host with a desktop, TDM opens a normal
Chrome window on the Twitch login page and reads the session cookie from it;
on a headless host it opens nothing and waits for the login helper
(scripts/tdm_login_helper.py, served at /login-helper.py) to send the session.
See docs/superpowers/specs/2026-10-14-simple-login-design.md.

Integrity tokens (acquire_integrity_token): a throwaway plain Chrome, under
Xvfb on headless hosts, loads only Twitch's bot-check SDK into an empty page
and calls /integrity from there -- or, with TDM_INTEGRITY_HELPER_URL set,
another machine does that (src/auth/integrity_helper.py).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import json
import logging
import os
import re
import shutil
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import aiohttp
from playwright.async_api import async_playwright
from yarl import URL

from src.auth.plain_chrome import PlainChrome
from src.config.client_info import ClientType


logger = logging.getLogger("TwitchDrops")

LOGIN_URL = "https://www.twitch.tv/login"
COOKIE_DOMAIN = "twitch.tv"
COOKIE_NAME = "auth-token"
DEVICE_ID_COOKIE_NAME = "unique_id"
_IS_WINDOWS = os.name == "nt"
DEFAULT_TIMEOUT_SEC = 600  # 10 minutes
COOKIE_POLL_INTERVAL_SEC = 1.5
CHROMIUM_LAUNCH_TIMEOUT_SEC = 30
# A Raspberry Pi 3B+ needs about a minute for a mint (confirmed live).
INTEGRITY_MINT_TIMEOUT_SEC = 120
# Any tiny www.twitch.tv document gives the SDK the right origin.
INTEGRITY_SDK_PAGE = "https://www.twitch.tv/robots.txt"
INTEGRITY_SDK_URL = (
    "https://k.twitchcdn.net/149e9513-01fa-4fb0-aad4-566afd725d1b/"
    "2d206a39-8ed7-437e-a3be-862e0f06eea3/p.js"
)
# Hosts whose own browser Twitch won't trust (a Raspberry Pi's Chromium gets
# tokens that always fail) can have another machine mint for them; see
# src/auth/integrity_helper.py.
INTEGRITY_HELPER_URL_ENV = "TDM_INTEGRITY_HELPER_URL"
INTEGRITY_HELPER_KEY_ENV = "TDM_INTEGRITY_HELPER_KEY"

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


@dataclass
class _BrowserLoginSession:
    """One in-progress login attempt. `chrome` is the plain Chrome window
    opened on a desktop host ("local" mode) or None on a headless host
    ("remote" mode), where only the login helper can complete it."""

    mode: str
    chrome: PlainChrome | None
    helper_cookies: dict[str, str] | None = None
    submitted: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)


class BrowserLoginManager:
    """Owns one Twitch login attempt. Callers serialize attempts (see
    _AuthState's lock); a second concurrent start() raises RuntimeError."""

    def __init__(self) -> None:
        self._session: _BrowserLoginSession | None = None

    @property
    def in_progress(self) -> bool:
        return self._session is not None

    async def start(self) -> str:
        """Open a plain Chrome on the Twitch login page when this host has a
        desktop ("local"), otherwise open nothing and wait for the login
        helper ("remote"). Returns the mode."""
        if self._session is not None:
            raise RuntimeError("A browser login session is already in progress")
        display = _detect_real_display()
        if display is not None:
            env = None if _IS_WINDOWS else {**os.environ, "DISPLAY": f":{display}"}
            try:
                chrome = PlainChrome(await _plain_chrome_binary(), LOGIN_URL, env, no_sandbox=_needs_no_sandbox())
                await chrome.start()
            except Exception as exc:
                logger.warning(f"Could not open a login browser on this desktop ({exc}); waiting for the login helper")
            else:
                self._session = _BrowserLoginSession("local", chrome)
                logger.info("Opened a browser window for the Twitch login on this computer")
                return "local"
        self._session = _BrowserLoginSession("remote", None)
        logger.info("Waiting for the login helper to send a Twitch session")
        return "remote"

    async def wait_for_cookie(self, timeout: float = DEFAULT_TIMEOUT_SEC) -> dict[str, str]:
        """Wait for the session cookies from the local window or the helper.
        Raises BrowserLoginTimeout, or BrowserLoginCancelled on cancel() or
        when the user closed the local window. Never tears the session down."""
        if self._session is None:
            raise RuntimeError("start() must be called before wait_for_cookie()")
        session = self._session
        deadline = time.monotonic() + timeout
        while True:
            if session.cancelled.is_set():
                raise BrowserLoginCancelled()
            if session.helper_cookies is not None:
                return session.helper_cookies
            if session.chrome is not None:
                try:
                    found = await session.chrome.cookies(COOKIE_DOMAIN)
                except Exception:
                    found = {}
                if found.get(COOKIE_NAME):
                    return {"auth-token": found[COOKIE_NAME], "unique_id": found.get(DEVICE_ID_COOKIE_NAME, "")}
                if not session.chrome.alive:
                    raise BrowserLoginCancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrowserLoginTimeout()
            waiters = [asyncio.ensure_future(session.submitted.wait()), asyncio.ensure_future(session.cancelled.wait())]
            await asyncio.wait(waiters, timeout=min(COOKIE_POLL_INTERVAL_SEC, remaining), return_when=asyncio.FIRST_COMPLETED)
            for waiter in waiters:
                waiter.cancel()

    async def inject_session_from_helper(self, auth_token: str, unique_id: str) -> None:
        """The login helper (scripts/tdm_login_helper.py) logged in with the
        user's own Chrome on their own device and sent the session here."""
        if self._session is None:
            raise RuntimeError("No browser login session is in progress")
        self._session.helper_cookies = {"auth-token": auth_token, "unique_id": unique_id}
        self._session.submitted.set()

    def cancel(self) -> None:
        if self._session is not None:
            self._session.cancelled.set()

    async def stop(self) -> None:
        """Close the local window, if any. Safe to call repeatedly."""
        session = self._session
        self._session = None
        if session is not None and session.chrome is not None:
            await session.chrome.close()


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


async def acquire_integrity_token(auth_token: str, device_id: str) -> tuple[str, datetime] | None:
    """Mint a fresh Client-Integrity token: through the integrity helper when
    TDM_INTEGRITY_HELPER_URL is set, else in a throwaway plain Chrome here.

    Best-effort: returns None on any failure (never raises) -- the caller
    (_AuthState._ensure_integrity_token) treats a missing token as "proceed
    without the header, try again later".
    """
    helper_url = os.environ.get(INTEGRITY_HELPER_URL_ENV, "").strip()
    if helper_url:
        return await _mint_via_helper(helper_url, auth_token, device_id)
    return await mint_integrity_token_locally(auth_token, device_id)


def _helper_url_is_safe(helper_url: str) -> bool:
    """The auth token goes along, so plain http only within loopback, a LAN
    or a VPN (Tailscale's 100.64.0.0/10 is WireGuard-encrypted)."""
    url = URL(helper_url)
    if url.scheme == "https":
        return True
    if url.scheme != "http" or not url.host:
        return False
    try:
        ip = ipaddress.ip_address(url.host)
    except ValueError:
        return url.host == "localhost" or url.host.endswith(".ts.net")
    return ip.is_loopback or ip.is_private or ip in ipaddress.ip_network("100.64.0.0/10")


async def _mint_via_helper(helper_url: str, auth_token: str, device_id: str) -> tuple[str, datetime] | None:
    if not _helper_url_is_safe(helper_url):
        logger.warning(f"{INTEGRITY_HELPER_URL_ENV} must be https:// outside a LAN or VPN; not sending the session there")
        return None
    headers = {"Authorization": f"Bearer {os.environ.get(INTEGRITY_HELPER_KEY_ENV, '').strip()}"}
    timeout = aiohttp.ClientTimeout(total=INTEGRITY_MINT_TIMEOUT_SEC + 60)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{helper_url.rstrip('/')}/mint",
                json={"auth_token": auth_token, "device_id": device_id},
                headers=headers,
            ) as response:
                if response.status != 200:
                    logger.warning(f"Integrity helper answered HTTP {response.status}")
                    return None
                data = await response.json()
        logger.info("Client-Integrity token received from the integrity helper")
        return data["token"], datetime.fromtimestamp(data["expires_at"], timezone.utc)
    except Exception as exc:
        logger.warning(f"Integrity helper unreachable: {type(exc).__name__}")
        return None


async def mint_integrity_token_locally(auth_token: str, device_id: str) -> tuple[str, datetime] | None:
    """Mint in a throwaway plain Chrome (see _mint_integrity_token_plain),
    under Xvfb when this host has no desktop. Never raises."""
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
    try:
        return await _mint_integrity_token_plain(await _plain_chrome_binary(), env, auth_token, device_id)
    except Exception as exc:
        logger.warning(f"Integrity token acquisition failed: {exc}")
        return None
    finally:
        if xvfb_process is not None:
            await _terminate_process_group_gracefully(xvfb_process)
            _release_display_number(display_number)


def _needs_no_sandbox() -> bool:
    """Chrome refuses to start as root (e.g. in the Docker image) unless its
    sandbox is disabled; everywhere else keep it, so a visible login window
    shows no "unsupported command-line flag" bar."""
    return not _IS_WINDOWS and os.geteuid() == 0


async def _plain_chrome_binary() -> str:
    """The installed Google Chrome (or Edge, which every Windows has; or a
    system Chromium on Linux), else Playwright's own Chromium binary --
    either way launched as a plain process, not through Playwright. On Linux
    the installed Chrome matters: Ubuntu's AppArmor grants the sandbox's user
    namespaces only to Google Chrome's own profile."""
    if _IS_WINDOWS:
        for vendor, app, exe in (("Google", "Chrome", "chrome.exe"), ("Microsoft", "Edge", "msedge.exe")):
            for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
                path = os.path.join(base or "", vendor, app, "Application", exe)
                if base and os.path.exists(path):
                    return path
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            path = shutil.which(name)
            if path:
                return path
    async with async_playwright() as playwright:
        return playwright.chromium.executable_path



_SDK_MINT_JS = """async function(headers, sdk) {
  return await new Promise(resolve => {
    const deadline = setTimeout(() => resolve({failure: 'sdk_timeout'}), %d);
    const finish = value => { clearTimeout(deadline); resolve(value); };
    document.addEventListener('kpsdk-load', () => window.KPSDK.configure([
      {protocol: 'https:', method: 'POST', domain: 'gql.twitch.tv', path: '/integrity'}
    ]), {once: true});
    document.addEventListener('kpsdk-ready', async () => {
      try {
        const r = await window.fetch('https://gql.twitch.tv/integrity', {
          method: 'POST', headers, body: null, credentials: 'omit', mode: 'cors'});
        finish({status: r.status, data: await r.json()});
      } catch (e) { finish({failure: 'integrity_fetch'}); }
    }, {once: true});
    const script = document.createElement('script');
    script.onerror = () => finish({failure: 'sdk_script'});
    script.src = sdk;
    document.body.appendChild(script);
  });
}""" % (INTEGRITY_MINT_TIMEOUT_SEC * 1000)


async def _mint_integrity_token_plain(
    binary: str, env: dict[str, str], auth_token: str, device_id: str
) -> tuple[str, datetime] | None:
    """Mint a Client-Integrity token in a plain, non-Playwright Chrome.

    /integrity sits behind Kasada's bot check. Instead of the whole twitch.tv
    app (slow and heavy on a Pi), only Kasada's SDK is loaded, into
    twitch.tv's robots.txt -- the approach rangermix/TwitchDropsMiner uses --
    then /integrity is called from the page with this session's token and
    device id. The token is bound to the account and the device id, not to
    the IP, session id or user agent (confirmed live 2026-10-29).
    """
    chrome = PlainChrome(binary, "about:blank", env, no_sandbox=_needs_no_sandbox(), quiet=True)
    await chrome.start()
    try:
        targets = (await chrome.call("Target.getTargets"))["result"]["targetInfos"]
        target = next(t for t in targets if t["type"] == "page")
        session = (
            await chrome.call("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True})
        )["result"]["sessionId"]
        started = time.monotonic()
        deadline = started + INTEGRITY_MINT_TIMEOUT_SEC
        await chrome.call("Page.enable", {}, session)
        await chrome.call("Page.navigate", {"url": INTEGRITY_SDK_PAGE}, session)
        while (await chrome.next_event(timeout=max(deadline - time.monotonic(), 0.1))).get("method") != (
            "Page.loadEventFired"
        ):
            pass
        page = await chrome.call("Runtime.evaluate", {"expression": "globalThis"}, session)
        headers = {"authorization": f"OAuth {auth_token}", "client-id": ClientType.WEB.CLIENT_ID}
        if device_id:
            headers["x-device-id"] = device_id
        reply = await chrome.call("Runtime.callFunctionOn", {
            "objectId": page["result"]["result"]["objectId"],
            "functionDeclaration": _SDK_MINT_JS,
            "arguments": [{"value": headers}, {"value": INTEGRITY_SDK_URL}],
            "awaitPromise": True, "returnByValue": True,
        }, session, timeout=max(deadline - time.monotonic(), 1))
        value = reply.get("result", {}).get("result", {}).get("value") or {}
        data = value.get("data") or {}
        token = data.get("token")
        if value.get("status") != 200 or not token:
            logger.warning(
                f"Integrity token acquisition: no token ({value.get('failure') or value.get('status')})"
            )
            return None
        logger.info(f"Client-Integrity token minted in {time.monotonic() - started:.0f}s")
        expiration = data.get("expiration")
        if isinstance(expiration, (int, float)):
            return token, datetime.fromtimestamp(expiration / 1000, timezone.utc)
        return token, _decode_jwt_expiry(token)
    finally:
        await chrome.close()


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
    if _IS_WINDOWS:
        # Windows always has a real desktop and no X11 (no DISPLAY, no Xvfb).
        return 0
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
    raise RuntimeError("No free virtual display number in the reserved range")


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
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
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


def _parent_pid(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as stat:
            # "<pid> (<comm>) <state> <ppid> ..." -- comm may contain spaces.
            return int(stat.read().rpartition(")")[2].split()[1])
    except (OSError, ValueError, IndexError):
        return None


async def sweep_orphaned_processes() -> int:
    """Kill any Xvfb/x11vnc processes left over from a previous run (crash
    or deploy mid-login), recognized by the reserved display/port ranges
    this module always uses and by having been re-parented to init. Safe to call on every app startup, including
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
            # Only a real orphan: its TDM process died and init adopted it.
            # Another TDM instance on the same host still parents its own.
            if _parent_pid(pid) != 1:
                continue
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
