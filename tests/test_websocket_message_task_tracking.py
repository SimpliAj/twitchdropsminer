import asyncio
import unittest
from unittest.mock import MagicMock

from src.config.constants import WebsocketTopic
from src.websocket.websocket import Websocket


class TestWebsocketMessageTaskTracking(unittest.IsolatedAsyncioTestCase):
    """
    Regression test for GitHub issue #12 (memory growth + unresponsive
    process over long runs).

    _handle_message used to do a bare `asyncio.create_task(topic(...))`
    with the returned Task discarded immediately -- no strong reference
    kept, and no exception handling, so a raising topic handler completed
    with an unretrieved exception (retaining its traceback, which pins
    every frame/local along the way) that nothing ever consumed. This
    locks in the fix: the task is tracked in `_message_tasks` until it
    finishes, and a raising handler is caught and logged instead of
    leaving an unretrieved exception on the task.
    """

    def _make_websocket(self) -> Websocket:
        pool = MagicMock()
        pool._twitch = MagicMock()
        return Websocket(pool, index=0)

    async def test_message_task_is_tracked_then_discarded_on_success(self):
        ws = self._make_websocket()
        seen: list[dict] = []

        async def handler(data):
            seen.append(data)

        topic = WebsocketTopic("Channel", "StreamState", 111, lambda _tid, data: handler(data))
        ws.topics[str(topic)] = topic

        ws._handle_message({"data": {"topic": str(topic), "message": '{"hello": "world"}'}})

        # the task exists and is tracked immediately after dispatch...
        self.assertEqual(len(ws._message_tasks), 1)
        # ...and clears itself out of the tracking set once it completes.
        for _ in range(50):
            if not ws._message_tasks:
                break
            await asyncio.sleep(0)
        self.assertEqual(ws._message_tasks, set())
        self.assertEqual(seen, [{"hello": "world"}])

    async def test_raising_handler_is_logged_not_left_unretrieved(self):
        ws = self._make_websocket()

        async def handler(_data):
            raise ValueError("boom")

        topic = WebsocketTopic("Channel", "StreamState", 222, lambda _tid, data: handler(data))
        ws.topics[str(topic)] = topic

        with self.assertLogs("TwitchDrops.websocket", level="ERROR") as logs:
            ws._handle_message({"data": {"topic": str(topic), "message": "{}"}})
            for _ in range(50):
                if not ws._message_tasks:
                    break
                await asyncio.sleep(0)

        self.assertEqual(ws._message_tasks, set())
        self.assertTrue(any("boom" in record for record in logs.output))


if __name__ == "__main__":
    unittest.main()
