"""Settings manager for application configuration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from src.i18n.translator import _
from src.models.game import Game


logger = logging.getLogger("TwitchDrops")


if TYPE_CHECKING:
    from src.config.settings import Settings
    from src.web.managers.broadcaster import WebSocketBroadcaster
    from src.web.managers.console import ConsoleOutputManager


class SettingsManager:
    """Manages application settings in the web interface.

    Provides access to and modification of user preferences including
    game priorities, proxy configuration, and UI preferences.
    """

    def __init__(
        self,
        broadcaster: WebSocketBroadcaster,
        settings: Settings,
        console: ConsoleOutputManager,
        on_change: Callable[[], None] | None = None,
        on_scheduler_change: Callable[[], None] | None = None,
        on_predictions_enable: Callable[[], None] | None = None,
    ):
        self._broadcaster = broadcaster
        self._settings = settings
        self._console = console
        self._on_change = on_change
        self._on_scheduler_change = on_scheduler_change
        self._on_predictions_enable = on_predictions_enable
        self._available_games: list[str] = []

    def get_settings(self) -> dict[str, Any]:
        """Get current settings for display.

        Returns:
            Dictionary containing all user-configurable settings
        """
        settings = vars(self._settings).copy()
        return settings

    def get_languages(self) -> dict[str, Any]:
        """Get available languages and current selection.

        Returns:
            Dictionary with available languages and current language
        """
        return {
            "available": _.get_languages(),
            "current": _.current_language,
        }

    def _log_change(self, message: str):
        """Log setting change to both console and system logger."""
        self._console.print(message)

    def update_settings(self, settings_data: dict[str, Any]):
        """Update settings from user input.

        Args:
            settings_data: Dictionary of settings to update
        """
        should_trigger_update = False
        should_trigger_update |= self.check_and_update_setting(
            "games_to_watch", settings_data.get("games_to_watch"), True
        )
        should_trigger_update |= self.check_and_update_setting(
            "dark_mode", settings_data.get("dark_mode")
        )
        should_trigger_update |= self.check_and_update_setting(
            "language", settings_data.get("language"), False, self._set_language
        )
        should_trigger_update |= self.check_and_update_setting(
            "connection_quality", settings_data.get("connection_quality")
        )
        if "proxy" in settings_data:
            proxy_value = settings_data["proxy"]
            should_trigger_update |= self.check_and_update_setting(
                "proxy",
                str(proxy_value).strip() if proxy_value else "",
                True,
                lambda proxy: self._log_change("Proxy cleared") if proxy == "" else None,
            )
        should_trigger_update |= self.check_and_update_setting(
            "minimum_refresh_interval_minutes",
            settings_data.get("minimum_refresh_interval_minutes"),
        )
        should_trigger_update |= self.check_and_update_setting(
            "inventory_filters", settings_data.get("inventory_filters")
        )
        should_trigger_update |= self.check_and_update_setting(
            "mining_benefits", settings_data.get("mining_benefits"), True
        )
        self.check_and_update_setting(
            "claim_channel_points", settings_data.get("claim_channel_points")
        )
        self.check_and_update_setting(
            "idle_channels", settings_data.get("idle_channels")
        )
        self.check_and_update_setting(
            "idle_use_followed", settings_data.get("idle_use_followed")
        )
        self.check_and_update_setting(
            "idle_parallel", settings_data.get("idle_parallel")
        )
        self.check_and_update_setting(
            "preferred_games", settings_data.get("preferred_games")
        )
        self.check_and_update_setting("scheduler_enabled", settings_data.get("scheduler_enabled"))
        self.check_and_update_setting("scheduler_start", settings_data.get("scheduler_start"))
        self.check_and_update_setting("scheduler_stop", settings_data.get("scheduler_stop"))
        if any(k in settings_data for k in ("scheduler_enabled", "scheduler_start", "scheduler_stop")):
            if self._on_scheduler_change:
                self._on_scheduler_change()
        self.check_and_update_setting(
            "discord_webhook_drops", settings_data.get("discord_webhook_drops")
        )
        self.check_and_update_setting(
            "discord_webhook_points", settings_data.get("discord_webhook_points")
        )
        self.check_and_update_setting(
            "discord_webhook_mentions", settings_data.get("discord_webhook_mentions")
        )
        self.check_and_update_setting(
            "drop_name_blacklist", settings_data.get("drop_name_blacklist")
        )
        self.check_and_update_setting(
            "auto_prioritize", settings_data.get("auto_prioritize")
        )
        self.check_and_update_setting(
            "auto_add_linked", settings_data.get("auto_add_linked")
        )
        self.check_and_update_setting(
            "tab_counter_enabled", settings_data.get("tab_counter_enabled")
        )
        prev_predictions = self._settings.make_predictions
        self.check_and_update_setting(
            "make_predictions", settings_data.get("make_predictions")
        )
        if not prev_predictions and self._settings.make_predictions and self._on_predictions_enable:
            self._on_predictions_enable()
        self.check_and_update_setting(
            "bet_strategy", settings_data.get("bet_strategy")
        )
        self.check_and_update_setting(
            "bet_percentage", settings_data.get("bet_percentage")
        )
        self.check_and_update_setting(
            "bet_max_points", settings_data.get("bet_max_points")
        )
        self.check_and_update_setting(
            "bet_minimum_points", settings_data.get("bet_minimum_points")
        )
        self.check_and_update_setting(
            "bet_percentage_gap", settings_data.get("bet_percentage_gap")
        )
        self.check_and_update_setting(
            "bet_delay_seconds", settings_data.get("bet_delay_seconds")
        )
        self.check_and_update_setting(
            "prediction_channels", settings_data.get("prediction_channels")
        )
        self.check_and_update_setting(
            "channel_strategies", settings_data.get("channel_strategies")
        )
        self.check_and_update_setting(
            "claim_moments", settings_data.get("claim_moments")
        )
        self.check_and_update_setting(
            "irc_chat_presence", settings_data.get("irc_chat_presence")
        )
        self.check_and_update_setting(
            "irc_mention_notify", settings_data.get("irc_mention_notify")
        )

        self._settings.save()
        asyncio.create_task(self._broadcaster.emit("settings_updated", self.get_settings()))

        if should_trigger_update and self._on_change:
            self._on_change()

    def check_and_update_setting(
        self,
        key: str,
        new_value: Any,
        should_trigger_update: bool = False,
        action: Callable[[Any], None] = lambda x: None,
    ):
        if new_value is None or getattr(self._settings, key, None) == new_value:
            return False
        setattr(self._settings, key, new_value)
        self._log_change(f"Setting changed: {key} = {new_value}")
        action(new_value)
        return should_trigger_update

    def _set_language(self, language: str):
        _.set_language(language)
        # Notify clients that translations need to be reloaded
        asyncio.create_task(self._broadcaster.emit("language_changed", {"language": language}))

    def set_games(self, games: set[Game]):
        """Update the list of available games for settings panel.

        Args:
            games: Set of Game objects discovered from campaigns
        """
        # Store and broadcast available games for settings panel
        game_names = sorted([g.name for g in games])
        self._available_games = game_names
        asyncio.create_task(self._broadcaster.emit("games_available", {"games": game_names}))
