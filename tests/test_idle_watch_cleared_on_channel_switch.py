import unittest
from unittest.mock import MagicMock

from src.core.client import Twitch


class TestClearIdleWatch(unittest.TestCase):
    """
    Regression test for the report (temperance, Discord, 13.09.):

        "When drop farmed and 'parallel idle watch' is enabled in the
        settings, watch_service.py:38 outputs 'Watch sent OK (idle)'. This
        should not happen, as only drops should be watched under these
        conditions."

    Root cause: State.CHANNEL_SWITCH finding a real drop-eligible channel
    called self.watch(new_watching) but never cleared _idle_channels_set or
    removed the idle StreamState/Predictions/CommunityPoints topics left
    over from the previous idle period. watch_loop's idle-parallel block
    reads _idle_channels_set unconditionally every iteration regardless of
    app state, so those stale idle channels kept getting watched alongside
    the real drop channel forever (or until the next idle period).

    _clear_idle_watch() is the shared cleanup both the entry-into-idle
    branch and the leave-idle-for-real-farming branch now call -- this
    tests it directly rather than driving the whole state machine loop.
    """

    def _make_bare_twitch(self):
        # Twitch.__init__ needs a real Settings + spins up several services;
        # _clear_idle_watch only touches _idle_topic_ids/_idle_channels_set/
        # websocket.remove_topics, so a bare uninitialized instance with just
        # those three attributes set is enough to exercise the real method.
        twitch = Twitch.__new__(Twitch)
        twitch.websocket = MagicMock()
        return twitch

    def test_clears_topics_and_channel_set_when_populated(self):
        twitch = self._make_bare_twitch()
        twitch._idle_topic_ids = ["Channel.StreamState.111", "Channel.StreamState.222"]
        twitch._idle_channels_set = {MagicMock(), MagicMock()}

        Twitch._clear_idle_watch(twitch)

        twitch.websocket.remove_topics.assert_called_once_with(
            ["Channel.StreamState.111", "Channel.StreamState.222"]
        )
        self.assertEqual(twitch._idle_topic_ids, [])
        self.assertEqual(twitch._idle_channels_set, set())

    def test_no_op_when_nothing_to_clear(self):
        twitch = self._make_bare_twitch()
        twitch._idle_topic_ids = []
        twitch._idle_channels_set = set()

        Twitch._clear_idle_watch(twitch)

        twitch.websocket.remove_topics.assert_not_called()
        self.assertEqual(twitch._idle_topic_ids, [])
        self.assertEqual(twitch._idle_channels_set, set())


if __name__ == "__main__":
    unittest.main()
