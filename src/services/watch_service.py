"""
Watch service for managing channel watching and drop progress monitoring.

This service handles the core watching loop that sends watch payloads to Twitch,
monitors drop progress, and determines when to switch channels.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from time import time
from typing import TYPE_CHECKING, NoReturn

from src.config import CALL, GQL_OPERATIONS, WATCH_INTERVAL, WebsocketTopic
from src.exceptions import GQLException, MinerException
from src.i18n import _
from src.utils import task_wrapper


if TYPE_CHECKING:
    from src.config import JsonType
    from src.core.client import Twitch
    from src.models import TimedDrop
    from src.models.channel import Channel


logger = logging.getLogger("TwitchDrops")


async def _send_idle_watch(channel) -> None:
    if not channel.online:
        return
    succeeded = await channel.send_watch()
    if succeeded:
        broadcast_id = channel._stream.broadcast_id if channel._stream else "none"
        logger.info(f"Watch sent OK (idle): {channel.name} (broadcast_id={broadcast_id})")
        await channel._send_watch_spade()
    else:
        logger.log(CALL, f"Watch requested failed for idle channel: {channel.name}")


class WatchService:
    """
    Service responsible for watching channels and monitoring drop progress.

    Handles:
    - Starting/stopping channel watching
    - Watch loop that sends periodic watch payloads
    - Drop progress monitoring via GQL and websocket
    - Channel switch eligibility checks
    - Watch loop sleep with restart capability
    """

    def __init__(self, twitch: Twitch) -> None:
        """
        Initialize the watch service.

        Args:
            twitch: The Twitch client instance
        """
        self._twitch = twitch

    def can_watch(self, channel: Channel) -> bool:
        """
        Determines if the given channel qualifies as a watching candidate.

        A channel can be watched if:
        - There are wanted games configured
        - The channel is online
        - Drops are enabled on the channel
        - The channel is streaming a wanted game
        - At least one campaign can be progressed on this channel

        Args:
            channel: The channel to evaluate

        Returns:
            True if the channel can be watched, False otherwise
        """
        if not self._twitch.wanted_games:
            return False

        # exit early if stream is offline or drops aren't enabled
        if not channel.online or not channel.drops_enabled:
            return False

        # check if we can progress any campaign for the played game
        if channel.game is None or channel.game not in self._twitch.wanted_games:
            return False

        return any(campaign.can_earn(channel) for campaign in self._twitch.inventory)

    def should_switch(self, channel: Channel) -> bool:
        """
        Determines if the given channel qualifies as a switch candidate.

        A channel should be switched to if:
        - We're not currently watching anything
        - The channel's game has higher priority than the watching channel's game
        - The channel has the same game priority but is ACL-based and watching isn't

        Args:
            channel: The channel to evaluate as a switch candidate

        Returns:
            True if we should switch to this channel, False otherwise
        """
        watching_channel = self._twitch.watching_channel.get_with_default(None)
        if watching_channel is None:
            return True

        channel_order = self._twitch._channel_service.get_priority(channel)
        watching_order = self._twitch._channel_service.get_priority(watching_channel)

        return (
            # this channel's game is higher order than the watching one's
            channel_order < watching_order
            or channel_order == watching_order  # or the order is the same
            # and this channel is ACL-based and the watching channel isn't
            and channel.acl_based > watching_channel.acl_based
        )

    def watch(self, channel: Channel, *, update_status: bool = True) -> None:
        """
        Start watching a specific channel.

        Updates GUI elements and sets the watching channel. Optionally prints
        a status message and updates the status bar.

        Args:
            channel: The channel to start watching
            update_status: Whether to print status message and update status bar
        """
        self._twitch.gui.channels.set_watching(channel)
        self._twitch.watching_channel.set(channel)
        # Subscribe CommunityPoints for this channel (only if enabled, 1 topic = safe)
        if self._twitch.settings.claim_channel_points:
            prev = self._twitch._watching_cp_topic_id
            new_topic_id = WebsocketTopic.as_str("Channel", "CommunityPoints", channel.id)
            if prev != new_topic_id:
                if prev:
                    self._twitch.websocket.remove_topics([prev])
                try:
                    self._twitch.websocket.add_topics([
                        WebsocketTopic(
                            "Channel",
                            "CommunityPoints",
                            channel.id,
                            self._twitch._message_handler_service.process_community_points,
                        )
                    ])
                    self._twitch._watching_cp_topic_id = new_topic_id
                except MinerException:
                    logger.warning(f"Topic limit reached — CommunityPoints not subscribed for {channel.name}")
        if self._twitch.settings.claim_moments:
            try:
                self._twitch.websocket.add_topics([WebsocketTopic(
                    "Channel", "Moments", channel.id,
                    self._twitch._message_handler_service.process_moments,
                )])
            except MinerException:
                logger.warning(f"Topic limit — Moments topic skipped for {channel.name}")
        if self._twitch.settings.make_predictions:
            whitelist = [c.lower() for c in self._twitch.settings.prediction_channels]
            if not whitelist or channel.name.lower() in whitelist:
                try:
                    self._twitch.websocket.add_topics([WebsocketTopic(
                        "Channel", "Predictions", channel.id,
                        self._twitch._prediction_service.process_prediction,
                    )])
                    logger.info(f"Predictions subscribed for {channel.name}")
                except MinerException:
                    logger.warning(f"Topic limit — Predictions topic skipped for {channel.name}")

        if update_status:
            # Check if manual mode is active for custom status message
            if self._twitch.is_manual_mode() and self._twitch._manual_target_game:
                status_text = f"🎯 Manual Mode: Watching {channel.name} for {self._twitch._manual_target_game.name}"
            else:
                status_text = _.t["status"]["watching"].format(channel=channel.name)
            self._twitch.print(status_text)
            self._twitch.gui.status.update(status_text)

    def subscribe_predictions_now(self) -> None:
        idle_parallel = getattr(self._twitch.settings, "idle_parallel", True)
        idle_set = self._twitch._idle_channels_set
        if idle_parallel and idle_set:
            channels = [ch for ch in idle_set if ch.online]
        else:
            ch = self._twitch.watching_channel.get_with_default(None)
            channels = [ch] if ch is not None else []

        whitelist = [c.lower() for c in self._twitch.settings.prediction_channels]
        for ch in channels:
            if whitelist and ch.name.lower() not in whitelist:
                continue
            try:
                self._twitch.websocket.add_topics([WebsocketTopic(
                    "Channel", "Predictions", ch.id,
                    self._twitch._prediction_service.process_prediction,
                )])
                logger.info(f"Predictions subscribed live for {ch.name}")
            except MinerException:
                logger.warning(f"Topic limit — Predictions skipped for {ch.name}")
                break

    def stop_watching(self) -> None:
        """
        Stop watching the current channel.

        Clears the watching channel and updates GUI elements.
        """
        self._twitch.gui.clear_drop()
        self._twitch.watching_channel.clear()
        self._twitch.gui.channels.clear_watching()

    def restart_watching(self) -> None:
        """
        Restart the watch loop (forces immediate re-send of watch payload).

        Stops the progress timer and signals the watch loop to restart.
        """
        self._twitch.gui.progress.stop_timer()
        self._twitch._watching_restart.set()

    async def watch_sleep(self, delay: float) -> None:
        """
        Sleep for a delay that can be interrupted by restart_watching().

        Uses wait_for with a timeout to allow an asyncio.sleep-like behavior
        that can be ended prematurely via the watching restart event.

        Args:
            delay: Time in seconds to sleep
        """
        self._twitch._watching_restart.clear()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._twitch._watching_restart.wait(), timeout=delay)

    @task_wrapper(critical=True)
    async def watch_loop(self) -> NoReturn:
        """
        Main watch loop that sends watch payloads and monitors drop progress.

        This loop:
        1. Waits for a channel to watch
        2. Sends watch payload to the channel
        3. Waits ~20 seconds for websocket progress update
        4. If no update received, queries drop progress via GQL or estimates it
        5. Sleeps until next watch interval (~20 seconds)
        6. Repeats

        The loop handles cases where Twitch temporarily stops reporting progress
        by falling back to GQL queries or minute bumping.
        """
        interval: float = WATCH_INTERVAL.total_seconds()
        _cp_poll_counter: int = 0
        _CP_POLL_EVERY: int = 3  # poll every ~3 iterations ≈ 60s

        while True:
            channel: Channel = await self._twitch.watching_channel.get()

            if not channel.online:
                # if the channel isn't online anymore, we stop watching it
                self.stop_watching()
                continue

            # logger.log(CALL, f"Sending watch payload to: {channel.name}")
            succeeded: bool = await channel.send_watch()
            last_sent: float = time()

            if not succeeded:
                logger.log(CALL, f"Watch requested failed for channel: {channel.name}")
            else:
                logger.info(f"Watch sent OK: {channel.name} (broadcast_id={channel._stream.broadcast_id if channel._stream else 'none'})")
                # Also send via Spade HTTP POST to credit channel points (GQL only credits drops)
                asyncio.create_task(channel._send_watch_spade())

            # Multi-channel idle: also send watch to all other idle channels
            # Only do this when parallel idle is explicitly enabled — avoids spurious
            # "Watch sent OK" logs for a second channel when parallel is OFF (Bug 1).
            idle_parallel = getattr(self._twitch.settings, "idle_parallel", True)
            idle_set = self._twitch._idle_channels_set
            if idle_parallel and idle_set:
                for idle_ch in list(idle_set):
                    if idle_ch is channel or not idle_ch.online:
                        continue
                    asyncio.create_task(_send_idle_watch(idle_ch))

            # wait ~20 seconds for a progress update
            await asyncio.sleep(20)

            if self._twitch.gui.progress.minute_almost_done():
                # If the previous update was more than ~60s ago, and the progress tracker
                # isn't counting down anymore, that means Twitch has temporarily
                # stopped reporting drop's progress. To ensure the timer keeps at least somewhat
                # accurate time, we can use GQL to query for the current drop,
                # or even "pretend" mining as a last resort option.
                handled: bool = False

                # Solution 1: use GQL to query for the currently mined drop status
                try:
                    context = await self._twitch.gql_request(
                        GQL_OPERATIONS["CurrentDrop"].with_variables({"channelID": str(channel.id)})
                    )
                    drop_data: JsonType | None = context["data"]["currentUser"][
                        "dropCurrentSession"
                    ]
                except GQLException:
                    drop_data = None

                if drop_data is not None:
                    gql_drop: TimedDrop | None = self._twitch._drops.get(drop_data["dropID"])
                    if gql_drop is not None and gql_drop.can_earn(channel):
                        gql_drop.update_minutes(drop_data["currentMinutesWatched"])
                        drop_text: str = (
                            f"{gql_drop.name} ({gql_drop.campaign.game}, "
                            f"{gql_drop.current_minutes}/{gql_drop.required_minutes})"
                        )
                        logger.log(CALL, f"Drop progress from GQL: {drop_text}")
                        handled = True

                # Solution 2: If GQL fails, figure out which campaign we're most likely mining
                # right now, and then bump up the minutes on it's drops
                if not handled:
                    active_campaign = self._twitch._inventory_service.get_active_campaign(channel)
                    if active_campaign is not None:
                        active_campaign.bump_minutes(channel)
                        # NOTE: This usually gets overwritten below
                        drop_text = f"Unknown drop ({active_campaign.game})"
                        if (active_drop := active_campaign.first_drop) is not None:
                            active_drop.display()
                            drop_text = (
                                f"{active_drop.name} ({active_drop.campaign.game}, "
                                f"{active_drop.current_minutes}/{active_drop.required_minutes})"
                            )
                        logger.log(CALL, f"Drop progress from active search: {drop_text}")
                        handled = True
                    else:
                        logger.log(CALL, "No active drop could be determined")

            # Periodic channel points poll (every ~60s) — covers the primary
            # channel plus any other channels being watched in parallel idle mode,
            # since the websocket push (process_community_points) can miss claims.
            _cp_poll_counter += 1
            if (
                _cp_poll_counter >= _CP_POLL_EVERY
                and self._twitch.settings.claim_channel_points
            ):
                _cp_poll_counter = 0
                asyncio.create_task(
                    self._twitch._message_handler_service._emit_channel_points(
                        channel._login, channel.id
                    )
                )
                idle_parallel = getattr(self._twitch.settings, "idle_parallel", True)
                if idle_parallel:
                    for idle_ch in list(self._twitch._idle_channels_set):
                        if idle_ch is channel or not idle_ch.online:
                            continue
                        asyncio.create_task(
                            self._twitch._message_handler_service._emit_channel_points(
                                idle_ch._login, idle_ch.id
                            )
                        )

            await self.watch_sleep(interval - min(time() - last_sent, interval))
