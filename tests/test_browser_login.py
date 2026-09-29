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
