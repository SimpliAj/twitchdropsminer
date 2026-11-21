import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.web.managers.settings import SettingsManager


def _manager(auto_prioritize):
    broadcaster = MagicMock()
    broadcaster.emit = AsyncMock()
    settings = MagicMock()
    settings.games_to_watch = ["A", "B"]
    settings.auto_prioritize = auto_prioritize
    settings.reward_priority_enabled = False
    manager = SettingsManager(broadcaster, settings, MagicMock(), on_change=MagicMock())
    manager._trigger_reload_debounced = MagicMock()
    return manager, settings


class TestGamesReorderReload(unittest.TestCase):
    def _save(self, manager, games):
        with patch("asyncio.create_task", side_effect=lambda coro: coro.close()):
            manager.update_settings({"games_to_watch": games})

    def test_reorder_with_auto_prioritize_saves_without_reload(self):
        manager, settings = _manager(True)
        self._save(manager, ["B", "A"])
        self.assertEqual(settings.games_to_watch, ["B", "A"])
        manager._trigger_reload_debounced.assert_not_called()

    def test_added_game_still_reloads(self):
        manager, _ = _manager(True)
        self._save(manager, ["B", "A", "C"])
        manager._trigger_reload_debounced.assert_called_once()

    def test_manual_order_matters_without_auto_prioritize(self):
        manager, _ = _manager(False)
        self._save(manager, ["B", "A"])
        manager._trigger_reload_debounced.assert_called_once()
