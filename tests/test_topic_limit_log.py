import unittest

from src.services import watch_service


class TestTopicLimitLog(unittest.TestCase):
    """The topic limit stays hit all session; it used to warn on every
    channel switch (three lines every 30 minutes)."""

    def setUp(self):
        watch_service._topic_limit_warned.clear()

    def test_warns_once_per_kind_then_debug(self):
        with self.assertLogs("TwitchDrops", level="DEBUG") as logs:
            watch_service.log_topic_limit("Moments", "skipped for a")
            watch_service.log_topic_limit("Moments", "skipped for b")
            watch_service.log_topic_limit("Predictions", "skipped for a")
        self.assertEqual([r.levelname for r in logs.records], ["WARNING", "DEBUG", "WARNING"])
