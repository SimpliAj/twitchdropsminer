from __future__ import annotations

import json
import logging
from typing import TypedDict, cast

from src.config import DEFAULT_LANG, LANG_PATH


class StatusMessages(TypedDict):
    terminated: str
    watching: str
    goes_online: str
    goes_offline: str
    claimed_drop: str
    no_channel: str
    no_campaign: str


class LoginStatus(TypedDict):
    logged_in: str
    logged_out: str
    logging_in: str
    required: str
    waiting_auth: str


class LoginMessages(TypedDict):
    error_code: str
    unexpected_content: str
    email_code_required: str
    twofa_code_required: str
    incorrect_login_pass: str
    incorrect_email_code: str
    incorrect_twofa_code: str
    status: LoginStatus


class ErrorMessages(TypedDict):
    captcha: str
    no_connection: str
    site_down: str


class GUIStatus(TypedDict):
    name: str
    idle: str
    ready: str
    exiting: str
    terminated: str
    cleanup: str
    gathering: str
    switching: str
    fetching_inventory: str
    fetching_campaigns: str
    adding_campaigns: str


class GUITabs(TypedDict):
    main: str
    inventory: str
    settings: str
    help: str
    system: str
    stats: str
    analytics: str


class GUILoginForm(TypedDict):
    name: str
    labels: str
    request: str
    username: str
    password: str
    twofa_code: str
    button: str
    oauth_prompt: str
    oauth_activate: str
    oauth_confirm: str


class GUIWebsocket(TypedDict):
    name: str
    websocket: str
    initializing: str
    connected: str
    disconnected: str
    connecting: str
    disconnecting: str
    reconnecting: str


class GUIProgress(TypedDict):
    name: str
    drop: str
    game: str
    campaign: str
    remaining: str
    drop_progress: str
    campaign_progress: str
    no_drop: str
    return_to_auto: str
    manual_mode_info: str


class GUIChannels(TypedDict):
    name: str
    online: str
    pending: str
    offline: str
    no_channels: str
    no_channels_for_games: str
    channel_count: str
    channel_count_plural: str
    viewers: str


class GUIFooter(TypedDict):
    version: str
    loading: str
    update_available: str


class GUIBadgeItem(TypedDict):
    title: str


class GUIBadges(TypedDict):
    manual: GUIBadgeItem
    auto: GUIBadgeItem
    proxy: GUIBadgeItem


class GUIWanted(TypedDict):
    name: str
    none: str


class GUIInvFilters(TypedDict):
    active: str
    not_linked: str
    upcoming: str
    expired: str
    finished: str
    item: str
    badge: str
    emote: str
    other: str
    clear: str
    search_placeholder: str


class GUIInvStatus(TypedDict):
    active: str
    expired: str
    upcoming: str
    claimed: str


class GUIInventory(TypedDict):
    no_campaigns: str
    status: GUIInvStatus
    starts: str
    ends: str
    claimed_drops: str
    filters: GUIInvFilters
    filter_linked: str
    no_campaigns_filtered: str
    linked_badge: str
    linked_badge_title: str
    link_account_badge: str
    link_account_title: str
    ignored_badge: str
    ignored_badge_title: str
    ignore_btn: str
    unignore_btn: str
    ignore_title: str
    unignore_title: str
    farming_label: str
    skipped_label: str
    farm_action_label: str
    skip_action_label: str
    farm_title_all: str
    farm_title_skip: str
    farm_title_farm: str
    drop_singular: str
    drop_plural: str
    drops_left_suffix: str


class GUISettingsGeneral(TypedDict):
    name: str
    dark_mode: str


class GUISettings(TypedDict):
    general: GUISettingsGeneral
    mining_benefits: str
    mining_benefits_help: str
    reload: str
    reload_campaigns: str
    games_to_watch: str
    games_help: str
    search_games: str
    add_game: str
    add_game_hint: str
    select_all: str
    deselect_all: str
    selected_games: str
    available_games: str
    no_games_selected: str
    no_games_match: str
    all_games_selected: str
    actions: str
    connection_quality: str
    minimum_refresh: str
    select_linked: str
    password_header: str
    password_current_label: str
    password_current_placeholder: str
    password_new_label: str
    password_new_placeholder: str
    password_confirm_label: str
    password_confirm_placeholder: str
    password_save: str
    password_disable: str
    password_saved: str
    password_disabled_msg: str
    password_mismatch: str
    password_status_active: str
    password_status_inactive: str
    discord_bot: dict
    notifications_header: str
    push_enabled: str
    push_sound: str
    campaign_end_alerts_enabled: str
    # New i18n keys
    channel_points_section: str
    channel_points_auto_claim: str
    channel_points_auto_claim_help: str
    discord_notifications: str
    discord_drops_webhook_label: str
    discord_drops_webhook_help: str
    discord_points_webhook_label: str
    discord_points_webhook_help: str
    test_webhook: str
    proxy_url_label: str
    set_proxy: str
    verify_proxy: str
    proxy_url_help: str
    idle_watch: str
    idle_watch_help: str
    idle_auto_followed: str
    idle_auto_followed_help: str
    idle_channel_placeholder: str
    idle_channel_add: str
    blacklist: str
    blacklist_help: str
    scheduler: str
    scheduler_help: str
    scheduler_enable: str
    scheduler_active_from: str
    scheduler_active_until: str
    scheduler_times_help: str
    bot_notification_channels: str
    bot_setchannel_hint: str
    add_account: str
    account_label_placeholder: str
    bet_percentage_gap_label: str
    blacklist_ids_header: str
    blacklist_ids_help: str
    blacklisted_games_header: str
    blacklisted_games_help: str


