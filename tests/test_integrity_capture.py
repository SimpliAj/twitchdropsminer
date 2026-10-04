import asyncio
import base64
import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from src.auth.browser_login import (
    _capture_integrity_token,
    _decode_jwt_expiry,
    acquire_integrity_token,
)


def _fake_jwt(exp: int | None) -> str:
    payload = {} if exp is None else {"exp": exp}
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{body}.sig"


class TestDecodeJwtExpiry(unittest.TestCase):
    """
    Client-Integrity tokens are now minted by a real Chromium session
    (see acquire_integrity_token) and their own JWT "exp" claim is the
    only reliable signal for when to renew -- this replaces the old
    streamlink-based module's own expiry handling.
    """

    def test_reads_the_real_exp_claim(self):
        exp = int(time.time()) + 7200
        expiry = _decode_jwt_expiry(_fake_jwt(exp))
        self.assertAlmostEqual(expiry.timestamp(), exp, delta=1)

    def test_falls_back_to_a_default_ttl_when_not_a_jwt_at_all(self):
        before = datetime.now(timezone.utc)
        expiry = _decode_jwt_expiry("not-a-jwt-token")
        self.assertGreater(expiry, before + timedelta(hours=3))

    def test_falls_back_to_a_default_ttl_when_exp_claim_is_missing(self):
        before = datetime.now(timezone.utc)
        expiry = _decode_jwt_expiry(_fake_jwt(None))
        self.assertGreater(expiry, before + timedelta(hours=3))

    def test_falls_back_to_a_default_ttl_on_malformed_base64(self):
        before = datetime.now(timezone.utc)
        expiry = _decode_jwt_expiry("header.!!!not-base64!!!.sig")
        self.assertGreater(expiry, before + timedelta(hours=3))


class TestCaptureIntegrityToken(unittest.IsolatedAsyncioTestCase):
    """
    Twitch's own JS fires a gql.twitch.tv/integrity POST on a normal page
    load (confirmed live) -- this listens for that response on a page
    that's already navigating, rather than driving a separate toolchain
    to mint one from scratch.
    """

    def _fake_page(self):
        page = MagicMock()
        page.on = MagicMock()
        page.remove_listener = MagicMock()
        return page

    async def test_captures_the_token_from_a_matching_response(self):
        page = self._fake_page()
        exp = int(time.time()) + 3600
        token = _fake_jwt(exp)

        async def fake_json():
            return {"token": token}

        response = MagicMock()
        response.url = "https://gql.twitch.tv/integrity"
        response.json = fake_json

        task = asyncio.ensure_future(_capture_integrity_token(page))
        await asyncio.sleep(0)  # let the task run far enough to register page.on()
        # Simulate the response arriving: call the handler page.on() was given.
        on_response = page.on.call_args.args[1]
        await on_response(response)
        result = await task

        self.assertIsNotNone(result)
        captured_token, expiry = result
        self.assertEqual(captured_token, token)
        self.assertAlmostEqual(expiry.timestamp(), exp, delta=1)
        page.remove_listener.assert_called_once()

    async def test_ignores_unrelated_responses(self):
        page = self._fake_page()

        async def fake_json():
            return {"some": "other payload"}

        unrelated = MagicMock()
        unrelated.url = "https://gql.twitch.tv/gql"
        unrelated.json = fake_json

        with patch(
            "src.auth.browser_login.INTEGRITY_CAPTURE_TIMEOUT_SEC", 0.2
        ):
            task = asyncio.ensure_future(_capture_integrity_token(page))
            await asyncio.sleep(0)
            on_response = page.on.call_args.args[1]
            await on_response(unrelated)
            result = await task

        self.assertIsNone(result)

    async def test_times_out_to_none_when_nothing_arrives(self):
        page = self._fake_page()
        with patch("src.auth.browser_login.INTEGRITY_CAPTURE_TIMEOUT_SEC", 0.2):
            result = await _capture_integrity_token(page)
        self.assertIsNone(result)


