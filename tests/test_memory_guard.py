import tracemalloc
import unittest
from unittest.mock import MagicMock, patch

from src.services import memory_guard


class TestMemoryGuard(unittest.IsolatedAsyncioTestCase):
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
                await memory_guard.run(restart, read=lambda: next(readings))
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
            await memory_guard.run(restart, read=read)
        restart.assert_not_called()
