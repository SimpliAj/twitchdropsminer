import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from src.config.settings import Settings
from src.web.managers.settings import SettingsManager


class TestSettingsReloadDebounce(unittest.IsolatedAsyncioTestCase):
    """Regression test for the idle-forever bug reported by thermalux/QFTFHT
    (Discord, 2026-08-27): every games_to_watch save fired on_change()
    (State.GAMES_UPDATE) synchronously and unconditionally. On reconnect with
    auto_prioritize on, the frontend re-sorts and re-saves once per campaign
    in inventory — 13-19 saves inside a single second in the captured logs —
    each restarting the full CHANNELS_CLEANUP/CHANNELS_FETCH/CHANNEL_SWITCH
    pipeline from scratch via change_state()'s unconditional overwrite of
    self._state, so the pipeline never reliably reached CHANNEL_SWITCH.
    """

    def _make_manager(self, delay: float = 0.02) -> tuple[SettingsManager, MagicMock]:
        mock_broadcaster = AsyncMock()
        mock_settings = MagicMock(spec=Settings)
        mock_settings.games_to_watch = []
        mock_settings.make_predictions = False
        mock_console = MagicMock()
        on_change = MagicMock()
        manager = SettingsManager(
            mock_broadcaster,
            mock_settings,
            mock_console,
            on_change=on_change,
            reload_debounce_seconds=delay,
        )
        return manager, on_change

    async def test_burst_of_saves_triggers_reload_once(self):
        manager, on_change = self._make_manager(delay=0.02)

        # Simulate a reconnect burst: many rapid games_to_watch saves, as
        # sortGamesByEndDate() does once per campaign in addCampaign().
        for i in range(15):
            manager.update_settings({"games_to_watch": [f"Game {i}"]})

        on_change.assert_not_called()  # nothing fires until the burst settles
        await asyncio.sleep(0.05)
        on_change.assert_called_once()

    async def test_single_save_still_triggers_reload(self):
        manager, on_change = self._make_manager(delay=0.02)

        manager.update_settings({"games_to_watch": ["Solo Game"]})

        on_change.assert_not_called()
        await asyncio.sleep(0.05)
        on_change.assert_called_once()

    async def test_no_change_does_not_trigger_reload(self):
        manager, on_change = self._make_manager(delay=0.02)

        manager.update_settings({"inventory_filters": {"show_upcoming": False}})

        await asyncio.sleep(0.05)
        on_change.assert_not_called()


if __name__ == "__main__":
    unittest.main()
