from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

from yarl import URL

from src.config import DEFAULT_LANG, SETTINGS_PATH
from src.utils import json_load, json_save


class InventoryFilters(TypedDict):
    game_name_search: list[str]
    show_active: bool
    show_benefit_badge: bool
    show_benefit_emote: bool
    show_benefit_item: bool
    show_benefit_other: bool
    show_expired: bool
    show_finished: bool
    show_linked: bool
    show_not_linked: bool
    show_sub_drops: bool
    show_upcoming: bool


default_settings = {
    "connection_quality": 1,
    "dark_mode": False,
    "games_to_watch": [],
    "language": DEFAULT_LANG,
    "inventory_list_view": False,
    "inventory_filters": {
        "game_name_search": [],
        "show_active": False,
        "show_benefit_badge": True,
        "show_benefit_emote": True,
        "show_benefit_item": True,
        "show_benefit_other": True,
        "show_expired": False,
        "show_finished": False,
        "show_sub_drops": False,
        "show_linked": False,
        "show_not_linked": False,
        "show_upcoming": True,
    },
    "minimum_refresh_interval_minutes": 30,
    "mining_benefits": {
        "BADGE": True,
        "DIRECT_ENTITLEMENT": True,
        "EMOTE": True,
        "UNKNOWN": True,
    },
    "proxy": "",
    "claim_channel_points": True,
    "idle_channels": [],
    "idle_parallel": True,
    "idle_use_followed": False,
    "preferred_games": [],
    "scheduler_enabled": False,
    "scheduler_start": "22:00",
    "scheduler_stop": "08:00",
    "discord_webhook_drops": "",
    "discord_webhook_points": "",
    "drop_name_blacklist": [],
    "blacklisted_drop_ids": [],
    "ignored_campaign_ids": [],
    "auto_prioritize": False,
    "auto_add_linked": False,
    "auto_add_excluded_games": [],
    "tab_counter_enabled": True,
    "claim_moments": True,
    "irc_chat_presence": True,
    "discord_webhook_mentions": "",
    "irc_mention_notify": True,
    "make_predictions": False,
    "bet_strategy": "SMART",
    "bet_percentage": 5,
    "bet_max_points": 50000,
    "bet_minimum_points": 1000,
    "bet_percentage_gap": 20,
    "bet_delay_seconds": 30,
    "prediction_channels": [],
    "channel_strategies": {},
}


@dataclass
class Settings:
    connection_quality: int
    dark_mode: bool
    games_to_watch: list[str]
    language: str
    inventory_list_view: bool
    inventory_filters: InventoryFilters
    minimum_refresh_interval_minutes: int
    mining_benefits: dict[str, bool]
    proxy: str
    claim_channel_points: bool
    idle_channels: list[str]
    idle_parallel: bool
    idle_use_followed: bool
    preferred_games: list[str]
    scheduler_enabled: bool
    scheduler_start: str
    scheduler_stop: str
    discord_webhook_drops: str
    discord_webhook_points: str
    drop_name_blacklist: list[str]
    blacklisted_drop_ids: list[str]
    ignored_campaign_ids: list[str]
    auto_prioritize: bool
    auto_add_linked: bool
    auto_add_excluded_games: list[str]
    tab_counter_enabled: bool
    claim_moments: bool
    irc_chat_presence: bool
    discord_webhook_mentions: str
    irc_mention_notify: bool
    make_predictions: bool
    bet_strategy: str
    bet_percentage: int
    bet_max_points: int
    bet_minimum_points: int
    bet_percentage_gap: int
    bet_delay_seconds: int
    prediction_channels: list[str]
    channel_strategies: dict[str, str]

    def __init__(self):
        self.load()

    def load(self):
        # TODO: remvoe customized serde in the future
        settings = json_load(SETTINGS_PATH, default_settings, merge=True)
        for key, value in settings.items():
            if value is URL:
                setattr(self, key, str(value))
            else:
                setattr(self, key, value)

    def save(self) -> None:
        json_save(SETTINGS_PATH, vars(self), sort=True)