class GUISystem(TypedDict):
    header: str
    accounts_header: str
    miner_header: str
    miner_desc: str
    reload_btn: str
    restart_header: str
    restart_desc: str
    restart_btn: str
    session_header: str
    session_desc: str
    logout_btn: str
    reload_ok: str
    restart_confirm: str
    restart_ok: str


class GUIAnalytics(TypedDict):
    stats_header: str
    total_claims: str
    games_label: str
    last_claim: str
    claims_by_game: str
    claims_activity: str
    channel_points: str
    refresh: str
    no_channel_points: str
    channel_filter_all: str
    channel_filter_followed: str
    channel_filter_subscribed: str
    channel_filter_title: str
    no_followed_channels: str
    no_subscribed_channels: str
    drop_history: str
    no_history: str
    unknown_result_tooltip: str


class GUIHelp(TypedDict):
    about: str
    about_text: str
    how_to_use: str
    how_to_use_items: list[str]
    features: str
    features_items: list[str]
    important_notes: str
    important_notes_items: list[str]
    github_repo: str
    contents: str


class GUIHeader(TypedDict):
    title: str
    language: str
    initializing: str
    auto_mode: str
    manual_mode: str
    connected: str
    disconnected: str


class GUIDropsToday(TypedDict):
    title: str
    aria_label: str
    close_aria: str
    loading: str
    load_failed: str
    empty: str
    subtitle: str


class GUICampaignModal(TypedDict):
    subtitle_remaining: str
    subtitle_all: str
    empty_remaining: str
    empty_all: str
    badge_claimed: str
    badge_earned: str
    badge_earned_title: str
    badge_claim_now: str
    badge_percent_left: str


class GUIAccountsManager(TypedDict):
    aria_label: str
    close_aria: str
    title: str
    subtitle: str
    fleet_status_heading: str
    loading_fleet: str
    load_failed: str
    select_accounts_heading: str
    bulk_actions_heading: str
    bulk_settings_heading: str
    no_accounts_registered: str
    no_accounts_for_actions: str
    no_accounts_for_settings: str
    select_all_accounts: str
    table_account: str
    table_status: str
    table_watching: str
    table_drops_today: str
    table_last_active: str
    status_unreachable: str
    status_paused: str
    start_idle_btn: str
    start_drop_mining_btn: str
    pause_all_btn: str
    starting: str
    pausing: str
    select_one_account: str
    request_failed: str
    result_idle_watching: str
    result_searching_drops: str
    result_paused: str
    result_failed: str
    account_fallback_label: str
    values_placeholder: str
    mode_add: str
    mode_remove: str
    mode_replace: str
    apply_btn: str
    applying: str
    enter_one_value: str
    confirm_replace: str
    result_updated: str


class GUIUpdateModal(TypedDict):
    title: str
    cancel: str
    install: str
    updating: str
    pulling: str
    docker_detected: str
    close: str
    applied: str
    restarting_suffix: str
    error_contacting: str


class GUIMessages(TypedDict):
    output: str
    status: GUIStatus
    tabs: GUITabs
    login: GUILoginForm
    websocket: GUIWebsocket
    progress: GUIProgress
    channels: GUIChannels
    inventory: GUIInventory
    settings: GUISettings
    help: GUIHelp
    header: GUIHeader
    footer: GUIFooter
    badges: GUIBadges
    wanted: GUIWanted
    system: GUISystem
    analytics: GUIAnalytics
    drops_today: GUIDropsToday
    campaign_modal: GUICampaignModal
    accounts_manager: GUIAccountsManager
    update_modal: GUIUpdateModal


class Translation(TypedDict):
    language_name: str
    english_name: str
    status: StatusMessages
    login: LoginMessages
    error: ErrorMessages
    gui: GUIMessages


class Translator:
    def __init__(self) -> None:
        self.logger: logging.Logger = logging.getLogger("TwitchDropsMiner.i18n.Translator")
        self._langs: dict[str, Translation] = {}
        self.current_language: str
        self.t: Translation
        # load available languages from JSON files by reading language_name field
        for filepath in LANG_PATH.glob("*.json"):
            with filepath.open("r", encoding="utf-8") as json_file:
                try:
                    loaded_translation: Translation = json.load(json_file)
                    self._langs[loaded_translation["language_name"]] = loaded_translation
                except Exception as e:
                    # if we can't read the file, skip it
                    self.logger.warning(f"Failed to load language file {filepath}: {e}")
                    continue
        self._langs = dict(sorted(self._langs.items()))
        self.set_language(DEFAULT_LANG)

    def get_languages(self) -> list[str]:
        return list(self._langs.keys())

    # Map of locale codes to language_name keys
    _LOCALE_MAP: dict[str, str] = {
        "en": "English", "de": "Deutsch", "es": "Español", "fr": "Français",
        "pl": "Polski", "tr": "Türkçe", "uk": "Українська", "ar": "العربية",
        "zh": "简体中文", "id": "Indonesian", "da": "Dansk", "cs": "Čeština",
    }

    def set_language(self, language: str):
        language = self._LOCALE_MAP.get(language, language)
        if language not in self._langs:
            raise ValueError(f"Unrecognized language {language}")

        self.current_language = language
        self.t = cast(Translation, self._langs.get(language))


_ = Translator()
