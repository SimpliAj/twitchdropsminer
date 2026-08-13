import unittest
from unittest.mock import MagicMock, patch

from src.config.constants import WebsocketTopic
from src.exceptions import MinerException
from src.websocket.pool import WebsocketPool


def _topic(target_id: int) -> WebsocketTopic:
    return WebsocketTopic("Channel", "StreamState", target_id, lambda *_: None)


class TestWebsocketPoolTopicLimit(unittest.TestCase):
    """
    Regression test for the Sparx419 crash report (Discord, 2026-08-12):
    idle-channel StreamState subscription raised MinerException
    ("Maximum topics limit has been reached") uncaught in client.py's
    idle block, taking the whole miner down instead of degrading.

    The fix relies on WebsocketPool.add_topics filling everything it can
    BEFORE raising for the leftover — this test locks in that contract so
    a caller can safely catch-and-log instead of crashing.
    """

    def setUp(self):
        # shrink capacity so the test doesn't need hundreds of topics
        # WS_TOPICS_LIMIT is imported separately into pool.py (the leftover
        # check) and websocket.py (the per-socket fill loop) — both need
        # patching or the per-socket loop fills past what pool.py expects.
        patchers = [
            patch("src.websocket.pool.MAX_WEBSOCKETS", 2),
            patch("src.websocket.pool.WS_TOPICS_LIMIT", 3),
            patch("src.websocket.websocket.WS_TOPICS_LIMIT", 3),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        twitch = MagicMock()
        self.pool = WebsocketPool(twitch)

    def test_add_topics_raises_when_over_capacity(self):
        # capacity is 2 websockets * 3 topics = 6
        topics = [_topic(i) for i in range(6)]
        self.pool.add_topics(topics)  # fills exactly to capacity, no raise

        overflow = [_topic(100)]
        with self.assertRaises(MinerException):
            self.pool.add_topics(overflow)

    def test_topics_added_before_overflow_are_not_lost(self):
        topics = [_topic(i) for i in range(6)]
        self.pool.add_topics(topics)

        overflow = [_topic(100), _topic(101)]
        with self.assertRaises(MinerException):
            self.pool.add_topics(overflow)

        # the 6 that fit are still subscribed — catching the exception
        # in the caller loses nothing beyond what didn't fit
        total_subscribed = sum(len(ws.topics) for ws in self.pool.websockets)
        self.assertEqual(total_subscribed, 6)


if __name__ == "__main__":
    unittest.main()
