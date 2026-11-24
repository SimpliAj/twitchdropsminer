import faulthandler
import subprocess
import sys
import tracemalloc
import unittest
from unittest.mock import MagicMock, patch

from src.services import memory_guard


class TestMemoryGuard(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        faulthandler.cancel_dump_traceback_later()

    def test_reads_this_process_on_linux(self):
        rss, total = memory_guard.memory()
        self.assertGreater(rss, 1024**2)
        self.assertGreater(total, rss)

    def test_limit(self):
        self.assertEqual(memory_guard.limit_for(905 * 1024**2), 452 * 1024**2 + 512 * 1024)
        self.assertEqual(memory_guard.limit_for(32 * 1024**3), memory_guard.MAX_LIMIT)
        self.assertEqual(memory_guard.limit_for(None), memory_guard.MAX_LIMIT)
        self.assertEqual(memory_guard.limit_for(256 * 1024**2), memory_guard.MIN_LIMIT)

    async def test_traces_from_half_and_restarts_over_the_limit(self):
        total = 4 * 1024**3  # limit 2 GB
        readings = iter([(512 * 1024**2, total), (1200 * 1024**2, total), (2100 * 1024**2, total)])
        restart = MagicMock()
        with patch.object(memory_guard, "CHECK_INTERVAL", 0):
            try:
                await memory_guard.run(restart, read=lambda: next(readings), available=lambda: total)
            finally:
                tracemalloc.stop()
        restart.assert_called_once()

    async def test_unknown_memory_never_restarts(self):
        restart = MagicMock()
        calls = {"n": 0}

        def read():
            calls["n"] += 1
            if calls["n"] > 3:
                raise StopAsyncIteration
            return None, None

        with patch.object(memory_guard, "CHECK_INTERVAL", 0), self.assertRaises(StopAsyncIteration):
            await memory_guard.run(restart, read=read, available=lambda: None)
        restart.assert_not_called()


class TestLowSystemMemory(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        faulthandler.cancel_dump_traceback_later()

    async def _run(self, rss, free):
        total = 905 * 1024**2
        readings = iter([(rss, total)])
        restart = MagicMock()

        def read():
            try:
                return next(readings)
            except StopIteration:
                raise StopAsyncIteration from None

        with patch.object(memory_guard, "CHECK_INTERVAL", 0), patch.object(memory_guard.tracemalloc, "start"):
            try:
                await memory_guard.run(restart, read=read, available=lambda: free)
            except StopAsyncIteration:
                pass
        return restart

    async def test_restarts_when_system_runs_out(self):
        (await self._run(200 * 1024**2, 40 * 1024**2)).assert_called_once()

    async def test_small_process_is_left_alone(self):
        (await self._run(100 * 1024**2, 40 * 1024**2)).assert_not_called()

    async def test_enough_free_memory_is_left_alone(self):
        (await self._run(200 * 1024**2, 300 * 1024**2)).assert_not_called()

    def test_reads_available_memory_on_linux(self):
        self.assertGreater(memory_guard.available_memory(), 0)


class TestStallDump(unittest.TestCase):
    def test_stuck_loop_dumps_the_stack_and_exits_when_supervised(self):
        code = (
            "import time\n"
            "from src.services import memory_guard\n"
            "memory_guard.STALL_TIMEOUT = 1\n"
            "memory_guard.arm_stall_dump()\n"
            "def stuck_here():\n"
            "    while True: time.sleep(0.05)\n"
            "stuck_here()\n"
        )
        env = {"INVOCATION_ID": "x", "PATH": "/usr/bin:/bin"}
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("stuck_here", out.stderr)
