"""Authentication state management for Twitch Drops Miner."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast

import aiohttp
from yarl import URL

from src.config import COOKIES_PATH, ClientType
from src.i18n import _
from src.utils import CHARS_HEX_LOWER, create_nonce

# 2026-09-19, GitHub issue #15 + Discord reports (multiple users completely
# unable to log in on v1.5.4): the full saga on this constant --
#   v1.5.0-1.5.2: login used self._twitch._client_type (ANDROID_APP).
#     Twitch started hard-rejecting ANDROID_APP on the device-code endpoint
#     around 2026-09-18 -- confirmed by direct curl against
#     id.twitch.tv/oauth2/device just now: ANDROID_APP's client_id gets a
#     durable, 100%-reproducible `{"status":400,"message":"invalid
#     client"}`, not a transient/rate-limit response. Every affected user's
#     login retries forever and never succeeds.
#   v1.5.3: switched LOGIN_CLIENT to ClientType.SMARTBOX (confirmed live,
#     still works for the device-code grant). This let logins succeed
#     again, but confirmed+reproduced in #15: the resulting access token
#     gets scoped to SMARTBOX at Twitch's end regardless of which Client-Id
#     header later GQL requests send, silently limiting visible drop
#     campaigns (39 -> 3) -- the same failure shape as the historical
#     upstream DevilXD/TwitchDropsMiner#264 "SmartTV login" issue.
#   v1.5.4: reverted to ANDROID_APP entirely. This turned out to be worse
#     for anyone without an already-valid cookie: ANDROID_APP's block is
#     durable, not transient, so a fresh login now NEVER succeeds at all
#     (confirmed via Discord reports + a full log showing dozens of
#     consecutive "invalid client" retries over hours) -- a hard lockout is
#     worse than SMARTBOX's degraded-but-functional campaign discovery
#     (the historical #264 thread confirms even a SmartTV-scoped token
#     still tracks already-in-progress campaigns fine, only NEW campaign
#     discovery is limited).
#   v1.5.5 (this): direct curl-tested all four ClientType client_ids
#     against the real device-code endpoint just now. WEB and ANDROID_APP
#     both return "invalid client"; MOBILE_WEB and SMARTBOX both actually
#     work. MOBILE_WEB's client_id (r8s4dac0uhzifbpu9sjdiwzctle17ff) is
#     ALSO the exact client_id the historical #264 thread found restored
#     full campaign visibility after SmartTV's got crippled back then --
#     strong precedent, not just a guess, though not personally verified
#     end-to-end against a live account (that needs a human completing the
#     device-code activation). If MOBILE_WEB turns out to have the same
#     token-scoping problem as SMARTBOX, the next step is a client_id this
#     app has never tried at all, not cycling back to SMARTBOX or
#     ANDROID_APP -- both are now conclusively ruled out for one reason or
#     the other.
LOGIN_CLIENT = ClientType.MOBILE_WEB


if TYPE_CHECKING:
    from src.config import ClientInfo, JsonType
    from src.core.client import Twitch
    from src.web.gui_manager import LoginForm


logger = logging.getLogger("TwitchDrops")


class _AuthState:
    """
    Manages authentication state including tokens, session, and login flow.

    This class handles:
    - OAuth device code flow for authentication
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

    async def _oauth_login(self) -> str:
        """
        Perform OAuth device code flow authentication.

        This implements the OAuth 2.0 Device Authorization Grant flow:
        1. Request device code and user code from Twitch
        2. Display code to user for entry at twitch.tv/activate
        3. Poll token endpoint until user completes authorization
        4. Return access token

        Returns:
            str: The access token
        """
        login_form: LoginForm = self._twitch.gui.login
        client_info: ClientInfo = LOGIN_CLIENT
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "Accept-Language": "en-US",
            "Cache-Control": "no-cache",
            "Client-Id": client_info.CLIENT_ID,
            "Host": "id.twitch.tv",
            "Origin": str(client_info.CLIENT_URL),
            "Pragma": "no-cache",
            "Referer": str(client_info.CLIENT_URL),
            "User-Agent": client_info.USER_AGENT,
            "X-Device-Id": self.device_id,
        }
        payload = {
            "client_id": client_info.CLIENT_ID,
            "scopes": "",  # no scopes needed
        }
        while True:
            try:
                from datetime import datetime, timedelta, timezone

                from src.exceptions import RequestInvalid

                now = datetime.now(timezone.utc)
                async with self._twitch.request(
                    "POST", "https://id.twitch.tv/oauth2/device", headers=headers, data=payload
                ) as response:
                    # {
                    #     "device_code": "40 chars [A-Za-z0-9]",
                    #     "expires_in": 1800,
                    #     "interval": 5,
                    #     "user_code": "8 chars [A-Z]",
                    #     "verification_uri": "https://www.twitch.tv/activate?device-code=ABCDEFGH"
                    # }
                    # 2026-09-18, user-reported (this fork's issue #13-adjacent bug
                    # reports, upstream DevilXD/TwitchDropsMiner#1165/#1166): a non-200
                    # error body (no "device_code" key) used to get indexed into
                    # directly, crashing with a bare KeyError instead of surfacing what
                    # Twitch actually said. Surface it and retry with backoff instead.
                    if response.status != 200:
                        error_body = await response.text()
                        logger.error(
                            f"Device code request failed (HTTP {response.status}): "
                            f"{error_body}. Retrying in 30s."
                        )
                        await asyncio.sleep(30)
                        continue
                    response_json: JsonType = await response.json()
                    device_code: str = response_json["device_code"]
                    user_code: str = response_json["user_code"]
                    interval: int = response_json["interval"]
                    verification_uri: URL = URL(response_json["verification_uri"])
                    expires_at = now + timedelta(seconds=response_json["expires_in"])

                # Print the code to the user, open them the activate page so they can type it in
                await login_form.ask_enter_code(verification_uri, user_code)

                payload = {
                    "client_id": client_info.CLIENT_ID,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                }
                while True:
                    # sleep first, not like the user is gonna enter the code *that* fast
                    await asyncio.sleep(interval)
                    async with self._twitch.request(
                        "POST",
                        "https://id.twitch.tv/oauth2/token",
                        headers=headers,
                        data=payload,
                        invalidate_after=expires_at,
                    ) as response:
                        # 200 means success, 400 means the user haven't entered the code yet
                        if response.status != 200:
                            continue
                        response_json = await response.json()
                        # {
                        #     "access_token": "40 chars [A-Za-z0-9]",
                        #     "refresh_token": "40 chars [A-Za-z0-9]",
                        #     "scope": [...],
                        #     "token_type": "bearer"
                        # }
                        self.access_token = cast(str, response_json["access_token"])
                        return self.access_token
            except RequestInvalid:
                # the device_code has expired, request a new code
                continue

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
            # 2026-09-19, Discord-reported (v1.5.5, thermalux/Stumpn):
            # "GQLException: Unauthorized: The 'Authorization' token is
            # invalid" on every GQL call, immediately after a successful
            # login. Root cause: the Authorization token below is minted
            # under LOGIN_CLIENT (see its own comment in _oauth_login), but
            # this branch was sending it alongside Client-Id/Origin/Referer
            # from self._twitch._client_type (ANDROID_APP) instead --
            # Twitch's GQL gateway validates that the token and the
            # Client-Id/Origin/Referer identity it travels with actually
            # match. SMARTBOX (v1.5.3) hit a milder version of this same
            # mismatch -- silently limited campaign visibility (#15) rather
            # than an outright reject -- but MOBILE_WEB's token gets hard-
            # rejected on the exact same header inconsistency. Every
            # authenticated (gql=True) header now travels as ONE consistent
            # identity with the token, matching how a real client actually
            # behaves (it never mixes headers from two different apps).
            # GQLClient (see gql_client.py's request()) always passes its
            # OWN client_type's USER_AGENT as the `user_agent` override
            # above, which would otherwise leave User-Agent as ANDROID_APP
            # even after the Client-Id/Origin/Referer below are corrected --
            # forcing it here too so gql=True always sends one fully
            # consistent identity, not three matching headers plus a
            # mismatched fourth.
            headers["User-Agent"] = LOGIN_CLIENT.USER_AGENT
            headers["Client-Id"] = LOGIN_CLIENT.CLIENT_ID
            headers["Origin"] = str(LOGIN_CLIENT.CLIENT_URL)
            headers["Referer"] = str(LOGIN_CLIENT.CLIENT_URL)
            headers["Authorization"] = f"OAuth {self.access_token}"
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
                        self.access_token = await self._oauth_login()
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
                # in -- _oauth_login() always mints its token under LOGIN_CLIENT's
                # client_id, regardless of which client_info is used for browsing here.
                if validate_response["client_id"] == LOGIN_CLIENT.CLIENT_ID:
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

    def invalidate(self):
        """Invalidate the current access token."""
        self._delattrs("access_token")
