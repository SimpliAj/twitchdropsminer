import asyncio
import re
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.auth.browser_login import (
    BrowserLoginCancelled,
    BrowserLoginManager,
    BrowserLoginTimeout,
    BrowserLoginUnavailable,
    COOKIE_POLL_INTERVAL_SEC,
    sweep_orphaned_processes,
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
        procs = [_mock_process(), _mock_process()]
        proc_iter = iter(procs)

        with (
            patch(
                "src.auth.browser_login._start_tagged_process",
                new=AsyncMock(side_effect=lambda argv: next(proc_iter)),
            ),
            patch("src.auth.browser_login._wait_for_display", new=AsyncMock()),
            patch("src.auth.browser_login._terminate_process_group") as mock_terminate,
            patch("src.auth.browser_login._release_display_number"),
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
            self.assertEqual(mock_terminate.call_count, 2)
            mock_browser.close.assert_awaited_once()

            # idempotent: calling again does nothing and doesn't raise
            await manager.stop()
            self.assertEqual(mock_terminate.call_count, 2)


class TestSweepOrphanedProcesses(unittest.IsolatedAsyncioTestCase):
    """
    sweep_orphaned_processes() had no coverage at all, which is how a real
    regression survived two review rounds: the pattern for one tracked
    process type changed while the sweep kept looking for the old form, so
    that process type was never swept again. Pin the patterns against the
    exact command lines BrowserLoginManager.start() generates -- see its
    Xvfb/x11vnc argv lists -- so a similar drift fails a test immediately.
    """

    OURS = {
        4001: "Xvfb :90 -screen 0 1280x800x24",
        4002: "x11vnc -display :90 -rfbport 5990 -localhost -nopw -forever -shared -quiet",
    }
    NOT_OURS = {
        5001: "Xvfb :0 -screen 0 1920x1080x24",
        5002: "x11vnc -display :0 -rfbport 5900 -localhost",
        5004: "/usr/bin/python3 main.py",
    }

    def _fake_pgrep(self, table):
        """Stand in for `pgrep -af <pattern>`, applying the pattern to a
        table of realistic command lines the way pgrep -f actually would."""

        async def create_subprocess_exec(*argv, **kwargs):
            self.assertEqual(argv[0], "pgrep")
            self.assertEqual(argv[1], "-af")
            pattern = argv[2]
            lines = [
                f"{pid} {cmdline}"
                for pid, cmdline in table.items()
                if re.search(pattern, cmdline) is not None
            ]
            proc = MagicMock()
            proc.communicate = AsyncMock(return_value=(("\n".join(lines) + "\n").encode(), b""))
            return proc

        return create_subprocess_exec

    async def test_kills_our_own_orphans_and_frees_their_display_slot(self):
        table = {**self.OURS, **self.NOT_OURS}
        with (
            patch("asyncio.create_subprocess_exec", new=self._fake_pgrep(table)),
            patch("src.auth.browser_login.os.kill") as mock_kill,
            patch("src.auth.browser_login._release_display_number") as mock_release,
        ):
            killed = await sweep_orphaned_processes()

        killed_pids = {call.args[0] for call in mock_kill.call_args_list}
        self.assertEqual(killed_pids, set(self.OURS))
        self.assertEqual(killed, len(self.OURS))
        # SIGKILL leaves /tmp/.X90-lock behind, which would otherwise burn
        # that display slot permanently.
        mock_release.assert_called_once_with(90)

    async def test_ignores_processes_outside_the_reserved_ranges(self):
        with (
            patch("asyncio.create_subprocess_exec", new=self._fake_pgrep(self.NOT_OURS)),
            patch("src.auth.browser_login.os.kill") as mock_kill,
            patch("src.auth.browser_login._release_display_number") as mock_release,
        ):
            killed = await sweep_orphaned_processes()

        self.assertEqual(killed, 0)
        mock_kill.assert_not_called()
        mock_release.assert_not_called()


class TestDisplayNumberSlotIsReleased(unittest.IsolatedAsyncioTestCase):
    async def test_stop_removes_the_x_lock_and_socket(self):
        manager = BrowserLoginManager()
        with (
            patch(
                "src.auth.browser_login._start_tagged_process",
                new=AsyncMock(side_effect=lambda argv: _mock_process()),
            ),
            patch("src.auth.browser_login._wait_for_display", new=AsyncMock()),
            patch("src.auth.browser_login._terminate_process_group"),
            patch("src.auth.browser_login.os.unlink") as mock_unlink,
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
            display_number = manager._session.display_number  # type: ignore[union-attr]
            await manager.stop()

        unlinked = {call.args[0] for call in mock_unlink.call_args_list}
        self.assertEqual(
            unlinked,
            {f"/tmp/.X{display_number}-lock", f"/tmp/.X11-unix/X{display_number}"},
        )


class TestWebsocketPortProperty(unittest.IsolatedAsyncioTestCase):
    async def test_is_none_when_no_session_is_active(self):
        self.assertIsNone(BrowserLoginManager().websocket_port)

    async def test_matches_the_port_start_returned(self):
        manager = BrowserLoginManager()
        with (
            patch(
                "src.auth.browser_login._start_tagged_process",
                new=AsyncMock(side_effect=lambda argv: _mock_process()),
            ),
            patch("src.auth.browser_login._wait_for_display", new=AsyncMock()),
            patch("src.auth.browser_login._terminate_process_group"),
            patch("src.auth.browser_login._release_display_number"),
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
                port = await manager.start()
            self.assertEqual(manager.websocket_port, port)
            await manager.stop()
            self.assertIsNone(manager.websocket_port)


if __name__ == "__main__":
    unittest.main()
