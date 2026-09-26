"""
Playwright-based supplemental campaign discovery.

SMARTBOX's GQL client-id (used for login/auth, see LOGIN_CLIENT in
src/auth/auth_state.py) returns a reduced drop-campaign catalog compared to
Twitch's real web client -- confirmed live 2026-09-21: WEB's Client-Id hard-
fails the "ViewerDropsDashboard" query with "failed integrity check" (a
JS-based anti-bot challenge only a real/emulated browser can pass), while
SMARTBOX's own client-id succeeds but returns a visibly smaller catalog than
what the actual twitch.tv/drops/campaigns page shows a logged-in user. Since
there's no header/parameter-level fix for this (the integrity check is
enforced server-side against a token only a browser JS challenge can
produce), this module drives a real Chromium via Playwright to the
actual drops page, authenticated with the same OAuth token already in use,
and captures the REAL ViewerDropsDashboard network response Twitch's own web
client gets -- far more robust than screen-scraping rendered HTML, since it
reads the same GQL JSON the rest of this app already understands.

Supplemental only: results are merged ADDITIVELY into the existing SMARTBOX-
sourced campaign list (see InventoryService._fetch_inventory) -- SMARTBOX
remains the actual watching/mining client, this only widens what gets
discovered. Runs on the same cadence as the existing inventory reload cycle
(minimum_refresh_interval_minutes, default 30min via MaintenanceService), so
no separate scheduler/timer is needed.

Playwright/Chromium is a genuinely heavy optional dependency (a real browser
binary, ~280MB) that is NOT yet in the Docker image (see Dockerfile's
python:3-alpine base -- Playwright's bundled Chromium needs glibc, alpine
ships musl, so `pip install playwright` alone will not work there without
extra image work). Everything here degrades to a no-op with a log line
instead of raising, so a box without Playwright installed keeps working
exactly as before.

2026-09-26: launches headful (not headless -- see the launch() call's own
comment), which additionally needs a real X display. Outside a desktop
environment that means Xvfb: run `Xvfb :99 -screen 0 1280x1024x24 &` once
and export `DISPLAY=:99` for this process before starting it. No DISPLAY at
all makes Chromium fail to launch, which this module still only logs and
degrades from, same as a missing Playwright install.

2026-09-26, confirmed live on two separate boxes: headful alone is not
sufficient on typical VPS/datacenter hosting.
  - A datacenter-IP sandbox got all the way to a real page load and a real
    integrity-token mint attempt, but Twitch's own /integrity endpoint
    itself returned HTTP 429 on every attempt (reproduced 3x, several
    minutes apart -- not our own request volume). The same 429, from the
    same environment, was independently reproduced via the completely
    different streamlink-based approach in src/auth/integrity.py, which
    rules out this module's specific technique as the cause.
  - A second VPS (different provider, real non-snap Chrome, --no-sandbox,
    Xvfb confirmed healthy via xdpyinfo) never got that far: Chromium
    itself hung indefinitely in headful mode and never opened its CDP
    debug port, with or without --disable-gpu/--use-gl=swiftshader/longer
    timeouts. Headless Chrome on the identical box worked immediately --
    so this is a VPS/virtualized-display compatibility issue with headful
    Chromium specifically, unrelated to Twitch.
  - Net effect: this fix needs BOTH a working headful-Chromium environment
    AND an IP Twitch's integrity check doesn't already distrust. Neither
    of our two test VPS boxes had both. Untested but expected to work: a
    real desktop/home machine, which normally has both by default. Do not
    assume this "just works" on a fresh VPS deploy without verifying it
    end-to-end there first.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.core.client import Twitch


logger = logging.getLogger("TwitchDrops")

DROPS_PAGE_URL = "https://www.twitch.tv/drops/campaigns"
DISCOVERY_TIMEOUT_SEC = 45


async def discover_campaigns_via_browser(twitch: Twitch) -> list[dict[str, Any]]:
    """
    Returns a list of raw campaign dicts (same shape as currentUser.dropCampaigns
    entries from the GQL API) discovered via a real browser session, or an
    empty list if Playwright isn't available or discovery fails for any
    reason. Never raises -- a failure here must not block the existing
    SMARTBOX-based inventory fetch that calls this.
    """
    try:
        from playwright.async_api import Response, async_playwright
    except ImportError:
        logger.debug("Playwright not installed -- skipping browser-based campaign discovery")
        return []

    access_token = getattr(twitch._auth_state, "access_token", None)
    if not access_token:
        logger.debug("No access token available yet -- skipping browser-based campaign discovery")
        return []

    campaigns: list[dict[str, Any]] = []
    found = asyncio.Event()

    async def handle_response(response: Response) -> None:
        if found.is_set() or "gql.twitch.tv" not in response.url:
            return
        try:
            body = await response.json()
        except Exception:
            return
        # Persisted-query GQL responses don't reliably echo back the
        # operation name across every Twitch GQL gateway version -- matching
        # on the actual response SHAPE (currentUser.dropCampaigns) is more
        # robust than trusting request/response labeling.
        entries = body if isinstance(body, list) else [body]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            current_user = (entry.get("data") or {}).get("currentUser")
            if not current_user or "dropCampaigns" not in current_user:
                continue
            drop_campaigns = current_user.get("dropCampaigns") or []
            if drop_campaigns:
                campaigns.extend(drop_campaigns)
                found.set()
                return

    try:
        async with async_playwright() as p:
            # 2026-09-26, user-shared (rangermix/TwitchDropsMiner#121): a
            # Chromium launched headless is issued a client-integrity token
            # but Twitch then rejects it on the gated query -- only headful
            # (a real display, Xvfb in a container/headless box) passes.
            # Confirmed independently by another patch for the exact same
            # underlying anti-bot check this module works around. Requires
            # a DISPLAY (see docs/campaign-discovery-headful.md for the
            # Xvfb setup this needs outside a desktop environment).
            browser = await p.chromium.launch(headless=False)
            try:
                context = await browser.new_context()
                # Same OAuth token the rest of the app already authenticated
                # with (see auth_state.py's own auth-token cookie <->
                # access_token equivalence) -- no separate interactive login.
                await context.add_cookies(
                    [
                        {
                            "name": "auth-token",
                            "value": access_token,
                            "domain": ".twitch.tv",
                            "path": "/",
                        }
                    ]
                )
                page = await context.new_page()
                page.on("response", lambda r: asyncio.ensure_future(handle_response(r)))
                await page.goto(
                    DROPS_PAGE_URL,
                    wait_until="networkidle",
                    timeout=DISCOVERY_TIMEOUT_SEC * 1000,
                )
                try:
                    await asyncio.wait_for(found.wait(), timeout=DISCOVERY_TIMEOUT_SEC)
                except TimeoutError:
                    logger.warning(
                        "Browser-based campaign discovery: no ViewerDropsDashboard "
                        "response observed within timeout"
                    )
            finally:
                await browser.close()
    except Exception as exc:
        logger.warning(f"Browser-based campaign discovery failed, continuing without it: {exc}")
        return []

    logger.info(f"Browser-based campaign discovery found {len(campaigns)} campaign(s)")
    return campaigns
