import unittest
from unittest.mock import MagicMock

from src.core.client import Twitch


def _make_channel(game):
    ch = MagicMock()
    ch.game = game
    return ch


class TestSkipGameRevert(unittest.TestCase):
    """
    Regression test for QFTFHT's report (Discord, 2026-08-13): using
    "Skip game" permanently excludes that game, with no way to revert.

    Root cause: _skipped_games only clears once every watchable game has
    been cycled through (or manual mode is entered) — no user-facing way
    to un-skip early. clear_skipped_games() is the fix, wired into the
    "Start Drop Mining" (/api/reload) action.
    """

    def setUp(self):
        self.twitch = Twitch(MagicMock())
        self.twitch.change_state = MagicMock()

    def test_skip_current_game_adds_to_skip_set(self):
        game = MagicMock(name="GameA")
        self.twitch.watching_channel.set(_make_channel(game))

        self.twitch.skip_current_game()

        self.assertIn(game, self.twitch._skipped_games)

    def test_clear_skipped_games_reverts_all_skips(self):
        game_a = MagicMock(name="GameA")
        game_b = MagicMock(name="GameB")
        self.twitch._skipped_games = {game_a, game_b}

        self.twitch.clear_skipped_games()

        self.assertEqual(self.twitch._skipped_games, set())

    def test_skip_persists_until_explicitly_cleared(self):
        # this is the bug: skipping one game doesn't self-heal on its own
        game = MagicMock(name="GameA")
        self.twitch.watching_channel.set(_make_channel(game))
        self.twitch.skip_current_game()

        # simulate time passing / other unrelated state changes
        self.twitch.skip_current_game()
        self.twitch.skip_current_game()

        self.assertIn(game, self.twitch._skipped_games)

        # only an explicit clear (wired to "Start Drop Mining") reverts it
        self.twitch.clear_skipped_games()
        self.assertNotIn(game, self.twitch._skipped_games)


if __name__ == "__main__":
    unittest.main()
