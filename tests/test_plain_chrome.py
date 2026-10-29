import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.auth.plain_chrome import PlainChrome
from tests.fake_devtools import FakeDevTools


def _fake_proc():
    proc = MagicMock()
    proc.returncode = None
    proc.wait = AsyncMock(return_value=0)
    return proc


class TestPlainChrome(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.devtools = FakeDevTools()
        self.port = await self.devtools.start()
        self.proc = _fake_proc()
        self.exec = AsyncMock(return_value=self.proc)
        self.patches = [
            patch("src.auth.plain_chrome.free_port", return_value=self.port),
            patch("src.auth.plain_chrome.asyncio.create_subprocess_exec", new=self.exec),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        await self.devtools.stop()

    async def test_launches_a_plain_chrome_on_the_url_with_a_debug_port(self):
        chrome = PlainChrome("chrome.exe", "https://www.twitch.tv/login")
        await chrome.start()
        argv = self.exec.await_args.args
        self.assertEqual(argv[0], "chrome.exe")
        self.assertIn(f"--remote-debugging-port={self.port}", argv)
        self.assertEqual(argv[-1], "https://www.twitch.tv/login")
        self.assertNotIn("--no-sandbox", argv)
        self.assertTrue(chrome.alive)
        await chrome.close()

    async def test_no_sandbox_flag_is_opt_in(self):
        chrome = PlainChrome("chromium", "about:blank", no_sandbox=True)
        await chrome.start()
        self.assertIn("--no-sandbox", self.exec.await_args.args)
        await chrome.close()

    async def test_cookies_filters_by_domain(self):
        self.devtools.cookies = [
            {"name": "auth-token", "value": "tok", "domain": ".twitch.tv"},
            {"name": "other", "value": "x", "domain": ".example.com"},
        ]
        chrome = PlainChrome("chrome.exe", "about:blank")
        await chrome.start()
        self.assertEqual(await chrome.cookies("twitch.tv"), {"auth-token": "tok"})
        await chrome.close()

    async def test_not_alive_after_the_window_is_closed(self):
        chrome = PlainChrome("chrome.exe", "about:blank")
        await chrome.start()
        await self.devtools.close_browser()
        with self.assertRaises(Exception):
            await chrome.cookies("twitch.tv")
        self.assertFalse(chrome.alive)
        await chrome.close()

    async def test_close_is_idempotent_and_sends_browser_close(self):
        chrome = PlainChrome("chrome.exe", "about:blank")
        await chrome.start()
        await chrome.close()
        await chrome.close()
        self.assertIn("Browser.close", self.devtools.calls)

    async def test_start_fails_cleanly_when_chrome_exits_early(self):
        self.proc.returncode = 1
        with patch("src.auth.plain_chrome.free_port", return_value=1):  # nothing listens there
            chrome = PlainChrome("chrome.exe", "about:blank")
            with self.assertRaises(RuntimeError):
                await chrome.start(timeout=2)
        self.assertFalse(chrome.alive)


class TestStaleTmpFilter(unittest.TestCase):
    def test_age_and_ownership(self):
        from src.auth.plain_chrome import STALE_TMP_AGE_SEC, is_stale_tmp_entry

        now = 1_000_000.0
        old = now - STALE_TMP_AGE_SEC - 1
        self.assertTrue(is_stale_tmp_entry(old, 1000, now, 1000))
        self.assertFalse(is_stale_tmp_entry(now - 60, 1000, now, 1000))  # fresh: another instance
        self.assertFalse(is_stale_tmp_entry(old, 0, now, 1000))  # someone else's
        self.assertTrue(is_stale_tmp_entry(old, 0, now, None))  # no uid (Windows)

    def test_sweep_removes_only_old_matching_entries(self):
        import os
        import tempfile
        import time
        from pathlib import Path

        from src.auth.plain_chrome import STALE_TMP_AGE_SEC, sweep_stale_chrome_tmp

        with tempfile.TemporaryDirectory() as tmp, patch("tempfile.gettempdir", return_value=tmp):
            old = Path(tmp, "tdm-chrome-old")
            fresh = Path(tmp, "tdm-chrome-fresh")
            unrelated = Path(tmp, "other-old")
            chromium = Path(tmp, "org.chromium.Chromium.abc")
            for d in (old, fresh, unrelated, chromium):
                d.mkdir()
            past = time.time() - STALE_TMP_AGE_SEC - 60
            for d in (old, unrelated, chromium):
                os.utime(d, (past, past))
            self.assertEqual(sweep_stale_chrome_tmp(), 2)
            self.assertFalse(old.exists() or chromium.exists())
            self.assertTrue(fresh.exists() and unrelated.exists())
