# Server-Side Browser Login Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace OAuth device-code login (currently `SMARTBOX`, degraded to ~5-10 visible campaigns out of ~46, see issues #15-#18) with a real `www.twitch.tv/login` browser session captured server-side, restoring full campaign visibility without adding a desktop helper program.

**Architecture:** A new `BrowserLoginManager` spins up Xvfb + non-headless Playwright Chromium + x11vnc + websockify on demand, only while a login is in progress. The dashboard embeds a noVNC viewer so the user completes the real Twitch login (credentials, 2FA, CAPTCHA) live, in their own browser, with the actual browser running server-side. The manager polls for the resulting `auth-token` cookie and hands it to `_AuthState`, which persists it exactly as today. No credentials ever reach the server; no separate program for the user to install.

**Tech Stack:** Python 3.12, Playwright (async API), Xvfb, x11vnc, websockify, FastAPI (native `WebSocket` route), vendored noVNC JS client, existing Socket.IO broadcaster for status events.

**Spec:** `docs/superpowers/specs/2026-09-29-server-side-browser-login-design.md`

## Global Constraints

- No credential storage or auto-fill anywhere in this feature (spec Non-goals).
- Device-code login (`SMARTBOX`/`MOBILE_WEB`/`ANDROID_APP` client entries, `LOGIN_CLIENT`, `_oauth_login`) is fully removed, not kept as a fallback (spec Non-goals).
- Everything runs in the existing single container/process — no sidecar, no new exposed Docker port (spec Architecture: `WS /api/login/browser/ws` proxies through the existing FastAPI/uvicorn listener).
- `AGENTS.md` rule 2: OOP required for backend code. Rule 4: any new/changed UI text needs `lang/English.json` updated (other languages flagged, not required). Rule 5: `README.md` and every per-agent instruction file's shared sections must be updated in the same change, `Specific Instructions` sections untouched.
- Base Docker image changes from `python:3-alpine` to `python:3-slim` (glibc required by Playwright's Chromium — spec Architecture).

## Review Focus

- **A login attempt that never completes (user walks away):** must time out (default 10 minutes) and fully tear down Xvfb/Chromium/x11vnc/websockify — a reasonable person expects the dashboard to eventually show a clear "login timed out" state, not hang forever or leak processes. Covered in Task 2 (`wait_for_cookie` timeout) and Task 2's teardown test.
- **Two browser tabs both trigger a login at once:** the second attempt must be rejected with a clear error, not silently start a second Chromium or corrupt the first session's state. Covered in Task 5 (`/api/login/browser/start` conflict check) with its own test.
- **App crash/restart with a login mid-flight:** a reasonable person restarting the container expects a clean slate, not a zombie Xvfb eating resources forever. Covered in Task 3 (`sweep_orphaned_processes`, called on startup).
- **Missing system dependencies in a from-source (non-Docker) install:** a reasonable person running `python main.py` directly without Playwright's Chromium installed expects a clear, actionable error message, not a silent hang or a bare traceback. Covered in Task 2 (`BrowserLoginUnavailable` with the underlying exception message) and Task 5 (surfaced to the dashboard).
- **Cookie appears but login page never navigates away (edge case: Twitch sets the cookie but the SPA stays on `/login`):** the manager must treat cookie presence itself as success regardless of page URL, not wait for a redirect that might not (visibly) happen. Covered in Task 2's cookie-polling test using a mocked context that returns the cookie while `page.url` stays on the login page.

---

## Task 1: Docker/pyproject foundation for Playwright + Xvfb + noVNC bridge

**Files:**
- Modify: `Dockerfile`
- Modify: `pyproject.toml`
- Test: manual — `docker build` succeeds and the container can launch headful Chromium (no automated test; this is infra, covered by Task 2's mocked unit tests plus the manual checklist in Task 7)

**Interfaces:**
- Produces: a working `playwright`, `Xvfb`, `x11vnc`, `websockify` toolchain inside the built image, available to Task 2's `BrowserLoginManager` via `playwright.chromium.launch(...)` and `asyncio.create_subprocess_exec("Xvfb", ...)` / `"x11vnc"` / `"websockify"`.

- [ ] **Step 1: Switch the Dockerfile base image and system packages**

Replace the whole `Dockerfile` with:

```dockerfile
FROM python:3.12-slim

# Build arguments for metadata
ARG BUILD_DATE
ARG VCS_REF
ARG VERSION

# Labels following OCI Image Format Specification
LABEL org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.authors="SimpliAj" \
      org.opencontainers.image.url="https://github.com/SimpliAj/twitchdropsminer" \
      org.opencontainers.image.documentation="https://github.com/SimpliAj/twitchdropsminer/blob/main/README.md" \
      org.opencontainers.image.source="https://github.com/SimpliAj/twitchdropsminer" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.vendor="SimpliAj" \
      org.opencontainers.image.title="Twitch Drops Miner (SimpliAj Fork)" \
      org.opencontainers.image.description="TwitchDropsMiner fork with channel points auto-claimer, idle watch, multi-account support and Discord webhooks"

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

# Set working directory
WORKDIR /app

# Install system dependencies:
# - tzdata: unchanged from before
# - xvfb: virtual X display the real-browser login runs under (see
#   src/auth/browser_login.py)
# - x11vnc: exposes that virtual display over VNC for the dashboard's
#   embedded noVNC viewer
# - websockify: bridges x11vnc's raw VNC protocol to a WebSocket noVNC's
#   JS client can consume directly
# - Playwright's own --with-deps (below) pulls in Chromium's shared-library
#   requirements; this base image change (alpine -> slim) is what makes
#   that possible at all, since Chromium needs glibc and alpine ships musl
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    xvfb \
    x11vnc \
    websockify \
    && rm -rf /var/lib/apt/lists/*

# Copy project metadata and install dependencies
COPY pyproject.toml .

# Install Python dependencies (playwright is now a base dependency, see
# pyproject.toml)
RUN pip install --no-cache-dir .

# Install Playwright's bundled Chromium and its remaining native deps
RUN playwright install --with-deps chromium

# Copy application code
COPY main.py ./
COPY src/ ./src/
COPY lang/ ./lang/
COPY icons/ ./icons/
COPY web/ ./web/

# Create data directory for persistent storage
RUN mkdir -p /app/data && chmod 777 /app/data
RUN mkdir -p /app/logs && chmod 777 /app/logs

# Expose web port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz', timeout=2)" || exit 1

# Run the application (web GUI is now default)
CMD ["python", "main.py"]
```

- [ ] **Step 2: Move `playwright` from optional to base dependency in `pyproject.toml`**

Find the `dependencies = [...]` list (currently ends with `"coverage>=7.3.1",`) and add `playwright` to it:

```toml
dependencies = [
    "aiohttp>=3.9",
    "truststore",
    "python-dateutil",
    "fastapi>=0.104.0",
    "uvicorn[standard]>=0.24.0",
    "python-socketio>=5.10.0",
    "yarl>=1.9.2",
    "pydantic>=2.7.0",
    "python-multipart>=0.0.9",
    "coverage>=7.3.1",
    "playwright>=1.61.0",
]
```

Then find the comment block right above `[project.optional-dependencies]` that explains `campaign-discovery`'s optional Playwright extra (the one starting "Optional, NOT in the default install: real-browser supplemental campaign discovery..."). Update it to reflect that Playwright is now always installed for login, and this extra only controls whether the *supplemental discovery* module (`src/services/campaign_discovery.py`) is still additionally used:

```toml
# Playwright itself is now a base dependency (see `dependencies` above) --
# required for the real-browser login flow (src/auth/browser_login.py).
# This extra is now unrelated to whether Playwright is installed; it only
# gates src/services/campaign_discovery.py's SUPPLEMENTAL campaign
# discovery (merges additional campaigns into the primary login-derived
# list). Kept separate since that module is a nice-to-have, not required
# for the app to function.
```
(Keep whatever extras-group name and remaining lines already exist below that comment — only the comment text above changes.)

- [ ] **Step 3: Commit**

```bash
git add Dockerfile pyproject.toml
git commit -m "build: switch base image to slim (glibc) for Playwright Chromium, make playwright a base dependency"
```

---

## Task 2: `BrowserLoginManager` core

**Files:**
- Create: `src/auth/browser_login.py`
- Test: `tests/test_browser_login.py`

**Interfaces:**
- Consumes: `playwright.async_api.async_playwright` (Playwright's Python async API, already a dependency after Task 1).
- Produces (used by Task 3 and Task 4):
  - `class BrowserLoginManager` with:
    - `async def start(self) -> int` — returns the local websockify TCP port.
    - `async def wait_for_cookie(self, timeout: float = DEFAULT_TIMEOUT_SEC) -> dict[str, str]` — returns `{"auth-token": str, "unique_id": str}`.
    - `def cancel(self) -> None`
    - `async def stop(self) -> None`
    - `@property def in_progress(self) -> bool`
  - `class BrowserLoginTimeout(Exception)`, `class BrowserLoginCancelled(Exception)`, `class BrowserLoginUnavailable(Exception)`
  - `async def sweep_orphaned_processes() -> int` (implemented here, wired into startup in Task 3)
  - Module constants: `DISPLAY_NUMBER_RANGE_START = 90`, `DISPLAY_NUMBER_RANGE_SIZE = 10`, `VNC_PORT_RANGE_START = 5990`, `WEBSOCKIFY_PORT_RANGE_START = 6990`, `DEFAULT_TIMEOUT_SEC = 600`, `COOKIE_POLL_INTERVAL_SEC = 1.5`, `LOGIN_URL = "https://www.twitch.tv/login"`, `COOKIE_DOMAIN = "twitch.tv"`, `COOKIE_NAME = "auth-token"`, `DEVICE_ID_COOKIE_NAME = "unique_id"`.

- [ ] **Step 1: Write the failing tests for process/session lifecycle**

Create `tests/test_browser_login.py`:

```python
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.auth.browser_login import (
    BrowserLoginCancelled,
    BrowserLoginManager,
    BrowserLoginTimeout,
    BrowserLoginUnavailable,
    COOKIE_POLL_INTERVAL_SEC,
)


def _mock_process():
    proc = MagicMock()
    proc.pid = 12345
    proc.terminate = MagicMock()
    proc.wait = AsyncMock()
    return proc


class TestBrowserLoginManagerStart(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = BrowserLoginManager()
        self._patchers = []

        start_process_patcher = patch(
            "src.auth.browser_login._start_tagged_process",
            new=AsyncMock(side_effect=lambda argv: _mock_process()),
        )
        self._patchers.append(start_process_patcher)

        wait_display_patcher = patch(
            "src.auth.browser_login._wait_for_display", new=AsyncMock()
        )
        self._patchers.append(wait_display_patcher)

        mock_page = AsyncMock()
        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.cookies = AsyncMock(return_value=[])
        mock_browser = AsyncMock()
        mock_browser.new_context = AsyncMock(return_value=mock_context)
        mock_playwright_instance = AsyncMock()
        mock_playwright_instance.chromium.launch = AsyncMock(return_value=mock_browser)
        mock_playwright_cm = AsyncMock()
        mock_playwright_cm.start = AsyncMock(return_value=mock_playwright_instance)

        playwright_patcher = patch(
            "src.auth.browser_login.async_playwright",
            return_value=mock_playwright_cm,
        )
        self._patchers.append(playwright_patcher)

        for p in self._patchers:
            p.start()
        self.addAsyncCleanup(self._stop_patchers)

        self.mock_context = mock_context
        self.mock_browser = mock_browser

    async def _stop_patchers(self):
        for p in self._patchers:
            p.stop()

    async def test_start_returns_a_websocket_port(self):
        port = await self.manager.start()
        self.assertIsInstance(port, int)
        self.assertTrue(self.manager.in_progress)

    async def test_start_twice_raises(self):
        await self.manager.start()
        with self.assertRaises(RuntimeError):
            await self.manager.start()

    async def test_chromium_launch_failure_raises_unavailable(self):
        # Reconfigure the chromium.launch mock (already patched in setUp) to raise
        with patch(
            "src.auth.browser_login.async_playwright"
        ) as mock_async_playwright:
            mock_playwright_instance = AsyncMock()
            mock_playwright_instance.chromium.launch = AsyncMock(
                side_effect=RuntimeError("no chromium binary")
            )
            mock_cm = AsyncMock()
            mock_cm.start = AsyncMock(return_value=mock_playwright_instance)
            mock_async_playwright.return_value = mock_cm

            manager = BrowserLoginManager()
            with self.assertRaises(BrowserLoginUnavailable):
                await manager.start()
            self.assertFalse(manager.in_progress)


class TestBrowserLoginManagerWaitForCookie(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = BrowserLoginManager()
        self._patchers = []

        start_process_patcher = patch(
            "src.auth.browser_login._start_tagged_process",
            new=AsyncMock(side_effect=lambda argv: _mock_process()),
        )
        self._patchers.append(start_process_patcher)
        wait_display_patcher = patch(
            "src.auth.browser_login._wait_for_display", new=AsyncMock()
        )
        self._patchers.append(wait_display_patcher)

        self.mock_context = AsyncMock()
        self.mock_context.cookies = AsyncMock(return_value=[])
        mock_page = AsyncMock()
        mock_browser = AsyncMock()
        mock_browser.new_context = AsyncMock(return_value=self.mock_context)
        self.mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_playwright_instance = AsyncMock()
        mock_playwright_instance.chromium.launch = AsyncMock(return_value=mock_browser)
        mock_cm = AsyncMock()
        mock_cm.start = AsyncMock(return_value=mock_playwright_instance)
        playwright_patcher = patch(
            "src.auth.browser_login.async_playwright", return_value=mock_cm
        )
        self._patchers.append(playwright_patcher)

        for p in self._patchers:
            p.start()
        self.addAsyncCleanup(self._stop)

    async def _stop(self):
        for p in self._patchers:
            p.stop()

    async def test_returns_cookie_once_it_appears(self):
        await self.manager.start()
        # First poll: no cookie. Second poll: cookie present, even though
        # the page never navigated away from /login (Review Focus case).
        self.mock_context.cookies = AsyncMock(
            side_effect=[
                [],
                [
                    {"name": "auth-token", "value": "abc123", "domain": ".twitch.tv"},
                    {"name": "unique_id", "value": "device-xyz", "domain": ".twitch.tv"},
                ],
            ]
        )
        result = await self.manager.wait_for_cookie(timeout=5)
        self.assertEqual(result["auth-token"], "abc123")
        self.assertEqual(result["unique_id"], "device-xyz")

    async def test_times_out_when_no_cookie_appears(self):
        await self.manager.start()
        self.mock_context.cookies = AsyncMock(return_value=[])
        with self.assertRaises(BrowserLoginTimeout):
            await self.manager.wait_for_cookie(timeout=COOKIE_POLL_INTERVAL_SEC * 1.5)

    async def test_cancel_raises_cancelled(self):
        await self.manager.start()
        self.mock_context.cookies = AsyncMock(return_value=[])

        async def cancel_soon():
            await asyncio.sleep(COOKIE_POLL_INTERVAL_SEC * 0.5)
            self.manager.cancel()

        asyncio.ensure_future(cancel_soon())
        with self.assertRaises(BrowserLoginCancelled):
            await self.manager.wait_for_cookie(timeout=30)


class TestBrowserLoginManagerStop(unittest.IsolatedAsyncioTestCase):
    async def test_stop_terminates_all_processes_and_is_idempotent(self):
        manager = BrowserLoginManager()
        procs = [_mock_process(), _mock_process(), _mock_process()]
        proc_iter = iter(procs)

        with (
            patch(
                "src.auth.browser_login._start_tagged_process",
                new=AsyncMock(side_effect=lambda argv: next(proc_iter)),
            ),
            patch("src.auth.browser_login._wait_for_display", new=AsyncMock()),
            patch("src.auth.browser_login._terminate_process_group") as mock_terminate,
        ):
            mock_context = AsyncMock()
            mock_context.new_page = AsyncMock(return_value=AsyncMock())
            mock_browser = AsyncMock()
            mock_browser.new_context = AsyncMock(return_value=mock_context)
            mock_playwright_instance = AsyncMock()
            mock_playwright_instance.chromium.launch = AsyncMock(return_value=mock_browser)
            mock_cm = AsyncMock()
            mock_cm.start = AsyncMock(return_value=mock_playwright_instance)
            with patch("src.auth.browser_login.async_playwright", return_value=mock_cm):
                await manager.start()

            await manager.stop()
            self.assertFalse(manager.in_progress)
            self.assertEqual(mock_terminate.call_count, 3)
            mock_browser.close.assert_awaited_once()

            # idempotent: calling again does nothing and doesn't raise
            await manager.stop()
            self.assertEqual(mock_terminate.call_count, 3)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/claude/twitchdrops && source venv/bin/activate && python -m pytest tests/test_browser_login.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.auth.browser_login'`

- [ ] **Step 3: Implement `src/auth/browser_login.py`**

```python
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

        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")

        try:
            x11vnc_process = await _start_tagged_process(
                [
                    "x11vnc",
                    "-display", f":{display_number}",
                    "-rfbport", str(vnc_port),
                    "-nopw", "-forever", "-shared", "-quiet",
                ]
            )
            websockify_process = await _start_tagged_process(
                ["websockify", str(websocket_port), f"localhost:{vnc_port}"]
            )
        except Exception as exc:
            await browser.close()
            await playwright.stop()
            _terminate_process_group(xvfb_process)
            raise BrowserLoginUnavailable(f"Failed to start VNC bridge: {exc}") from exc

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
    patterns = [
        rf"Xvfb :(9[0-{DISPLAY_NUMBER_RANGE_SIZE - 1}])\b",
        rf"x11vnc .*-rfbport {VNC_PORT_RANGE_START}",
        rf"websockify {WEBSOCKIFY_PORT_RANGE_START}",
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/claude/twitchdrops && source venv/bin/activate && python -m pytest tests/test_browser_login.py -v`
Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/auth/browser_login.py tests/test_browser_login.py
git commit -m "feat: add BrowserLoginManager for server-side real-browser Twitch login"
```

---

## Task 3: Orphaned-process sweep on startup

**Files:**
- Modify: `src/__main__.py`
- Test: covered by Task 2's existing `sweep_orphaned_processes` unit-testability; this task just wires the call in, verified manually (see Task 7's checklist) since it needs a real startup sequence to observe.

**Interfaces:**
- Consumes: `src.auth.browser_login.sweep_orphaned_processes` (Task 2).

- [ ] **Step 1: Call `sweep_orphaned_processes()` early in startup**

In `src/__main__.py`, inside the `async def main():` function (see the existing block starting with `logger.info("=== TwitchDropsMiner Starting ===")`), add the sweep right after that logging block and before anything else starts:

```python
        logger.info("=== TwitchDropsMiner Starting ===")
        logger.info(f"Version: {__version__}")
        logger.info(f"Python version: {sys.version}")
        logger.info(f"Platform: {sys.platform}")
        logger.info(f"Proxy: {settings.proxy}")
        logger.info(f"Language: {settings.language}")
        logger.info(
            f"Minimum refresh interval: {settings.minimum_refresh_interval_minutes} minutes"
        )

        from src.auth.browser_login import sweep_orphaned_processes

        killed = await sweep_orphaned_processes()
        if killed:
            logger.warning(
                f"Cleaned up {killed} orphaned browser-login process(es) from a previous run"
            )
```

- [ ] **Step 2: Commit**

```bash
git add src/__main__.py
git commit -m "feat: sweep orphaned browser-login processes on startup"
```

---

## Task 4: Backend auth integration (`_AuthState`, `client_info.py`, `client.py`)

**Files:**
- Modify: `src/auth/auth_state.py`
- Modify: `src/auth/__init__.py`
- Modify: `src/config/client_info.py`
- Modify: `src/core/client.py`
- Test: `tests/test_auth_state_browser_login.py`

**Interfaces:**
- Consumes: `BrowserLoginManager` (Task 2).
- Produces: `_AuthState._browser_login()` (replaces `_oauth_login()`), consumed by `_AuthState._validate()`'s existing "missing access_token" branch (no change to `_validate()`'s own structure needed beyond the one call-site swap below).

- [ ] **Step 1: Remove the now-dead client identities from `src/config/client_info.py`**

Delete the entire `MOBILE_WEB = ClientInfo(...)` block and the entire `SMARTBOX = ClientInfo(...)` block from the `ClientType` class (keep `WEB` and `ANDROID_APP` — `ANDROID_APP`'s removal is deferred since other parts of the app may still reference it for non-login purposes; grep confirmed only `client.py`'s `_client_type` default and the two `LOGIN_CLIENT`-adjacent Helix calls need changing, both handled in Step 3 below).

- [ ] **Step 2: Write the failing test for `_browser_login()`**

Create `tests/test_auth_state_browser_login.py`:

```python
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.auth.auth_state import _AuthState


class TestBrowserLoginIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_browser_login_returns_access_token_from_manager(self):
        mock_twitch = MagicMock()
        auth_state = _AuthState(mock_twitch)

        with patch("src.auth.auth_state.BrowserLoginManager") as MockManager:
            instance = MockManager.return_value
            instance.start = AsyncMock(return_value=6990)
            instance.wait_for_cookie = AsyncMock(
                return_value={"auth-token": "real-token-value", "unique_id": "device-abc"}
            )
            instance.stop = AsyncMock()

            token = await auth_state._browser_login()

            self.assertEqual(token, "real-token-value")
            instance.start.assert_awaited_once()
            instance.wait_for_cookie.assert_awaited_once()
            instance.stop.assert_awaited_once()

    async def test_browser_login_stops_manager_even_on_failure(self):
        mock_twitch = MagicMock()
        auth_state = _AuthState(mock_twitch)

        with patch("src.auth.auth_state.BrowserLoginManager") as MockManager:
            instance = MockManager.return_value
            instance.start = AsyncMock(return_value=6990)
            instance.wait_for_cookie = AsyncMock(side_effect=RuntimeError("timed out"))
            instance.stop = AsyncMock()

            with self.assertRaises(RuntimeError):
                await auth_state._browser_login()

            instance.stop.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd /home/claude/twitchdrops && source venv/bin/activate && python -m pytest tests/test_auth_state_browser_login.py -v`
Expected: FAIL — `_browser_login` does not exist yet (AttributeError)

- [ ] **Step 4: Rewrite `src/auth/auth_state.py`**

Remove the entire historical comment block and the `LOGIN_CLIENT = ClientType.SMARTBOX` line (lines 16-90 in the current file, everything from `# 2026-09-19, GitHub issue #15...` through `LOGIN_CLIENT = ClientType.SMARTBOX`). Remove the `async def _oauth_login(self) -> str:` method entirely (the whole device-code polling implementation). Add this instead, right where `_oauth_login` used to be:

```python
    async def _browser_login(self) -> str:
        """
        Perform a real-browser Twitch login, replacing the retired OAuth
        device-code flow (see docs/superpowers/specs/
        2026-09-29-server-side-browser-login-design.md for why: device-code's
        only still-working client IDs either got GQL-rejected outright or
        scoped to a degraded campaign-visibility tier, intrinsic to how the
        token was minted).

        Drives a BrowserLoginManager: starts a real (non-headless) Chromium
        under a virtual display, surfaces its live view to the dashboard via
        the noVNC bridge (see src/web/app.py's /api/login/browser/* routes),
        and waits for the user to complete the real twitch.tv/login flow
        (credentials, 2FA, CAPTCHA -- all handled live by the user, never by
        this app). Always tears the manager down afterward, success or not.

        Returns:
            str: The access token (from the captured auth-token cookie)
        """
        from src.auth.browser_login import BrowserLoginManager

        login_form: LoginForm = self._twitch.gui.login
        manager = BrowserLoginManager()
        try:
            websocket_port = await manager.start()
            await login_form.start_browser_login(websocket_port)
            cookies = await manager.wait_for_cookie()
            self.device_id = cookies["unique_id"] or self.device_id
            self.access_token = cookies["auth-token"]
            return self.access_token
        finally:
            await manager.stop()
```

In the `headers()` method, remove the entire `if gql:` block's `LOGIN_CLIENT`-specific override (the block starting `if gql:` through `headers["Authorization"] = f"OAuth {self.access_token}"`). Replace it with:

```python
        if gql:
            # Login and browsing now share one real identity (ClientType.WEB
            # -- see src/core/client.py's _client_type), captured directly
            # from the real browser session (src/auth/browser_login.py), so
            # there is no separate login-client identity to reconcile headers
            # against anymore.
            headers["Authorization"] = f"OAuth {self.access_token}"
```

In `_validate()`, find this line:

```python
                    if "auth-token" not in cookie:
                        self.access_token = await self._oauth_login()
                        cookie["auth-token"] = self.access_token
```

Replace `self._oauth_login()` with `self._browser_login()`:

```python
                    if "auth-token" not in cookie:
                        self.access_token = await self._browser_login()
                        cookie["auth-token"] = self.access_token
```

Also find this later block in `_validate()`:

```python
                if validate_response["client_id"] == LOGIN_CLIENT.CLIENT_ID:
                    break
```

Replace with (the client_id now comes straight from whichever `ClientInfo` is browsing, no separate login client):

```python
                if validate_response["client_id"] == client_info.CLIENT_ID:
                    break
```

- [ ] **Step 5: Update `src/auth/__init__.py`**

```python
"""Authentication module for Twitch Drops Miner."""

from .auth_state import _AuthState


__all__ = ["_AuthState"]
```

- [ ] **Step 6: Update `src/core/client.py`**

Change the import (currently `from src.auth import LOGIN_CLIENT, _AuthState`):

```python
from src.auth import _AuthState
```

Change the client type default (currently `self._client_type: ClientInfo = ClientType.SMARTBOX`):

```python
        # 2026-09-29: WEB (not SMARTBOX/ANDROID_APP) -- matches the real
        # browser session's own identity now that login is a real
        # www.twitch.tv/login session (see src/auth/browser_login.py), so
        # there is no login-vs-browsing identity mismatch to work around
        # anymore (see auth_state.py's headers() for the header side of
        # this same fix).
        self._client_type: ClientInfo = ClientType.WEB
```

In `_fetch_followed_live_logins` (around the `client_id = LOGIN_CLIENT.CLIENT_ID` line), replace with:

```python
            client_id = self._client_type.CLIENT_ID
```

Do the same in `_fetch_subscribed_channels`'s matching line. In both places, also trim the comment above from explaining a LOGIN_CLIENT/browsing mismatch to noting there's no longer a mismatch to avoid:

```python
            # 2026-09-29: self._client_type is now the SAME identity the
            # access_token was minted under (a real browser session, see
            # auth_state.py's _browser_login) -- no separate LOGIN_CLIENT to
            # reconcile against anymore.
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd /home/claude/twitchdrops && source venv/bin/activate && python -m pytest tests/test_auth_state_browser_login.py -v`
Expected: PASS

Run the full existing suite to catch anything else referencing the removed names: `python -m pytest tests/ -v`
Expected: no failures related to `LOGIN_CLIENT`, `SMARTBOX`, `MOBILE_WEB`, or `_oauth_login`. If any test file references them, update it to match (there should be none — grep during planning found no test file referencing these names, only `tests/test_login_rate_limit.py` exists and it tests the unrelated dashboard-password rate limiter).

- [ ] **Step 8: Commit**

```bash
git add src/auth/auth_state.py src/auth/__init__.py src/config/client_info.py src/core/client.py tests/test_auth_state_browser_login.py
git commit -m "feat: wire _AuthState to BrowserLoginManager, retire device-code login entirely"
```

---

## Task 5: FastAPI endpoints + `LoginFormManager` rewrite

**Files:**
- Modify: `src/web/managers/login.py`
- Modify: `src/web/app.py`
- Test: `tests/test_login_browser_endpoints.py`

**Interfaces:**
- Consumes: `LoginFormManager.start_browser_login(websocket_port: int)` (new, added here — this is what Task 4's `_browser_login()` calls).
- Produces: `POST /api/login/browser/start`, `POST /api/login/browser/cancel`, `WS /api/login/browser/ws` — consumed by Task 6's frontend.

- [ ] **Step 1: Rewrite `src/web/managers/login.py`**

Replace the whole file:

```python
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
```

(`LoginData`, `submit_login()`, `clear()`, and `_login_event`/`_oauth_pending` are removed entirely — they only ever served the retired device-code/username-password flows, confirmed unused by any current code path other than the two API endpoints this task also removes below.)

- [ ] **Step 2: Write the failing tests for the new endpoints**

Create `tests/test_login_browser_endpoints.py`:

```python
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from src.web import app as app_module


class TestBrowserLoginEndpoints(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_module.app)
        app_module.gui_manager = MagicMock()
        app_module.gui_manager.login = MagicMock()
        self._manager_patcher = patch("src.web.app._browser_login_manager", None)
        self._manager_patcher.start()
        self.addCleanup(self._manager_patcher.stop)

    def test_start_rejects_when_gui_not_initialized(self):
        app_module.gui_manager = None
        response = self.client.post("/api/login/browser/start")
        self.assertEqual(response.status_code, 503)

    def test_start_rejects_a_second_concurrent_attempt(self):
        with patch("src.web.app.BrowserLoginManager") as MockManager:
            instance = MockManager.return_value
            instance.in_progress = False
            instance.start = AsyncMock(return_value=6990)

            first = self.client.post("/api/login/browser/start")
            self.assertEqual(first.status_code, 200)

            instance.in_progress = True
            second = self.client.post("/api/login/browser/start")
            self.assertEqual(second.status_code, 409)

    def test_cancel_calls_manager_cancel(self):
        with patch("src.web.app.BrowserLoginManager") as MockManager:
            instance = MockManager.return_value
            instance.in_progress = False
            instance.start = AsyncMock(return_value=6990)
            instance.cancel = MagicMock()

            self.client.post("/api/login/browser/start")
            response = self.client.post("/api/login/browser/cancel")
            self.assertEqual(response.status_code, 200)
            instance.cancel.assert_called_once()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /home/claude/twitchdrops && source venv/bin/activate && python -m pytest tests/test_login_browser_endpoints.py -v`
Expected: FAIL — `/api/login/browser/start` doesn't exist (404s)

- [ ] **Step 4: Remove the old device-code endpoints and add the new ones in `src/web/app.py`**

Delete the existing `@app.post("/api/login")` (`submit_login`) and `@app.post("/api/oauth/confirm")` (`confirm_oauth`) route functions entirely.

Add a module-level manager reference and the three new routes in their place. First, near the top of `src/web/app.py` where other module-level globals are declared (next to `gui_manager: WebGUIManager | None = None` and `twitch_client: Twitch | None = None`), add:

```python
from src.auth.browser_login import BrowserLoginManager

_browser_login_manager: BrowserLoginManager | None = None
```

Then add the three routes where the old ones were:

```python
@app.post("/api/login/browser/start")
async def start_browser_login():
    """Start a real-browser Twitch login attempt."""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    global _browser_login_manager
    if _browser_login_manager is not None and _browser_login_manager.in_progress:
        raise HTTPException(status_code=409, detail="A login attempt is already in progress")

    _browser_login_manager = BrowserLoginManager()
    try:
        websocket_port = await _browser_login_manager.start()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to start browser login: {exc}") from exc
    await gui_manager.login.start_browser_login(websocket_port)
    return {"success": True, "websocket_path": "/api/login/browser/ws"}


@app.post("/api/login/browser/cancel")
async def cancel_browser_login():
    """Cancel the in-progress real-browser login attempt, if any."""
    if _browser_login_manager is not None:
        _browser_login_manager.cancel()
    return {"success": True}


@app.websocket("/api/login/browser/ws")
async def browser_login_websocket(websocket: WebSocket):
    """Proxy raw bytes between the dashboard's noVNC client and the local
    websockify bridge for the in-progress browser login session."""
    await websocket.accept()
    if _browser_login_manager is None or not _browser_login_manager.in_progress:
        await websocket.close(code=4404)
        return

    port = _browser_login_manager._session.websocket_port  # type: ignore[union-attr]
    reader, writer = await asyncio.open_connection("localhost", port)

    async def pump_upstream():
        try:
            while True:
                data = await websocket.receive_bytes()
                writer.write(data)
                await writer.drain()
        except Exception:
            pass

    async def pump_downstream():
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                await websocket.send_bytes(data)
        except Exception:
            pass

    await asyncio.gather(pump_upstream(), pump_downstream(), return_exceptions=True)
    writer.close()
```

Confirm `WebSocket` is imported at the top of `src/web/app.py` (FastAPI's own class, from `fastapi import ... WebSocket`) — add it to the existing `from fastapi import ...` line if not already present. Confirm `asyncio` is already imported (it's used elsewhere in this large file already).

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /home/claude/twitchdrops && source venv/bin/activate && python -m pytest tests/test_login_browser_endpoints.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/web/managers/login.py src/web/app.py tests/test_login_browser_endpoints.py
git commit -m "feat: add browser-login API endpoints, remove device-code endpoints"
```

---

## Task 6: Frontend — vendor noVNC, replace the login panel

**Files:**
- Create: `web/static/vendor/novnc/rfb.js` (and its dependency chunks — see Step 1)
- Modify: `web/index.html`
- Modify: `web/static/app.js`
- Test: manual (browser-side interactive feature; see Task 7's checklist)

**Interfaces:**
- Consumes: `browser_login_ready` Socket.IO event (`{websocket_path: string}`, from Task 5), `login_status`'s existing shape (unchanged), `POST /api/login/browser/start`, `POST /api/login/browser/cancel` (Task 5).

- [ ] **Step 1: Vendor the noVNC JS client**

Download noVNC's core client bundle (the `@novnc/novnc` npm package's `core/rfb.js` and its internal module dependencies under `core/`) into `web/static/vendor/novnc/`. Preserve noVNC's own internal directory structure (`core/rfb.js` imports from `core/util/`, `core/decoders/`, etc.) exactly as published, so its internal `import` paths keep working unmodified — do not flatten or rename any of its files. Confirm the vendored version supports ES module `import` syntax (recent noVNC releases do), since `rfb.js` will be loaded as `<script type="module">` in Step 2.

- [ ] **Step 2: Replace the login panel HTML in `web/index.html`**

Replace this block (currently):

```html
                <!-- Login Section -->
                <section class="panel login-panel">
                    <h2>Login</h2>
                    <div id="login-status" class="login-status"></div>
                    <div id="login-form" style="display: none;">
                        <form onsubmit="return false;">
                        <input type="text" id="username" placeholder="Username" autocomplete="username" />
                        <input type="password" id="password" placeholder="Password" autocomplete="current-password" />
                        <input type="text" id="2fa-token" placeholder="2FA Token (optional)" />
                        <button id="login-button">Login</button>
                        </form>
                    </div>
                    <div id="oauth-code-display" style="display: none;">
                        <p>Enter this code at: <a id="oauth-url" href="#" target="_blank">Twitch Activate</a></p>
                        <div class="oauth-code" id="oauth-code"></div>
                        <button id="oauth-confirm">I've entered the code</button>
                    </div>
                </section>
```

with:

```html
                <!-- Login Section -->
                <section class="panel login-panel">
                    <h2>Login</h2>
                    <div id="login-status" class="login-status"></div>
                    <div id="browser-login-panel" style="display: none;">
                        <p id="browser-login-instructions">Log in to Twitch below. This is a real browser session running on the server -- your credentials never leave it.</p>
                        <div id="browser-login-canvas-container"></div>
                        <button id="browser-login-cancel">Cancel login</button>
                    </div>
                </section>
```

- [ ] **Step 3: Replace the login JS in `web/static/app.js`**

Replace the socket event handlers (currently):

```javascript
socket.on('login_required', () => {
    showLoginForm();
});

socket.on('oauth_code_required', (data) => {
    showOAuthCode(data.url, data.code);
});
```

with:

```javascript
socket.on('login_required', () => {
    // No-op on its own now -- browser_login_ready (below) is what actually
    // shows the panel, once the server-side session is ready to view.
});

socket.on('browser_login_ready', (data) => {
    showBrowserLoginPanel(data.websocket_path);
});
```

Remove the `socket.on('login_clear', ...)` handler entirely (it only ever cleared the retired username/password/2FA fields).

Replace the functions `showLoginForm`, `showOAuthCode`, and the relevant branch in `updateLoginStatus` (currently):

```javascript
function showLoginForm() {
    document.getElementById('login-form').style.display = 'block';
    document.getElementById('oauth-code-display').style.display = 'none';
}

function showOAuthCode(url, code) {
    document.getElementById('login-form').style.display = 'none';
    document.getElementById('oauth-code-display').style.display = 'block';
    document.getElementById('oauth-url').href = url;
    document.getElementById('oauth-code').textContent = code;
}

function updateLoginStatus(data) {
    const statusEl = document.getElementById('login-status');
    const loginPanel = document.querySelector('.login-panel');
    const t = state.translations;
    if (data.user_id) {
        const name = data.user_login || String(data.user_id);
        statusEl.innerHTML = `<span style="color:var(--success-color);font-weight:600;">✓ @${name}</span>`;
        statusEl.removeAttribute('translation-key');
        document.getElementById('login-form').style.display = 'none';
        document.getElementById('oauth-code-display').style.display = 'none';
        if (loginPanel) loginPanel.classList.add('is-logged-in');
    } else {
        const loggedOut = t.login?.status?.logged_out || 'Not logged in';
        statusEl.textContent = data.status || loggedOut;
        statusEl.setAttribute('translation-key', 'logged_out');
        statusEl.style.color = 'var(--text-secondary)';
        if (loginPanel) loginPanel.classList.remove('is-logged-in');
        if (data.oauth_pending) {
            showOAuthCode(data.oauth_pending.url, data.oauth_pending.code);
        }
    }
}
```

with:

```javascript
let browserLoginRfb = null;

function closeBrowserLoginPanel() {
    document.getElementById('browser-login-panel').style.display = 'none';
    // Null out BEFORE disconnect() so the 'disconnect' event handler (see
    // connectBrowserLoginRfb) sees browserLoginRfb === null and does not
    // schedule a reconnect for a close WE initiated.
    const rfb = browserLoginRfb;
    browserLoginRfb = null;
    if (rfb) {
        rfb.disconnect();
    }
    document.getElementById('browser-login-canvas-container').innerHTML = '';
}

async function showBrowserLoginPanel(websocketPath) {
    document.getElementById('browser-login-panel').style.display = 'block';
    await connectBrowserLoginRfb(websocketPath);
}

async function connectBrowserLoginRfb(websocketPath) {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${proto}//${window.location.host}${websocketPath}`;
    const { default: RFB } = await import('/static/vendor/novnc/rfb.js');
    browserLoginRfb = new RFB(document.getElementById('browser-login-canvas-container'), url);
    // A network blip must not abandon the login -- reconnect to the same
    // websockify bridge (the server-side Chromium session is untouched by
    // a WebSocket drop) as long as the panel is still open. Only a real
    // login_status success (see updateLoginStatus) or an explicit cancel
    // (see cancelBrowserLogin) calls closeBrowserLoginPanel, which nulls
    // browserLoginRfb out first -- that null check is what stops this from
    // reconnecting after either of those.
    browserLoginRfb.addEventListener('disconnect', () => {
        if (browserLoginRfb === null) return;
        setTimeout(() => {
            if (document.getElementById('browser-login-panel').style.display === 'block') {
                connectBrowserLoginRfb(websocketPath);
            }
        }, 2000);
    });
}

function updateLoginStatus(data) {
    const statusEl = document.getElementById('login-status');
    const loginPanel = document.querySelector('.login-panel');
    const t = state.translations;
    if (data.user_id) {
        const name = data.user_login || String(data.user_id);
        statusEl.innerHTML = `<span style="color:var(--success-color);font-weight:600;">✓ @${name}</span>`;
        statusEl.removeAttribute('translation-key');
        closeBrowserLoginPanel();
        if (loginPanel) loginPanel.classList.add('is-logged-in');
    } else {
        const loggedOut = t.login?.status?.logged_out || 'Not logged in';
        statusEl.textContent = data.status || loggedOut;
        statusEl.setAttribute('translation-key', 'logged_out');
        statusEl.style.color = 'var(--text-secondary)';
        if (loginPanel) loginPanel.classList.remove('is-logged-in');
        if (data.browser_login_ready) {
            showBrowserLoginPanel(data.browser_login_ready.websocket_path);
        }
    }
}
```

Replace `submitLogin` and `confirmOAuth` (currently near line 2830-2860) with a single cancel handler:

```javascript
async function cancelBrowserLogin() {
    try {
        await fetch(API_BASE + '/api/login/browser/cancel', { method: 'POST' });
    } catch (error) {
        console.error('Failed to cancel browser login:', error);
    }
    closeBrowserLoginPanel();
}
```

Find wherever `login-button` and `oauth-confirm` click listeners are wired up (search app.js for `'login-button'` and `'oauth-confirm'`) and replace both with a single listener wired to the new `browser-login-cancel` button:

```javascript
document.getElementById('browser-login-cancel').addEventListener('click', cancelBrowserLogin);
```

- [ ] **Step 4: Commit**

```bash
git add web/static/vendor/novnc/ web/index.html web/static/app.js
git commit -m "feat: replace device-code login UI with embedded noVNC browser-login panel"
```

---

## Task 7: Translations and documentation

**Files:**
- Modify: `lang/English.json`
- Modify: `README.md`
- Modify: `AGENTS.md`
- Test: none (documentation-only task)

**Interfaces:**
- Consumes: nothing new.

- [ ] **Step 1: Update `lang/English.json`'s `login` section**

Remove now-dead keys (`unexpected_content` and `error_code` and the individual incorrect-credential/2FA/email-code strings stay if still used elsewhere by non-login-flow code — grep `incorrect_login_pass`, `incorrect_email_code`, `incorrect_twofa_code`, `email_code_required`, `twofa_code_required` across `src/` before removing any of them, since they may be shared with other error-surfacing paths; only remove a key confirmed to have zero remaining references). At minimum, update `status.required`'s implied meaning (still fine as "Login required" — reused as-is for the browser-login-ready state) and add:

```json
"status": {
    "logged_in": "Logged in",
    "logged_out": "Logged out",
    "logging_in": "Logging in...",
    "required": "Login required",
    "waiting_auth": "Waiting for authentication..."
},
"browser_login": {
    "instructions": "Log in to Twitch below. This is a real browser session running on the server -- your credentials never leave it.",
    "cancel": "Cancel login"
}
```

Update `web/index.html`'s new panel to reference these via whatever this project's existing `translation-key` attribute convention is (check a few other elements in `index.html` for the exact `data-i18n`/`translation-key` pattern already used elsewhere and apply the same one to `#browser-login-instructions`'s text and `#browser-login-cancel`'s button label, instead of the hardcoded English text Task 6 introduced — Task 6 leaves them hardcoded specifically so this task's job is well-defined and separable: wire them to the existing i18n mechanism).

- [ ] **Step 2: Update `README.md`**

Find the section documenting authentication (search for "device code" or "OAuth" in `README.md`) and replace its description with the new real-browser login flow: the dashboard shows an embedded live browser view on first run (or whenever the session expires), the user completes a normal Twitch login there (including 2FA/CAPTCHA if Twitch asks for it), and TDM captures the resulting session automatically — no separate program to install, no device code to type in elsewhere.

Find the Docker section and note the larger image size (Playwright's Chromium + Xvfb/x11vnc/websockify) and the base image change (`python:3-alpine` → `python:3-slim`).

- [ ] **Step 3: Update `AGENTS.md`**

In the `### Authentication` section (currently describing "OAuth device code flow... Client info defined in src/config/client_info.py (presents as Android app...)"), replace with:

```markdown
### Authentication

- Real-browser login: a server-side Playwright Chromium session, embedded live in the dashboard via noVNC, replaces the retired OAuth device-code flow (see `docs/superpowers/specs/2026-09-29-server-side-browser-login-design.md`)
- Managed by `src/auth/auth_state.py` (`_AuthState` class) and `src/auth/browser_login.py` (`BrowserLoginManager`)
- Access tokens stored in `cookies.jar` in DATA_DIR, exactly as before
- Device ID from Twitch's `unique_id` cookie, captured from the same real browser session
- Client info defined in `src/config/client_info.py` — a single real `WEB` identity for both login and browsing (no separate login-vs-browsing client anymore)
```

In `### Key Design Decisions` (currently listing "OAuth device code flow - Works great for web-based deployment"), replace that line with:

```markdown
- **Real-browser login, server-side** - a virtual display + embedded noVNC view keeps login itself web-based (no desktop helper program), while avoiding device-code auth's token-scoping problems (issues #15-#18)
```

- [ ] **Step 4: Add a manual test checklist**

Create `docs/manual-testing-browser-login.md`:

```markdown
# Manual Testing: Real-Browser Login

Automated tests cover BrowserLoginManager's lifecycle logic and the API
endpoints with mocked Playwright/subprocess calls (see
tests/test_browser_login.py, tests/test_auth_state_browser_login.py,
tests/test_login_browser_endpoints.py). The actual interactive login flow
is inherently manual (real Twitch account, possibly real 2FA/CAPTCHA) --
run this checklist after any change touching src/auth/browser_login.py,
src/web/app.py's browser-login routes, or web/static/app.js's login panel.

- [ ] **Fresh login from an empty data dir**: delete/rename the data
      directory, start the app, confirm the dashboard shows the embedded
      browser-login panel, complete a real Twitch login in it (including
      2FA if your account has it), confirm the dashboard transitions to
      logged-in and campaigns load with full visibility (not the old
      SMARTBOX-degraded ~5-10 count).
- [ ] **Cancel mid-login**: start a login, click "Cancel login" before
      completing it. Confirm the panel closes, and check `ps aux` on the
      host/container for any lingering `Xvfb`/`chromium`/`x11vnc`/
      `websockify` process (there should be none within a few seconds).
- [ ] **Timeout with no user action**: start a login and leave it alone
      for the full timeout window (10 minutes). Confirm the dashboard
      shows an error/timed-out state and the same zombie-process check
      above stays clean.
- [ ] **Network blip during an active noVNC session**: start a login,
      open the browser devtools Network tab, and manually throttle/kill
      the WebSocket connection (or briefly disable networking on the
      client machine). Confirm the noVNC view reconnects on its own
      within a few seconds without the server-side Chromium session
      restarting (the in-progress Twitch login page state should still
      be there after reconnecting).
- [ ] **Container restart recovery**: start a login, then forcibly kill
      the container (`docker kill`, not a graceful stop) mid-login.
      Restart it and check the startup log for a
      "Cleaned up N orphaned browser-login process(es)" line (or confirm
      via `ps aux` that no Xvfb/x11vnc/websockify from the killed
      container survived).
- [ ] **Two dashboard tabs, both trigger login**: open the dashboard in
      two browser tabs while logged out, trigger login from both.
      Confirm the second gets a clear "already in progress" error rather
      than a second Chromium session.
```

- [ ] **Step 5: Commit**

```bash
git add lang/English.json web/index.html README.md AGENTS.md docs/manual-testing-browser-login.md
git commit -m "docs: update translations, README, AGENTS.md, and add manual test checklist for real-browser login"
```
