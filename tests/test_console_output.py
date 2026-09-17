from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from src.web.managers.console import ConsoleOutputManager


class TestConsoleOutputCollapsing(unittest.IsolatedAsyncioTestCase):
    """
    Regression test (ported from upstream rangermix/TwitchDropsMiner #91,
    "collapse repeated no-campaign logs"): a keyed message repeated
    consecutively (e.g. the "no campaign available" status, which used to
    spam the console every tick while idle) is now suppressed after the
    first occurrence, until either the text under that key changes or a
    different message is printed in between.
    """

    async def test_identical_keyed_messages_are_collapsed(self):
        broadcaster = AsyncMock()
        output = ConsoleOutputManager(broadcaster)

        with patch("src.web.managers.console.logger") as logger:
            output.print("Waiting", collapse_key="status.no_campaign")
            output.print("Waiting", collapse_key="status.no_campaign")
            await asyncio.sleep(0)

        self.assertEqual(len(output.get_history()), 1)
        broadcaster.emit.assert_awaited_once()
        logger.info.assert_called_once_with("Waiting")

    async def test_intervening_message_resets_collapsing(self):
        broadcaster = AsyncMock()
        output = ConsoleOutputManager(broadcaster)

        output.print("Waiting", collapse_key="status.no_campaign")
        output.print("Campaign refresh started")
        output.print("Waiting", collapse_key="status.no_campaign")
        await asyncio.sleep(0)

        self.assertEqual(len(output.get_history()), 3)
        self.assertEqual(broadcaster.emit.await_count, 3)

    async def test_changed_text_with_same_key_is_emitted(self):
        broadcaster = AsyncMock()
        output = ConsoleOutputManager(broadcaster)

        output.print("Waiting", collapse_key="status.no_campaign")
        output.print("En attente", collapse_key="status.no_campaign")
        await asyncio.sleep(0)

        self.assertEqual(len(output.get_history()), 2)
        self.assertEqual(broadcaster.emit.await_count, 2)

    async def test_unkeyed_duplicates_keep_existing_behavior(self):
        broadcaster = AsyncMock()
        output = ConsoleOutputManager(broadcaster)

        output.print("Ordinary message")
        output.print("Ordinary message")
        await asyncio.sleep(0)

        self.assertEqual(len(output.get_history()), 2)
        self.assertEqual(broadcaster.emit.await_count, 2)


if __name__ == "__main__":
    unittest.main()