class TestAcquireIntegrityToken(unittest.IsolatedAsyncioTestCase):
    """
    Regression coverage for the replacement of the streamlink-based
    approach: a real, throwaway Chromium session authenticated with the
    already-saved cookies, reusing the exact launch configuration the
    real-browser login itself already uses successfully.
    """

    def _mock_playwright_stack(self, capture_result):
        mock_page = AsyncMock()
        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_browser = AsyncMock()
        mock_browser.new_context = AsyncMock(return_value=mock_context)
        mock_playwright_instance = AsyncMock()
        mock_playwright_instance.chromium.launch = AsyncMock(return_value=mock_browser)
        mock_cm = AsyncMock()
        mock_cm.start = AsyncMock(return_value=mock_playwright_instance)
        return mock_cm, mock_browser, mock_context, mock_page

    async def test_returns_the_captured_token_on_a_real_display(self):
        expiry = datetime.now(timezone.utc) + timedelta(hours=4)
        mock_cm, mock_browser, mock_context, _page = self._mock_playwright_stack(("itok", expiry))

        with (
            patch("src.auth.browser_login._detect_real_display", return_value=7),
            patch("src.auth.browser_login.async_playwright", return_value=mock_cm),
            patch(
                "src.auth.browser_login._capture_integrity_token",
                new=AsyncMock(return_value=("itok", expiry)),
            ),
        ):
            result = await acquire_integrity_token("token123", "device-abc")

        self.assertEqual(result, ("itok", expiry))
        mock_context.add_cookies.assert_awaited_once()
        cookies = mock_context.add_cookies.call_args.args[0]
        names = {c["name"] for c in cookies}
        self.assertEqual(names, {"auth-token", "unique_id"})

    async def test_skips_the_device_id_cookie_when_blank(self):
        expiry = datetime.now(timezone.utc) + timedelta(hours=4)
        mock_cm, mock_browser, mock_context, _page = self._mock_playwright_stack(("itok", expiry))

        with (
            patch("src.auth.browser_login._detect_real_display", return_value=7),
            patch("src.auth.browser_login.async_playwright", return_value=mock_cm),
            patch(
                "src.auth.browser_login._capture_integrity_token",
                new=AsyncMock(return_value=("itok", expiry)),
            ),
        ):
            await acquire_integrity_token("token123", "")

        cookies = mock_context.add_cookies.call_args.args[0]
        names = {c["name"] for c in cookies}
        self.assertEqual(names, {"auth-token"})

    async def test_none_when_chromium_launch_fails(self):
        with (
            patch("src.auth.browser_login._detect_real_display", return_value=7),
            patch(
                "src.auth.browser_login.async_playwright",
                side_effect=RuntimeError("no playwright"),
            ),
        ):
            result = await acquire_integrity_token("token123", "device-abc")
        self.assertIsNone(result)

    async def test_tears_down_xvfb_when_it_spawned_one(self):
        expiry = datetime.now(timezone.utc) + timedelta(hours=4)
        mock_cm, mock_browser, mock_context, _page = self._mock_playwright_stack(("itok", expiry))
        xvfb_proc = MagicMock()

        with (
            patch("src.auth.browser_login._detect_real_display", return_value=None),
            patch("src.auth.browser_login._find_free_display_number", return_value=92),
            patch(
                "src.auth.browser_login._start_tagged_process",
                new=AsyncMock(return_value=xvfb_proc),
            ),
            patch("src.auth.browser_login._wait_for_display", new=AsyncMock()),
            patch("src.auth.browser_login.async_playwright", return_value=mock_cm),
            patch(
                "src.auth.browser_login._capture_integrity_token",
                new=AsyncMock(return_value=("itok", expiry)),
            ),
            patch(
                "src.auth.browser_login._terminate_process_group_gracefully", new=AsyncMock()
            ) as mock_terminate,
            patch("src.auth.browser_login._release_display_number") as mock_release,
        ):
            result = await acquire_integrity_token("token123", "device-abc")

        self.assertEqual(result, ("itok", expiry))
        mock_terminate.assert_awaited_once_with(xvfb_proc)
        mock_release.assert_called_once_with(92)


if __name__ == "__main__":
    unittest.main()
