from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict, abc, deque
from datetime import datetime, timedelta, timezone
from functools import partial
from time import time
from typing import TYPE_CHECKING, Any, Final, Literal

import aiohttp

from src.api import GQLClient, HTTPClient
from src.auth import _AuthState
from src.config import (
    GQL_OPERATIONS,
    MAX_CHANNELS,
    ClientType,
    State,
    WebsocketTopic,
)
from src.exceptions import (
    ExitRequest,
    MinerException,
    RequestException,
)
from src.i18n import _
from src.models.campaign import DropsCampaign
from src.models.channel import Channel
from src.services.channel_service import ChannelService
from src.services.drop_history import save_drop_claim
from src.services.inventory_service import InventoryService
from src.services.irc_service import IRCService
from src.services.maintenance import MaintenanceService
from src.services.message_handlers import MessageHandlerService
from src.services.prediction_service import PredictionService
from src.services.scheduler_service import SchedulerService
from src.services.stream_selector import StreamSelector
from src.services.watch_service import WatchService
from src.utils import (
    AwaitableValue,
)
from src.websocket import WebsocketPool


if TYPE_CHECKING:
    from src.config import ClientInfo, GQLRequest, JsonType
    from src.config.settings import Settings
    from src.models.channel import Stream
    from src.models.drop import TimedDrop
    from src.models.game import Game
    from src.web.gui_manager import WebGUIManager


logger = logging.getLogger("TwitchDrops")
gql_logger = logging.getLogger("TwitchDrops.gql")


