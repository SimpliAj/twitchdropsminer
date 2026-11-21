import unittest
from unittest.mock import AsyncMock, patch

from src.auth import browser_login


class TestHiddenDesktopFallback(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        browser_login._hidden_desktop_failed = False

    tearDown = setUp

    async def _run(self, once):
        with patch.object(browser_login, "_IS_WINDOWS", True), \
                patch.object(browser_login, "_mint_integrity_token_plain_once", once):
            return await browser_login._mint_integrity_token_plain("chrome", {}, "tok", "dev")

    async def test_falls_back_to_an_offscreen_window_and_remembers_it(self):
        once = AsyncMock(side_effect=[RuntimeError("Chrome debug endpoint never came up"), ("t", None), ("t2", None)])
        self.assertEqual(await self._run(once), ("t", None))
        self.assertEqual([c.kwargs["hidden_desktop"] for c in once.await_args_list], [True, False])
        self.assertEqual(await self._run(once), ("t2", None))
        self.assertFalse(once.await_args_list[-1].kwargs["hidden_desktop"])

    async def test_working_hidden_desktop_is_used_once(self):
        once = AsyncMock(return_value=("t", None))
        self.assertEqual(await self._run(once), ("t", None))
        once.assert_awaited_once()
        self.assertTrue(once.await_args.kwargs["hidden_desktop"])

    async def test_a_rejected_mint_is_not_retried(self):
        once = AsyncMock(return_value=None)
        self.assertIsNone(await self._run(once))
        once.assert_awaited_once()
