"""Authentication state management for Twitch Drops Miner."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, cast

import aiohttp

from src.config import COOKIES_PATH
from src.i18n import _
from src.utils import CHARS_HEX_LOWER, create_nonce


if TYPE_CHECKING:
    from src.config import ClientInfo, JsonType
    from src.core.client import Twitch
    from src.web.gui_manager import LoginForm


logger = logging.getLogger("TwitchDrops")

# How long to wait before offering a fresh login browser after an attempt
# timed out, was cancelled, or could not start at all (see _browser_login).
BROWSER_LOGIN_RETRY_DELAY_SEC = 5.0

# Client-Integrity token cache policy (see _ensure_integrity_token).
INTEGRITY_RENEW_MARGIN = timedelta(minutes=15)  # renew early so a GQL request never races the expiry
INTEGRITY_FAILURE_COOLDOWN = timedelta(minutes=5)  # don't hammer a failing acquisition every GQL request
INTEGRITY_FAILURE_COOLDOWN_MAX = timedelta(hours=6)  # consecutive failures double the cooldown up to this


class _AuthState:
    """
    Manages authentication state including tokens, session, and login flow.

    This class handles:
    - Real-browser login flow for authentication
    - Access token validation and management
    - Session and device ID management
    - Cookie persistence
    """

    def __init__(self, twitch: Twitch):
        self._twitch: Twitch = twitch
        self._lock = asyncio.Lock()
        self._logged_in = asyncio.Event()
        self.user_id: int
        self.device_id: str
        self.session_id: str
        self.access_token: str
        self.client_version: str
        # Client-Integrity token cache (see _ensure_integrity_token) -- these
        # three, unlike the attrs above, always exist (never deleted), since
        # they track a renewable cache rather than "are we logged in".
        self._integrity_token: str | None = None
        self._integrity_expires_at: datetime | None = None
        self._integrity_failed_until: datetime | None = None
        self._integrity_minted_at: datetime | None = None
        self._integrity_failures = 0  # consecutive failed acquisitions / quickly rejected tokens
        # serializes renewals: concurrent validate() calls would each launch a Chromium
        self._integrity_lock = asyncio.Lock()

    def _hasattrs(self, *attrs: str) -> bool:
        """Check if all specified attributes exist."""
        return all(hasattr(self, attr) for attr in attrs)

    def _delattrs(self, *attrs: str) -> None:
        """Delete all specified attributes if they exist."""
        for attr in attrs:
            if hasattr(self, attr):
                delattr(self, attr)

    def clear(self) -> None:
        """Clear all authentication state."""
        self._delattrs(
            "user_id",
            "device_id",
            "session_id",
            "access_token",
            "client_version",
        )
        self._logged_in.clear()

    async def _browser_login(self) -> str:
        """
        Log in to Twitch with a real, non-automated browser and return the
        access token. On a host with a desktop, BrowserLoginManager opens a
        plain Chrome window on the login page; on a headless host it waits
        for the login helper (scripts/tdm_login_helper.py, shown as a
        one-line command in the dashboard) to send the session.

        The manager is published as browser_login.get_active_manager() for
        the lifetime of each attempt, so the web layer's helper-session and
        cancel routes act on the very instance awaited here.

        Never lets a failed attempt escape: a timeout or a cancel (including
        the user closing the local window) surfaces as dashboard status and
        is retried with a fresh attempt, rather than propagating out through
        validate() -> client.run() into __main__'s fatal handler.
        """
        from src.auth import browser_login
        from src.auth.browser_login import (
            BrowserLoginCancelled,
            BrowserLoginManager,
            BrowserLoginTimeout,
        )

        login_form: LoginForm = self._twitch.gui.login
        while True:
            manager = BrowserLoginManager()
            browser_login.set_active_manager(manager)
            try:
                if await manager.start() == "local":
                    await login_form.start_browser_login_local()
                else:
                    await login_form.start_browser_login_remote()
                cookies = await manager.wait_for_cookie()
                self.device_id = cookies["unique_id"] or self.device_id
                self.access_token = cookies["auth-token"]
                return self.access_token
            except BrowserLoginTimeout:
                logger.warning("Browser login timed out; a new session will be offered")
                self._update_login_status(
                    login_form, "timed_out", "Login timed out - starting a new login session..."
                )
            except BrowserLoginCancelled:
                logger.info("Browser login cancelled; a new session will be offered")
                self._update_login_status(
                    login_form, "cancelled", "Login cancelled - starting a new login session..."
                )
            finally:
                browser_login.set_active_manager(None)
                await manager.stop()
            await asyncio.sleep(BROWSER_LOGIN_RETRY_DELAY_SEC)

    @staticmethod
    def _update_login_status(login_form: LoginForm, key: str, fallback: str) -> None:
        """Push a login status to the dashboard, tolerating language files
        that predate the key (only English.json is guaranteed to carry every
        one -- the translator does no English fallback merging)."""
        login_form.update(_.t["login"]["status"].get(key, fallback), None)

    def headers(self, *, user_agent: str = "", gql: bool = False) -> JsonType:
        """
        Build HTTP headers for Twitch API requests.

        Args:
            user_agent: Optional custom User-Agent string
            gql: If True, include GraphQL-specific headers

        Returns:
            Dictionary of HTTP headers
        """
        client_info: ClientInfo = self._twitch._client_type
        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip",
            "Accept-Language": "en-US",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "Client-Id": client_info.CLIENT_ID,
        }
        if user_agent:
            headers["User-Agent"] = user_agent
        if hasattr(self, "session_id"):
            headers["Client-Session-Id"] = self.session_id
        # if hasattr(self, "client_version"):
        # headers["Client-Version"] = self.client_version
        if hasattr(self, "device_id"):
            headers["X-Device-Id"] = self.device_id
        if gql:
            # Login and browsing now share one real identity (ClientType.WEB
            # -- see src/core/client.py's _client_type), captured directly
            # from the real browser session (src/auth/browser_login.py), so
            # there is no separate login-client identity to reconcile headers
            # against anymore.
            headers["Authorization"] = f"OAuth {self.access_token}"
            # See _ensure_integrity_token: ClientType.WEB's dropCampaigns
            # query is rejected outright without this. Omitted (not a hard
            # requirement here) when acquisition hasn't succeeded yet or is
            # in its failure cooldown -- the request still goes out and
            # fails the same "failed integrity check" way it always did
            # without this, not worse, and self-heals once a token is
            # acquired.
            if self._integrity_token is not None:
                headers["Client-Integrity"] = self._integrity_token
        return headers

    async def validate(self):
        """Thread-safe wrapper for _validate()."""
        async with self._lock:
            await self._validate()
        return self

    async def _validate(self):
        """
        Validate and restore authentication state.

        This method:
        1. Generates session ID if needed
        2. Extracts device ID from Twitch cookies
        3. Validates existing access token or initiates login flow
        4. Ensures token client ID matches expected client
        5. Saves validated cookies to disk

        Raises:
            RuntimeError: On repeated validation failures
        """
        if not hasattr(self, "session_id"):
            self.session_id = create_nonce(CHARS_HEX_LOWER, 16)
        if not self._hasattrs("device_id", "access_token", "user_id"):
            session = await self._twitch.get_session()
            jar = cast(aiohttp.CookieJar, session.cookie_jar)
            client_info: ClientInfo = self._twitch._client_type
        if not self._hasattrs("device_id"):
            async with self._twitch.request(
                "GET", client_info.CLIENT_URL, headers=self.headers()
            ) as response:
                page_html = await response.text("utf8")
                assert page_html is not None
            #     match = re.search(r'twilightBuildID="([-a-z0-9]+)"', page_html)
            # if match is None:
            #     raise MinerException("Unable to extract client_version")
            # self.client_version = match.group(1)
            # doing the request ends up setting the "unique_id" value in the cookie
            cookie = jar.filter_cookies(client_info.CLIENT_URL)
            self.device_id = cookie["unique_id"].value
        if not self._hasattrs("access_token", "user_id"):
            # looks like we're missing something
            login_form: LoginForm = self._twitch.gui.login
            logger.info("Checking login")
            login_form.update(_.t["login"]["status"]["logging_in"], None)
            for _client_mismatch_attempt in range(2):
                for _invalid_token_attempt in range(2):
                    cookie = jar.filter_cookies(client_info.CLIENT_URL)
                    if "auth-token" not in cookie:
                        self.access_token = await self._browser_login()
                        client_info = self._twitch._client_type
                        cookie = jar.filter_cookies(client_info.CLIENT_URL)
                        cookie["auth-token"] = self.access_token
                    elif not hasattr(self, "access_token"):
                        logger.info("Restoring session from cookie")
                        self.access_token = cookie["auth-token"].value
                    # validate the auth token, by obtaining user_id
                    async with self._twitch.request(
                        "GET",
                        "https://id.twitch.tv/oauth2/validate",
                        headers={"Authorization": f"OAuth {self.access_token}"},
                    ) as response:
                        if response.status == 401:
                            # the access token we have is invalid - clear the cookie and reauth
                            logger.info("Restored session is invalid")
                            assert client_info.CLIENT_URL.host is not None
                            jar.clear_domain(client_info.CLIENT_URL.host)
                            continue
                        elif response.status == 200:
                            validate_response = await response.json()
                            break
                else:
                    raise RuntimeError("Login verification failure (step #2)")
                # ensure the cookie's client ID matches the client actually used to log
                # in -- _browser_login() mints its token from the real browser session,
                # which is the same client_info used for browsing here.
                if validate_response["client_id"] == client_info.CLIENT_ID:
                    break
                # otherwise, we need to delete the entire cookie file and clear the jar
                logger.info("Cookie client ID mismatch")
                jar.clear()
                COOKIES_PATH.unlink(missing_ok=True)
            else:
                raise RuntimeError("Login verification failure (step #1)")
            self.user_id = int(validate_response["user_id"])
            self.user_login: str = validate_response.get("login", "")
            cookie["persistent"] = str(self.user_id)
            logger.info(f"Login successful, user ID: {self.user_id}, login: {self.user_login}")
            login_form.update(_.t["login"]["status"]["logged_in"], self.user_id, self.user_login)
            # update our cookie and save it
            jar.update_cookies(cookie, client_info.CLIENT_URL)
            jar.save(COOKIES_PATH)
        self._logged_in.set()
        await self._ensure_integrity_token()

    def _adopt_integrity_token(self, result: tuple[str, datetime] | None) -> None:
        """Common bookkeeping for a Client-Integrity acquisition attempt,
        however it was obtained (captured live during login, or minted
        standalone by _ensure_integrity_token). None means it failed."""
        if result is None:
            self._start_integrity_cooldown(datetime.now(timezone.utc))
            return
        if self._integrity_token is not None:
            self._integrity_failures = 0  # the previous token lived until its expiry
        self._integrity_token, self._integrity_expires_at = result
        self._integrity_failed_until = None
        self._integrity_minted_at = datetime.now(timezone.utc)

    def _start_integrity_cooldown(self, since: datetime) -> None:
        """Back off 5 min, 10, 20, ... (capped) while acquisition keeps failing."""
        delay = min(INTEGRITY_FAILURE_COOLDOWN * 2 ** min(self._integrity_failures, 10), INTEGRITY_FAILURE_COOLDOWN_MAX)
        self._integrity_failures += 1
        self._integrity_failed_until = since + delay
        logger.info(
            f"Client-Integrity failed {self._integrity_failures}x in a row; "
            f"next attempt after {self._integrity_failed_until:%H:%M:%S} UTC"
        )

    def reject_integrity_token(self) -> None:
        """Twitch answered IntegrityCheckFailed despite the cached token: drop
        it so the next validate() mints a fresh one, instead of reusing a
        rejected token until its expiry (up to INTEGRITY_DEFAULT_TTL). A token
        rejected within INTEGRITY_FAILURE_COOLDOWN of being minted also starts
        that cooldown, so a host whose fresh tokens keep failing (e.g. a
        flagged datacenter IP) doesn't launch a browser per request."""
        if self._integrity_token is None:
            return
        self._integrity_token = None
        self._integrity_expires_at = None
        if self._integrity_minted_at is not None:
            if datetime.now(timezone.utc) - self._integrity_minted_at > INTEGRITY_FAILURE_COOLDOWN:
                self._integrity_failures = 0  # it worked for a while, so this isn't a streak
            else:
                self._start_integrity_cooldown(self._integrity_minted_at)
        logger.warning("Client-Integrity token was rejected by Twitch; it will be renewed")

    async def _ensure_integrity_token(self) -> None:
        """Keep a Client-Integrity token cached and fresh for headers().

        dropCampaign / DropCampaignDetails reject the web identity with
        "failed integrity check" unless the request carries a token that
        Twitch's own page minted. browser_login.acquire_integrity_token()
        mints one in a throwaway plain Chrome with the saved session cookies
        (under Xvfb on headless hosts). Renewed before expiry, dropped and
        renewed early when Twitch rejects it (reject_integrity_token).

        Best-effort and silent on failure: GQL requests proceed without the
        header, which fails dropCampaigns the same way it would anyway, and
        it self-heals once acquisition succeeds again.
        """
        if not self._hasattrs("access_token", "device_id"):
            return
        from src.auth import browser_login

        if not self._integrity_renewal_due():
            return
        async with self._integrity_lock:
            if not self._integrity_renewal_due():
                return  # another caller renewed it while we waited
            logger.info("Renewing the Client-Integrity token (drives a real, throwaway Chromium)...")
            result = await browser_login.acquire_integrity_token(self.access_token, self.device_id)
            self._adopt_integrity_token(result)

    def _integrity_renewal_due(self) -> bool:
        now = datetime.now(timezone.utc)
        if (
            self._integrity_token is not None
            and self._integrity_expires_at is not None
            and now < self._integrity_expires_at - INTEGRITY_RENEW_MARGIN
        ):
            return False  # still fresh
        # don't hammer a failing acquisition every single GQL request
        return self._integrity_failed_until is None or now >= self._integrity_failed_until

    def invalidate(self):
        """Invalidate the current access token."""
        self._delattrs("access_token")