class Twitch:
    def __init__(self, settings: Settings):
        self.settings: Settings = settings
        # State management
        self._state: State = State.IDLE
        self._state_change = asyncio.Event()
        self.wanted_games: list[Game] = []
        self.inventory: list[DropsCampaign] = []
        self._drops: dict[str, TimedDrop] = {}
        self._campaigns: dict[str, DropsCampaign] = {}
        self._webhook_sent_drops: set[str] = set()
        self._mnt_triggers: deque[datetime] = deque()
        # Client type and auth
        self._client_type: ClientInfo = ClientType.ANDROID_APP
        self._auth_state: _AuthState = _AuthState(self)
        # GUI (will be set by main.py)
        self.gui: WebGUIManager = None  # type: ignore[assignment]
        # API clients (will be initialized after GUI is set)
        self._http_client: HTTPClient | None = None
        self._gql_client: GQLClient | None = None
        # Storing and watching channels
        self.channels: OrderedDict[int, Channel] = OrderedDict()
        self.watching_channel: AwaitableValue[Channel] = AwaitableValue()
        self._idle_topic_ids: list[str] = []
        self._idle_channels_set: set = set()  # all channels currently watched in idle mode
        self._watching_cp_topic_id: str = ""  # CommunityPoints topic for current watching channel
        self._watching_task: asyncio.Task[None] | None = None
        self._watching_restart = asyncio.Event()
        # Manual mode tracking
        self._manual_target_channel: Channel | None = None
        self._manual_target_game: Game | None = None
        # Websocket
        self.websocket = WebsocketPool(self)
        # Maintenance task
        self._mnt_task: asyncio.Task[None] | None = None
        # Services
        self._maintenance_service: MaintenanceService = MaintenanceService(self)
        self._channel_service: ChannelService = ChannelService(self)
        self._message_handler_service: MessageHandlerService = MessageHandlerService(self)
        self._inventory_service: InventoryService = InventoryService(self)
        self._watch_service: WatchService = WatchService(self)
        self._stream_selector: StreamSelector = StreamSelector()
        # Pause/resume state (PR #65)
        self._is_paused: bool = False
        self._pause_source: str | None = None
        self._user_override: bool = False
        self._scheduler_task: asyncio.Task[None] | None = None
        self._scheduler_service: SchedulerService = SchedulerService(self)
        from src.services.campaign_alert_service import CampaignAlertService
        self._campaign_alert_service: CampaignAlertService = CampaignAlertService(self)
        self._irc_service: IRCService = IRCService(self)
        self._prediction_service: PredictionService = PredictionService(self)
        # Skip game tracking (set grows until all games tried, then resets)
        self._skipped_games: set[Game] = set()

    def _ensure_api_clients(self) -> None:
        """Ensure API clients are initialized (called after GUI is set)."""
        if self._http_client is None:
            self._http_client = HTTPClient(self.settings, self.gui, self, self._client_type)
        if self._gql_client is None:
            self._gql_client = GQLClient(self._http_client, self._auth_state, self._client_type)

    async def get_session(self):
        """
        Get the HTTP session (for backward compatibility).

        Delegates to HTTPClient.
        """
        self._ensure_api_clients()
        assert self._http_client is not None
        return await self._http_client.get_session()

    def request(self, method: str, url: str | Any, **kwargs):
        """
        Make an HTTP request (for backward compatibility).

        Delegates to HTTPClient.
        """
        self._ensure_api_clients()
        assert self._http_client is not None
        return self._http_client.request(method, url, **kwargs)

    async def shutdown(self) -> None:
        start_time = time()
        self.stop_watching()
        if self._watching_task is not None:
            self._watching_task.cancel()
            self._watching_task = None
        if self._mnt_task is not None:
            self._mnt_task.cancel()
            self._mnt_task = None
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            self._scheduler_task = None
        # stop websocket and close HTTP session
        await self.websocket.stop(clear_topics=True)
        await self._irc_service.stop()
        if self._http_client is not None:
            await self._http_client.close()
        self._drops.clear()
        self.channels.clear()
        self.inventory.clear()
        self._auth_state.clear()
        self.wanted_games.clear()
        self._mnt_triggers.clear()
        # wait at least half a second + whatever it takes to complete the closing
        # this allows aiohttp to safely close the session
        await asyncio.sleep(start_time + 0.5 - time())

    def wait_until_login(self) -> abc.Coroutine[Any, Any, Literal[True]]:
        """Wait until the user is logged in."""
        return self._auth_state._logged_in.wait()

    def change_state(self, state: State) -> None:
        """Change the current state of the miner."""
        if self._state is not State.EXIT:
            # prevent state changing once we switch to exit state
            self._state = state
        self._state_change.set()

    def get_change_state_callable(self, state: State) -> abc.Callable[[], None]:
        """Return a callable that changes state when invoked (deferred call for GUI usage)."""
        return partial(self.change_state, state)

    def skip_current_game(self) -> None:
        """Skip the current game; accumulates skipped games until all tried, then resets."""
        watching_channel = self.watching_channel.get_with_default(None)
        if watching_channel is not None and watching_channel.game is not None:
            self._skipped_games.add(watching_channel.game)
        self.change_state(State.CHANNEL_SWITCH)

    def close(self) -> None:
        """
        Called when the application is requested to close by the user,
        usually by the console or application window being closed.
        """
        self.change_state(State.EXIT)

    def pause(self, source: str = "user") -> None:
        """Pause the miner."""
        if source == "user":
            self._user_override = True
        self._is_paused = True
        self._pause_source = source
        self.change_state(State.PAUSED)
        asyncio.create_task(self.gui.broadcast_pause_state(True))

    def resume(self, *, user_override: bool = False) -> None:
        """Resume the miner."""
        if user_override:
            self._user_override = True
            self._pause_source = None
        self._is_paused = False
        self._pause_source = None
        self.change_state(State.INVENTORY_FETCH)
        asyncio.create_task(self.gui.broadcast_pause_state(False))

    def is_paused(self) -> bool:
        """Return whether the miner is currently paused."""
        return self._is_paused

    def print(self, message: str) -> None:
        """Print a message in the GUI."""
        self.gui.print(message)

    def _remove_channel_topics(self, channels: abc.Iterable[Channel]) -> None:
        """Remove websocket topics for a list of channels."""
        topics_to_remove: list[str] = []
        for channel in channels:
            topics_to_remove.append(WebsocketTopic.as_str("Channel", "StreamState", channel.id))
            topics_to_remove.append(WebsocketTopic.as_str("Channel", "StreamUpdate", channel.id))
        if topics_to_remove:
            self.websocket.remove_topics(topics_to_remove)

    async def run(self) -> None:
        """Main entry point for the miner - handles exit requests."""
        while True:
            try:
                await self._run()
                break
            except ExitRequest:
                break
            except aiohttp.ContentTypeError as exc:
                raise RequestException(_.t["login"]["unexpected_content"]) from exc

    async def _run(self) -> None:
        """
        Main method that runs the whole client.

        Here, we manage several things, specifically:
        • Fetching the drops inventory to make sure that everything we can claim, is claimed
        • Selecting a stream to watch, and watching it
        • Changing the stream that's being watched if necessary
        """
        # Initialize API clients now that GUI is available
        self._ensure_api_clients()
        auth_state = await self.get_auth()
        await self.websocket.start()
        await self._irc_service.start()
        # NOTE: watch task is explicitly restarted on each new run
        if self._watching_task is not None:
            self._watching_task.cancel()
        self._watching_task = asyncio.create_task(self._watch_service.watch_loop())
        # Start scheduler service
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
        self._scheduler_task = asyncio.create_task(self._scheduler_service.run_scheduler())
        # Add default topics
        self.websocket.add_topics(
            [
                WebsocketTopic(
                    "User", "Drops", auth_state.user_id, self._message_handler_service.process_drops
                ),
                WebsocketTopic(
                    "User",
                    "Notifications",
                    auth_state.user_id,
                    self._message_handler_service.process_notifications,
                ),
            ]
        )
        if self.settings.idle_use_followed:
            followed = await self._fetch_followed_channels()
            existing = {ch.lower() for ch in self.settings.idle_channels}
            for login in followed:
                if login not in existing:
                    self.settings.idle_channels.append(login)
                    existing.add(login)
            logger.info(f"Follower import: added {len(followed)} channels to idle pool")
        full_cleanup: bool = False
        channels: Final[OrderedDict[int, Channel]] = self.channels
        self.change_state(State.INVENTORY_FETCH)
        while True:
            if self._state is State.PAUSED:
                self.gui.status.update("⏸ Mining paused")
                self.stop_watching()
                self._state_change.clear()
                await self._state_change.wait()
                continue
            elif self._state is State.IDLE:
                self.gui.status.update(_.t["gui"]["status"]["idle"])
                self.stop_watching()
                # Remove any previously added idle channel topics to prevent accumulation
                if self._idle_topic_ids:
                    self.websocket.remove_topics(self._idle_topic_ids)
                    self._idle_topic_ids = []
                # Try idle watch if channels are configured or followed auto-mode is on
                self._idle_channels_set = set()
                if self.settings.idle_channels or self.settings.idle_use_followed:
                    logger.info(f"Idle watch: trying channels {self.settings.idle_channels}")
                    idle_parallel = getattr(self.settings, "idle_parallel", True)
                    if idle_parallel:
                        idle_chs = await self._fetch_all_idle_channels()
                    else:
                        single = await self._fetch_idle_channel()
                        idle_chs = [single] if single else []
                    if idle_chs:
                        self._idle_channels_set = set(idle_chs)
                        names = ", ".join(ch.name for ch in idle_chs)
                        logger.info(f"Idle watch: watching {len(idle_chs)} channel(s): {names}")
                        self.gui.status.update(f"💤 Idle: {names}")
                        # Use first channel as primary for display/watch_loop
                        self.watch(idle_chs[0], update_status=False)
                        idle_topics: list[WebsocketTopic] = [
                            WebsocketTopic(
                                "Channel",
                                "StreamState",
                                ch.id,
                                self._message_handler_service.process_idle_stream_state,
                            )
                            for ch in idle_chs
                        ]
                        self._idle_topic_ids = [str(t) for t in idle_topics]
                        self.websocket.add_topics(idle_topics)
                        logger.info(f"Idle watch: subscribed StreamState for {names}")
                        # Subscribe CommunityPoints for all additional idle channels
                        if self.settings.claim_channel_points:
                            for ch in idle_chs[1:]:
                                cp_topic_id = WebsocketTopic.as_str("Channel", "CommunityPoints", ch.id)
                                if cp_topic_id not in self._idle_topic_ids:
                                    try:
                                        self.websocket.add_topics([WebsocketTopic(
                                            "Channel", "CommunityPoints", ch.id,
                                            self._message_handler_service.process_community_points,
                                        )])
                                        self._idle_topic_ids.append(cp_topic_id)
                                    except Exception:
                                        logger.warning(f"CP topic limit — skipping {ch.name}")
                        # Bug 3 fix: subscribe Predictions for ALL parallel idle channels
                        # (not just the primary — ch[0] already gets Predictions via watch()).
                        if self.settings.make_predictions:
                            pred_whitelist = [c.lower() for c in self.settings.prediction_channels]
                            for ch in idle_chs[1:]:
                                if pred_whitelist and ch._login.lower() not in pred_whitelist:
                                    continue
                                pred_topic_id = WebsocketTopic.as_str("Channel", "Predictions", ch.id)
                                if pred_topic_id not in self._idle_topic_ids:
                                    try:
                                        self.websocket.add_topics([WebsocketTopic(
                                            "Channel", "Predictions", ch.id,
                                            self._prediction_service.process_prediction,
                                        )])
                                        self._idle_topic_ids.append(pred_topic_id)
                                    except Exception:
                                        logger.warning(f"Predictions topic limit — skipping {ch.name}")
                    else:
                        logger.info("Idle watch: no idle channels online")
                # clear the flag and wait until it's set again
                self._state_change.clear()
            elif self._state is State.INVENTORY_FETCH:
                # ensure the websocket is running
                await self.websocket.start()
                await self.fetch_inventory()
                asyncio.create_task(
                    self._campaign_alert_service.check_and_alert(list(self.inventory))
                )
                self.gui.set_games({campaign.game for campaign in self.inventory})
                # Broadcast unwanted items (based on settings)
                self.gui.broadcast_wanted_items()
                # Save state on every inventory fetch
                self.change_state(State.GAMES_UPDATE)
            elif self._state is State.GAMES_UPDATE:
                # claim drops from expired and active campaigns
                logger.info("Checking for claimable drops")
                logger.debug("Campaigns in inventory: %s", self.inventory)
                for campaign in self.inventory:
                    if not campaign.upcoming:
                        for drop in campaign.drops:
                            # If drop is fully complete but has no claim_id (WebSocket missed),
                            # generate a synthetic claim_id so can_claim returns True
                            if (
                                not drop.is_claimed
                                and drop.claim_id is None
                                and hasattr(drop, "progress")
                                and drop.progress >= 1.0
                            ):
                                logger.info(f"Generating synthetic claim_id for completed drop: {drop.id}")
                                await drop.generate_claim()
                            if drop.can_claim:
                                claimed = await drop.claim()
                                if claimed:
                                    webhook_url = self.settings.discord_webhook_drops
                                    if webhook_url and drop.id not in self._webhook_sent_drops:
                                        self._webhook_sent_drops.add(drop.id)
                                        import json as _json_wh
                                        from src.config import DATA_DIR as _DATA_DIR_WH
                                        _wcfg_wh = _DATA_DIR_WH / "web_config.json"
                                        try:
                                            _acct_wh = _json_wh.loads(_wcfg_wh.read_text()).get("active_account", "") if _wcfg_wh.exists() else ""
                                        except Exception:
                                            _acct_wh = ""
                                        embed: dict = {
                                            "title": "🎁 Drop Claimed!",
                                            "color": 0x9147ff,
                                            "fields": [
                                                {"name": "Game", "value": campaign.game.name, "inline": True},
                                                {"name": "Drop", "value": drop.name, "inline": True},
                                                {"name": "Reward", "value": drop.rewards_text(), "inline": False},
                                            ],
                                        }
                                        if _acct_wh:
                                            embed["footer"] = {"text": f"Account: {_acct_wh}"}
                                        if drop.benefits:
                                            embed["thumbnail"] = {"url": drop.benefits[0].image_url}
                                        asyncio.create_task(
                                            self._message_handler_service._send_discord_webhook(
                                                webhook_url, {"embeds": [embed]}
                                            )
                                        )
                                    # Save to drop history
                                    save_drop_claim(
                                        campaign.game.name,
                                        drop.name,
                                        drop.rewards_text(),
                                        drop.benefits[0].image_url if drop.benefits else None,
                                    )
                # figure out which games we want based on games_to_watch whitelist
                self.wanted_games.clear()
                games_to_watch: list[str] = self.settings.games_to_watch
                next_hour: datetime = datetime.now(timezone.utc) + timedelta(hours=1)
                logger.info("games_to_watch: %s", games_to_watch)
                logger.info(
                    "inventory has %d eligible campaigns",
                    sum(1 for c in self.inventory if c.eligible),
                )
                logger.debug("inventories: %s", self.inventory)

                # Log detailed game -> campaigns -> channels mapping
                if logger.isEnabledFor(logging.DEBUG):
                    self._output_campaign_mapping(next_hour)

                logger.info("Building wanted games list")
                # Build wanted_games list preserving the order from games_to_watch
                self.wanted_games = self._stream_selector.get_wanted_games(
                    self.settings, self.inventory
                )
                logger.info("Wanted games list built")

                if self.wanted_games:
                    logger.info(
                        "Wanted games: %s", ", ".join(game.name for game in self.wanted_games)
                    )
                else:
                    logger.warning(
                        "No wanted games found! games_to_watch=%s, eligible_campaigns=%d",
                        games_to_watch,
                        sum(
                            1 for c in self.inventory if c.eligible and c.can_earn_within(next_hour)
                        ),
                    )

                # Handle manual mode: check if manual game still has drops
                if self.is_manual_mode():
                    manual_has_drops = any(
                        campaign.can_earn_within(next_hour)
                        and campaign.game == self._manual_target_game
                        for campaign in self.inventory
                    )
                    if not manual_has_drops:
                        self.exit_manual_mode("All drops completed for manual game")
                    elif self._manual_target_game in self.wanted_games:
                        # Move manual game to front of wanted_games for priority
                        self.wanted_games.remove(self._manual_target_game)
                        self.wanted_games.insert(0, self._manual_target_game)
                        logger.info(
                            f"Manual mode: prioritizing game {self._manual_target_game.name}"
                        )

                full_cleanup = True
                self.restart_watching()
                self.change_state(State.CHANNELS_CLEANUP)
            elif self._state is State.CHANNELS_CLEANUP:
                self.gui.status.update(_.t["gui"]["status"]["cleanup"])
                if not self.wanted_games or full_cleanup:
                    # no games selected or we're doing full cleanup: remove everything
                    to_remove_channels: list[Channel] = list(channels.values())
                else:
                    # remove all channels that:
                    to_remove_channels = [
                        channel
                        for channel in channels.values()
                        if (
                            not channel.acl_based  # aren't ACL-based
                            and (
                                channel.offline  # and are offline
                                # or online but aren't streaming the game we want anymore
                                or (channel.game is None or channel.game not in self.wanted_games)
                            )
                        )
                    ]
                full_cleanup = False
                if to_remove_channels:
                    self._remove_channel_topics(to_remove_channels)
                    for channel in to_remove_channels:
                        del channels[channel.id]
                        # Don't remove from GUI - batch_update in CHANNELS_FETCH will handle it atomically
                    del to_remove_channels
                if self.wanted_games:
                    self.change_state(State.CHANNELS_FETCH)
                else:
                    # with no games available, we switch to IDLE after cleanup
                    self.print(_.t["status"]["no_campaign"])
                    self.change_state(State.IDLE)
            elif self._state is State.CHANNELS_FETCH:
                self.gui.status.update(_.t["gui"]["status"]["gathering"])
                # start with all current channels, keep them in memory for smooth update
                new_channels: set[Channel] = set(channels.values())
                channels.clear()
                # gather and add ACL channels from campaigns
                # NOTE: we consider only campaigns that can be progressed
                # NOTE: we use another set so that we can set them online separately
                no_acl: set[Game] = set()
                acl_channels: set[Channel] = set()
                next_hour = datetime.now(timezone.utc) + timedelta(hours=1)
                for campaign in self.inventory:
                    if campaign.game in self.wanted_games and campaign.can_earn_within(next_hour):
                        if campaign.allowed_channels:
                            acl_channels.update(campaign.allowed_channels)
                        else:
                            no_acl.add(campaign.game)
                # remove all ACL channels that already exist from the other set
                acl_channels.difference_update(new_channels)
                # use the other set to set them online if possible
                await self.bulk_check_online(acl_channels)
                # finally, add them as new channels
                new_channels.update(acl_channels)
                for game in no_acl:
                    # for every campaign without an ACL, for it's game,
                    # add a list of live channels with drops enabled
                    try:
                        new_channels.update(await self.get_live_streams(game, drops_enabled=True))
                    except Exception as exc:
                        logger.warning(f"Failed to fetch channels for {game.name}: {exc} — skipping")
                # sort them descending by viewers, by priority and by game priority
                # NOTE: Viewers sort also ensures ONLINE channels are sorted to the top
                # NOTE: We can drop using the set now, because there's no more channels being added
                ordered_channels: list[Channel] = sorted(
                    new_channels, key=ChannelService.get_viewers_key, reverse=True
                )
                ordered_channels.sort(key=lambda ch: ch.acl_based, reverse=True)
                ordered_channels.sort(key=self._channel_service.get_priority)
                # ensure that we won't end up with more channels than we can handle
                # NOTE: we trim from the end because that's where the non-priority,
                # offline (or online but low viewers) channels end up
                to_remove_channels = ordered_channels[MAX_CHANNELS:]
                ordered_channels = ordered_channels[:MAX_CHANNELS]
                if to_remove_channels:
                    # tracked channels and gui were cleared earlier, so no need to do it here
                    # just make sure to unsubscribe from their topics
                    self._remove_channel_topics(to_remove_channels)
                    del to_remove_channels
                # set our new channel list and update GUI in one batch
                for channel in ordered_channels:
                    channels[channel.id] = channel
                # Batch update GUI - prevents flickering from individual adds
                self.gui.channels.batch_update(ordered_channels)
                # subscribe to these channel's state updates
                to_add_topics: list[WebsocketTopic] = []
                for channel_id in channels:
                    to_add_topics.append(
                        WebsocketTopic(
                            "Channel",
                            "StreamState",
                            channel_id,
                            self._message_handler_service.process_stream_state,
                        )
                    )
                    to_add_topics.append(
                        WebsocketTopic(
                            "Channel",
                            "StreamUpdate",
                            channel_id,
                            self._message_handler_service.process_stream_update,
                        )
                    )
                try:
                    self.websocket.add_topics(to_add_topics)
                except MinerException:
                    logger.warning("Topic limit reached — too many channels, some won't be tracked")
                # relink watching channel after cleanup
                # NOTE: this replaces 'self.watching_channel's internal value with the new object
                # Don't call stop_watching() here - let CHANNEL_SWITCH handle it to avoid clearing drop display
                watching_channel = self.watching_channel.get_with_default(None)
                if watching_channel is not None:
                    new_watching: Channel | None = channels.get(watching_channel.id)
                    if new_watching is not None and self.can_watch(new_watching):
                        self.watch(new_watching, update_status=False)
                    # If channel not found, CHANNEL_SWITCH will handle selecting a new one
                    del new_watching
                # pre-display the active drop with a substracted minute
                for channel in channels.values():
                    # check if there's any channels we can watch first
                    if self.can_watch(channel):
                        if (active_campaign := self.get_active_campaign(channel)) is not None and (
                            active_drop := active_campaign.first_drop
                        ) is not None:
                            active_drop.display(countdown=False, subone=True)
                        break
                self.change_state(State.CHANNEL_SWITCH)
                del (
                    no_acl,
                    acl_channels,
                    new_channels,
                    to_add_topics,
                    ordered_channels,
                    watching_channel,
                )
            elif self._state is State.CHANNEL_SWITCH:
                self.gui.status.update(_.t["gui"]["status"]["switching"])

                # Determine the best channel to watch
                new_watching: Channel | None = None  # type: ignore[no-redef]
                selected_channel: Channel | None = self.gui.channels.get_selection()
                watching_channel: Channel | None = self.watching_channel.get_with_default(None)  # type: ignore[no-redef]

                # Handle user selection
                if selected_channel is not None and self.can_watch(selected_channel):
                    # Check if this is a game change -> enter manual mode
                    if watching_channel and selected_channel.game != watching_channel.game:
                        self.enter_manual_mode(selected_channel)
                    new_watching = selected_channel
                # Handle manual mode
                elif self.is_manual_mode():
                    # Try to stay on manual target channel
                    if self._manual_target_channel and self.can_watch(self._manual_target_channel):
                        new_watching = self._manual_target_channel
                    else:
                        # Manual channel offline, find another channel for same game
                        for channel in channels.values():
                            if channel.game == self._manual_target_game and self.can_watch(channel):
                                new_watching = channel
                                self._manual_target_channel = channel
                                game_name = (
                                    self._manual_target_game.name
                                    if self._manual_target_game
                                    else "Unknown"
                                )
                                logger.info(
                                    f"Manual mode: switching to {channel.name} (same game: {game_name})"
                                )
                                break
                        # No channels available for manual game -> exit manual mode
                        if new_watching is None:
                            self.exit_manual_mode("No channels available for manual game")
                # Auto-select best channel based on priority
                else:
                    skipped = self._skipped_games

                    def _streak_key(ch: Channel) -> int:
                        return 0 if self._stream_selector._has_unclaimed_streak_today(ch.name) else 1

                    if skipped:
                        # Skip mode: find channel from a game not yet skipped
                        watchable = sorted(
                            channels.values(), key=self._channel_service.get_priority
                        )
                        watchable = sorted(watchable, key=_streak_key)
                        for channel in watchable:
                            if self.can_watch(channel) and channel.game not in skipped:
                                new_watching = channel
                                break
                        # All games skipped → reset and pick best available
                        if new_watching is None:
                            self._skipped_games.clear()
                            watchable = sorted(
                                channels.values(), key=self._channel_service.get_priority
                            )
                            watchable = sorted(watchable, key=_streak_key)
                            for channel in watchable:
                                if self.can_watch(channel):
                                    new_watching = channel
                                    break
                    else:
                        watchable = sorted(
                            channels.values(), key=self._channel_service.get_priority
                        )
                        watchable = sorted(watchable, key=_streak_key)
                        for channel in watchable:
                            if self.can_watch(channel) and self.should_switch(channel):
                                new_watching = channel
                                break

                if new_watching is not None:
                    # Switch to new channel
                    self.watch(new_watching)
                    asyncio.create_task(self._irc_service.join(new_watching.name))
                    # Display the active drop for the new channel
                    if (active_campaign := self.get_active_campaign(new_watching)) is not None and (
                        active_drop := active_campaign.first_drop
                    ) is not None:
                        active_drop.display(countdown=False, subone=True)
                    self._state_change.clear()
                elif watching_channel is not None and self.can_watch(watching_channel):
                    # Continue watching current channel
                    if self.is_manual_mode() and self._manual_target_game:
                        status_text = f"🎯 Manual Mode: Watching {watching_channel.name} for {self._manual_target_game.name}"
                    else:
                        status_text = _.t["status"]["watching"].format(
                            channel=watching_channel.name
                        )
                    self.gui.status.update(status_text)
                    self._state_change.clear()
                else:
                    # No channels available to watch
                    self.print(_.t["status"]["no_channel"])
                    self.change_state(State.IDLE)
            elif self._state is State.EXIT:
                self.gui.status.update(_.t["gui"]["status"]["exiting"])
                # we've been requested to exit the application
                break
            if self._state is State.IDLE and not self._idle_topic_ids:
                try:
                    await asyncio.wait_for(self._state_change.wait(), timeout=3600)
                except asyncio.TimeoutError:
                    logger.info("Idle for 1 hour — refreshing campaigns")
                    self.change_state(State.INVENTORY_FETCH)
            else:
                await self._state_change.wait()

    async def _fetch_followed_channels(self) -> list[str]:
        try:
            resp = await self.gql_request(GQL_OPERATIONS["FollowedChannels"])
            edges = (
                resp.get("data", {})
                .get("currentUser", {})
                .get("follows", {})
                .get("edges", [])
            )
            return [e["node"]["login"].lower() for e in edges if e.get("node", {}).get("login")]
        except Exception as e:
            logger.warning(f"Failed to fetch followed channels: {e}")
            return []

    async def _fetch_followed_live_logins(self) -> list[str]:
        """Fetch logins of live followed channels via Helix; falls back to GQL on 401."""
        try:
            auth = await self.get_auth()
            user_id = auth.user_id
            client_id = self._client_type.CLIENT_ID
            access_token = auth.access_token
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Client-Id": client_id,
            }
            logins: list[str] = []
            cursor: str | None = None
            helix_ok = True
            for _ in range(5):
                url = f"https://api.twitch.tv/helix/streams/followed?user_id={user_id}&first=100"
                if cursor:
                    url += f"&after={cursor}"
                async with self._http_client.request("GET", url, headers=headers) as resp:
                    if resp.status == 401:
                        helix_ok = False
                        break
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(f"Idle (followed): Helix returned {resp.status}: {body[:200]}")
                        break
                    data = await resp.json()
                logins.extend(s["user_login"] for s in data.get("data", []))
                cursor = data.get("pagination", {}).get("cursor")
                if not cursor:
                    break
            if helix_ok:
                logger.info(f"Idle (followed): {len(logins)} live followed channels")
                return logins
            # Helix 401 — token lacks user:read:follows scope, fall back to GQL
            logger.info("Idle (followed): Helix 401, falling back to GQL followed list")
            return await self._fetch_followed_channels()
        except Exception as exc:
            logger.warning(f"Could not fetch followed live channels: {exc}")
            return []

    async def _fetch_idle_channel_by_login(self, login: str) -> Channel | None:
        """Fetch a specific channel by login and return it if online."""
        from src.models.channel import Stream
        try:
            response = await self.gql_request(
                GQL_OPERATIONS["GetStreamInfo"].with_variables({"channel": login})
            )
            user_data = response["data"]["user"]
            if not user_data or not user_data.get("stream"):
                return None
            channel = Channel(self, id=user_data["id"], login=login, display_name=user_data.get("displayName"))
            channel._stream = Stream.from_get_stream(channel, user_data)
            return channel
        except Exception as exc:
            logger.debug(f"Idle channel fetch failed for {login}: {exc}")
            return None

    async def _fetch_idle_channel(self) -> Channel | None:
        """Return the first online idle channel (manual list + optionally followed)."""
        logins: list[str] = list(self.settings.idle_channels)
        if self.settings.idle_use_followed:
            followed = await self._fetch_followed_live_logins()
            seen = set(logins)
            for login in followed:
                if login not in seen:
                    logins.append(login)
                    seen.add(login)
        for login in logins:
            channel = await self._fetch_idle_channel_by_login(login)
            if channel is not None:
                return channel
        return None

    async def _fetch_all_idle_channels(self) -> list:
        """Return ALL online idle channels (for parallel idle watching)."""
        logins: list[str] = list(self.settings.idle_channels)
        if self.settings.idle_use_followed:
            followed = await self._fetch_followed_live_logins()
            seen = set(logins)
            for login in followed:
                if login not in seen:
                    logins.append(login)
                    seen.add(login)
        results = []
        for login in logins:
            channel = await self._fetch_idle_channel_by_login(login)
            if channel is not None:
                results.append(channel)
        return results

    def can_watch(self, channel: Channel) -> bool:
        """Delegate to WatchService."""
        return self._watch_service.can_watch(channel)

    def should_switch(self, channel: Channel) -> bool:
        """Delegate to WatchService."""
        return self._watch_service.should_switch(channel)

    def watch(self, channel: Channel, *, update_status: bool = True) -> None:
        """Delegate to WatchService."""
        self._watch_service.watch(channel, update_status=update_status)

    def stop_watching(self) -> None:
        """Delegate to WatchService."""
        _ch = self.watching_channel.get_with_default(None)
        if _ch is not None:
            asyncio.create_task(self._irc_service.part(_ch.name))
        self._watch_service.stop_watching()

    def restart_watching(self) -> None:
        """Delegate to WatchService."""
        self._watch_service.restart_watching()

    def is_manual_mode(self) -> bool:
        """Check if manual mode is currently active."""
        return self._manual_target_channel is not None and self._manual_target_game is not None

    def enter_manual_mode(self, channel: Channel) -> None:
        """
        Enter manual mode for the given channel's game.

        Args:
            channel: The channel that was manually selected by the user
        """
        if channel.game is None:
            logger.warning(f"Cannot enter manual mode: channel {channel.name} has no game")
            return

        self._manual_target_channel = channel
        self._manual_target_game = channel.game
        self._skipped_games.clear()
        logger.info(f"Entered manual mode for game: {channel.game.name}, channel: {channel.name}")

        # Broadcast manual mode change to GUI
        self.gui.broadcast_manual_mode_change(self.get_manual_mode_info())

    def exit_manual_mode(self, reason: str = "") -> None:
        """
        Exit manual mode and return to automatic channel selection.

        Args:
            reason: Optional reason for exiting manual mode (for logging)
        """
        if not self.is_manual_mode():
            return

        game_name = self._manual_target_game.name if self._manual_target_game else "Unknown"
        logger.info(
            f"Exiting manual mode for game: {game_name}. Reason: {reason or 'User requested'}"
        )

        self._manual_target_channel = None
        self._manual_target_game = None

        # Broadcast manual mode change to GUI
        self.gui.broadcast_manual_mode_change(self.get_manual_mode_info())

        # Trigger channel switch to select new channel automatically
        self.change_state(State.CHANNEL_SWITCH)

    def get_manual_mode_info(self) -> dict[str, Any]:
        """
        Get current manual mode status information.

        Returns:
            Dictionary with manual mode status including active state and game name
        """
        if self.is_manual_mode():
            return {
                "active": True,
                "game_name": self._manual_target_game.name if self._manual_target_game else "",
                "channel_name": self._manual_target_channel.name
                if self._manual_target_channel
                else "",
            }
        return {"active": False}

    def on_channel_update(
        self, channel: Channel, stream_before: Stream | None, stream_after: Stream | None
    ) -> None:
        """Delegate to MessageHandlerService."""
        self._message_handler_service.on_channel_update(channel, stream_before, stream_after)

    async def get_auth(self) -> _AuthState:
        """Get authentication state (validates token if needed)."""
        await self._auth_state.validate()
        return self._auth_state

    async def gql_request(self, ops: GQLRequest | list[GQLRequest]) -> JsonType | list[JsonType]:
        """
        Execute GraphQL request(s).

        Delegates to GQLClient for execution.
        """
        self._ensure_api_clients()
        assert self._gql_client is not None
        return await self._gql_client.request(ops)

    async def fetch_campaigns(
        self, campaigns_chunk: list[tuple[str, JsonType]]
    ) -> dict[str, JsonType]:
        """Delegate to InventoryService."""
        return await self._inventory_service.fetch_campaigns(campaigns_chunk)

    async def fetch_inventory(self) -> None:
        """Delegate to InventoryService."""
        await self._inventory_service.fetch_inventory()

    def get_active_campaign(self, channel: Channel | None = None) -> DropsCampaign | None:
        """Delegate to InventoryService."""
        return self._inventory_service.get_active_campaign(channel)

    async def get_live_streams(
        self, game: Game, *, limit: int = 20, drops_enabled: bool = True
    ) -> list[Channel]:
        """Delegate to ChannelService."""
        return await self._channel_service.get_live_streams(
            game, limit=limit, drops_enabled=drops_enabled
        )

    async def bulk_check_online(self, channels: abc.Iterable[Channel]):
        """Delegate to ChannelService."""
        await self._channel_service.bulk_check_online(channels)

    def _filter_wanted_campaigns(self, next_hour: datetime) -> list[Game]:
        """
        Filter campaigns to find wanted games based on settings and benefits.
        """
        wanted_games: list[Game] = []
        games_to_watch: list[str] = self.settings.games_to_watch
        mining_benefits: dict[str, bool] = self.settings.mining_benefits

        for game_name in games_to_watch:
            game_name_lower: str = game_name.lower()
            for campaign in self.inventory:
                game: Game = campaign.game
                if (
                    game.name.lower() == game_name_lower
                    and game not in wanted_games
                    and campaign.can_earn_within(next_hour)
                    and campaign.has_wanted_unclaimed_benefits(mining_benefits)
                ):
                    wanted_games.append(game)
                    break
        return wanted_games

    def _output_campaign_mapping(self, next_hour: datetime) -> None:
        logger.info("=== Active Campaigns Mapping ===")
        from collections import defaultdict

        game_campaign_map: dict[str, list[tuple[DropsCampaign, list[str]]]] = defaultdict(list)
        for campaign in self.inventory:
            if campaign.eligible and not campaign.finished:
                logger.info("eligible Campaign: %s - %s", campaign.name, campaign.game.name)
            if campaign.can_earn_within(next_hour):
                channel_names = []
                if campaign.allowed_channels:
                    channel_names = [ch.name for ch in campaign.allowed_channels]
                else:
                    channel_names = ["<directory>"]
                game_campaign_map[campaign.game.name].append((campaign, channel_names))
        for game_name in sorted(game_campaign_map.keys()):
            logger.debug(f"Game: {game_name}")
            for campaign, channel_list in game_campaign_map[game_name]:
                status_info = f"{'ACTIVE' if campaign.active else 'UPCOMING'}"
                ends_info = campaign.ends_at.astimezone().strftime("%Y-%m-%d %H:%M")
                channel_info = (
                    f"{len(channel_list)} channels"
                    if channel_list[0] != "<directory>"
                    else "directory"
                )
                logger.debug(f"  └─ Campaign: {campaign.name} [{status_info}] (ends: {ends_info})")
                logger.debug(f"     Channels: {channel_info}")
                if channel_list[0] != "<directory>" and len(channel_list) <= 10:
                    logger.debug(f"     └─ {', '.join(channel_list)}")
                elif channel_list[0] != "<directory>":
                    logger.debug(
                        f"     └─ {', '.join(channel_list[:10])} ... (+{len(channel_list) - 10} more)"
                    )
        logger.info("=== End Campaigns Mapping ===")
