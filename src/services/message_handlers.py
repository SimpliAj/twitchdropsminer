"""
Message handler service for processing websocket updates.

This service handles all websocket message types including drop progress,
drop claims, notifications, stream state changes, and broadcast updates.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import aiohttp

import json as _json_mod

from src.config import CALL, DATA_DIR, GQL_OPERATIONS, State
from src.i18n import _
from src.utils import json_load, json_save, task_wrapper

_WEB_CONFIG_FILE = DATA_DIR / "web_config.json"


def _get_points_file() -> "Path":
    try:
        cfg = _json_mod.loads(_WEB_CONFIG_FILE.read_text()) if _WEB_CONFIG_FILE.exists() else {}
        account = cfg.get("active_account")
        if account:
            d = DATA_DIR / "accounts" / account
            d.mkdir(parents=True, exist_ok=True)
            return d / "channel_points.json"
    except Exception:
        pass
    return DATA_DIR / "channel_points.json"


if TYPE_CHECKING:
    from src.config import JsonType
    from src.core.client import Twitch
    from src.models import TimedDrop
    from src.models.channel import Channel, Stream


logger = logging.getLogger("TwitchDrops")


class MessageHandlerService:
    """
    Service responsible for processing websocket messages.

    Handles:
    - Drop progress updates (websocket)
    - Drop claim notifications (websocket)
    - User notifications (websocket)
    - Stream state changes (viewcount, stream-up, stream-down)
    - Broadcast settings updates (game/title changes)
    - Channel update callbacks
    """

    def __init__(self, twitch: Twitch) -> None:
        """
        Initialize the message handler service.

        Args:
            twitch: The Twitch client instance
        """
        self._twitch = twitch

    @task_wrapper
    async def process_stream_state(self, channel_id: int, message: JsonType) -> None:
        """
        Process websocket stream state updates (viewcount, stream-up, stream-down).

        Args:
            channel_id: The channel ID that sent the update
            message: The websocket message payload
        """
        msg_type: str = message["type"]
        channel: Channel | None = self._twitch.channels.get(channel_id)

        if channel is None:
            logger.error(f"Stream state change for a non-existing channel: {channel_id}")
            return

        if msg_type == "viewcount":
            if not channel.online:
                # if it's not online for some reason, set it so
                channel.check_online()
            else:
                viewers = message["viewers"]
                channel.viewers = viewers
                channel.display()
                # logger.debug(f"{channel.name} viewers: {viewers}")
        elif msg_type == "stream-down":
            channel.set_offline()
        elif msg_type == "stream-up":
            channel.check_online()
        elif msg_type == "commercial":
            # skip these
            pass
        else:
            logger.warning(f"Unknown stream state: {msg_type}")

    @task_wrapper
    async def process_stream_update(self, channel_id: int, message: JsonType) -> None:
        """
        Process websocket broadcast settings updates (game/title changes).

        Args:
            channel_id: The channel ID that sent the update
            message: The websocket message payload containing:
                - channel_id: Channel ID string
                - type: "broadcast_settings_update"
                - channel: Channel login name
                - old_status: Previous stream title
                - status: New stream title
                - old_game: Previous game name
                - game: New game name
                - old_game_id: Previous game ID
                - game_id: New game ID
        """
        channel: Channel | None = self._twitch.channels.get(channel_id)

        if channel is None:
            logger.error(f"Broadcast settings update for a non-existing channel: {channel_id}")
            return

        if message["old_game"] != message["game"]:
            game_change = f", game changed: {message['old_game']} -> {message['game']}"
        else:
            game_change = ""

        logger.log(CALL, f"Channel update from websocket: {channel.name}{game_change}")

        # There's no information about channel tags here, but this event is triggered
        # when the tags change. We can use this to just update the stream data after the change.
        # Use 'check_online' to introduce a delay, allowing for multiple title and tags
        # changes before we update. This eventually calls 'on_channel_update' below.
        channel.check_online()

    def on_channel_update(
        self, channel: Channel, stream_before: Stream | None, stream_after: Stream | None
    ) -> None:
        """
        Called by a Channel when its status is updated (ONLINE, OFFLINE, title/tags change).

        This method determines whether a channel switch is needed based on the
        status change and channel watching eligibility.

        Args:
            channel: The channel that was updated
            stream_before: The previous stream state (None if was offline)
            stream_after: The new stream state (None if now offline)

        Note:
            'stream_before' gets deallocated once this function finishes.
        """
        watching_channel: Channel | None = self._twitch.watching_channel.get_with_default(None)
        is_watching_this: bool = watching_channel is not None and watching_channel == channel

        # Channel going from OFFLINE to ONLINE
        if stream_before is None and stream_after is not None:
            if self._twitch.can_watch(channel) and self._twitch.should_switch(channel):
                self._twitch.print(_.t["status"]["goes_online"].format(channel=channel.name))
                self._twitch.watch(channel)
            else:
                logger.info(f"{channel.name} goes ONLINE")

        # Channel going from ONLINE to OFFLINE
        elif stream_before is not None and stream_after is None:
            if is_watching_this:
                self._twitch.print(_.t["status"]["goes_offline"].format(channel=channel.name))
                self._twitch.change_state(State.CHANNEL_SWITCH)
            else:
                logger.info(f"{channel.name} goes OFFLINE")

        # Channel staying ONLINE but with updates
        elif stream_before is not None and stream_after is not None:
            drops_status: str = (
                f"(🎁: {stream_before.drops_enabled and '✔' or '❌'} -> "
                f"{stream_after.drops_enabled and '✔' or '❌'})"
            )

            if is_watching_this and not self._twitch.can_watch(channel):
                # Watching this channel but can't watch it anymore
                logger.info(f"{channel.name} status updated, switching... {drops_status}")
                self._twitch.change_state(State.CHANNEL_SWITCH)
            elif not is_watching_this:
                # Not watching this channel
                logger.info(f"{channel.name} status updated {drops_status}")
                if self._twitch.can_watch(channel) and self._twitch.should_switch(channel):
                    self._twitch.watch(channel)

        # Channel was OFFLINE and stays OFFLINE
        else:
            logger.log(CALL, f"{channel.name} stays OFFLINE")

        channel.display()

    @task_wrapper
    async def process_drops(self, user_id: int, message: JsonType) -> None:
        """
        Process websocket drop progress and claim updates.

        Args:
            user_id: The user ID that sent the message
            message: The websocket message payload, examples:
                - {"type": "drop-progress", data: {"current_progress_min": 3, "required_progress_min": 10}}
                - {"type": "drop-claim", data: {"drop_instance_id": ...}}
        """
        msg_type: str = message["type"]
        if msg_type not in ("drop-progress", "drop-claim"):
            return

        drop_id: str = message["data"]["drop_id"]
        drop: TimedDrop | None = self._twitch._drops.get(drop_id)
        watching_channel: Channel | None = self._twitch.watching_channel.get_with_default(None)

        if msg_type == "drop-claim":
            if drop is None:
                logger.error(
                    f"Received a drop claim ID for a non-existing drop: {drop_id}\n"
                    f"Drop claim ID: {message['data']['drop_instance_id']}"
                )
                return

            drop.update_claim(message["data"]["drop_instance_id"])
            campaign = drop.campaign
            await drop.claim()
            drop.display()
            # Discord webhook for drop claim
            webhook_url = self._twitch.settings.discord_webhook_drops
            if webhook_url and drop.is_claimed and drop.id not in self._twitch._webhook_sent_drops:
                self._twitch._webhook_sent_drops.add(drop.id)
                embed: dict = {
                    "title": "🎁 Drop Claimed!",
                    "color": 0x9147ff,
                    "fields": [
                        {"name": "Game", "value": campaign.game.name, "inline": True},
                        {"name": "Drop", "value": drop.name, "inline": True},
                        {"name": "Reward", "value": drop.rewards_text(), "inline": False},
                    ],
                }
                if drop.benefits:
                    embed["thumbnail"] = {"url": drop.benefits[0].image_url}
                asyncio.create_task(self._send_discord_webhook(webhook_url, {"embeds": [embed]}))

            # Save to drop history
            import datetime as _dt, json as _json
            _hist_file = _get_points_file().parent / "drops_history.json"
            _entry = {
                "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                "game": campaign.game.name,
                "drop": drop.name,
                "reward": drop.rewards_text(),
                "image_url": drop.benefits[0].image_url if drop.benefits else None,
            }
            try:
                _hist = _json.loads(_hist_file.read_text()) if _hist_file.exists() else []
                _hist.insert(0, _entry)
                _hist_file.write_text(_json.dumps(_hist[:500], indent=2))
            except Exception:
                pass

            # About 4-20s after claiming the drop, next drop can be started
            # by re-sending the watch payload. We can test for it by fetching the current drop
            # via GQL, and then comparing drop IDs.
            await asyncio.sleep(4)

            if watching_channel is not None:
                for _attempt in range(8):
                    context = await self._twitch.gql_request(
                        GQL_OPERATIONS["CurrentDrop"].with_variables(
                            {"channelID": str(watching_channel.id)}
                        )
                    )
                    drop_data: JsonType | None = context["data"]["currentUser"][
                        "dropCurrentSession"
                    ]
                    if drop_data is None or drop_data["dropID"] != drop.id:
                        break
                    await asyncio.sleep(2)

            if campaign.can_earn(watching_channel):
                self._twitch.restart_watching()
            else:
                self._twitch.change_state(State.INVENTORY_FETCH)
            return

        assert msg_type == "drop-progress"
        if drop is not None:
            drop_text = (
                f"{drop.name} ({drop.campaign.game}, "
                f"{message['data']['current_progress_min']}/"
                f"{message['data']['required_progress_min']})"
            )
        else:
            drop_text = "<Unknown>"

        logger.log(CALL, f"Drop update from websocket: {drop_text}")

        if drop is not None and drop.can_earn(self._twitch.watching_channel.get_with_default(None)):
            # the received payload is for the drop we expected
            drop.update_minutes(message["data"]["current_progress_min"])

    async def _send_discord_webhook(self, url: str, payload: dict) -> None:
        if not url:
            return
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10))
        except Exception as e:
            logger.debug(f"Discord webhook failed: {e}")

    @task_wrapper
    async def process_idle_stream_state(self, channel_id: int, message: JsonType) -> None:
        """Handle stream-down for idle watch channels — re-enter IDLE to pick next channel."""
        if message.get("type") == "stream-down":
            logger.info(f"Idle channel {channel_id} went offline, switching...")
            self._twitch.change_state(State.IDLE)

    @task_wrapper
    async def process_community_points(self, channel_id: int, message: JsonType) -> None:
        msg_type = message.get("type", "")
        logger.info(f"Community points event on {channel_id}: type={msg_type} | raw={message}")
        if not self._twitch.settings.claim_channel_points:
            return
        if msg_type != "claim-available":
            return
        claim = message.get("data", {}).get("claim", {})
        claim_id = claim.get("id")
        point_gain = claim.get("point_gain", {})
        claimed_amount = point_gain.get("total_points", 0) if point_gain else 0
        if not claim_id:
            return
        channel = self._twitch.channels.get(channel_id)
        channel_login = channel._login if channel else str(channel_id)
        try:
            await self._twitch.gql_request(
                GQL_OPERATIONS["ClaimCommunityPoints"].with_variables({
                    "input": {
                        "claimID": claim_id,
                        "channelID": str(channel_id),
                    }
                })
            )
            logger.info(f"Claimed channel points on {channel_login} (+{claimed_amount})")
            # Send Discord webhook for WebSocket-path claim
            webhook_url = self._twitch.settings.discord_webhook_points
            if webhook_url and claimed_amount:
                asyncio.create_task(self._send_discord_webhook(webhook_url, {
                    "embeds": [{
                        "title": "💰 Bonus Chest Claimed!",
                        "color": 0xffd700,
                        "fields": [
                            {"name": "Channel", "value": channel_login, "inline": True},
                            {"name": "Bonus", "value": f"+{claimed_amount} pts", "inline": True},
                        ],
                    }]
                }))
            # Fetch updated balance and broadcast to UI
            await self._emit_channel_points(channel_login, channel_id, claimed_amount)
        except Exception as e:
            logger.warning(f"Failed to claim channel points on {channel_login}: {e}")

    async def _emit_channel_points(
        self, channel_login: str, channel_id: int, claimed_amount: int = 0
    ) -> None:
        try:
            resp = await self._twitch.gql_request(
                GQL_OPERATIONS["ChannelPointsContext"].with_variables(
                    {"channelLogin": channel_login}
                )
            )
            data = resp.get("data") or {}
            cp = {}
            try:
                cp = data["community"]["channel"]["self"]["communityPoints"]
            except (KeyError, TypeError):
                pass
            points: int = cp.get("balance", 0)
            # Claim available chest via GQL polling (fallback if PubSub misses it)
            available_claim = cp.get("availableClaim")
            if available_claim and available_claim.get("id"):
                try:
                    await self._twitch.gql_request(
                        GQL_OPERATIONS["ClaimCommunityPoints"].with_variables({
                            "input": {
                                "claimID": available_claim["id"],
                                "channelID": str(channel_id),
                            }
                        })
                    )
                    logger.info(f"Claimed channel points via GQL poll on {channel_login} | claim data: {available_claim}")
                    # Discord webhook for channel points claim
                    webhook_url = self._twitch.settings.discord_webhook_points
                    if webhook_url:
                        # Try multiple known field names for bonus amount
                        bonus = (available_claim.get("pointsEarnedForClaim")
                                 or available_claim.get("pointGain")
                                 or available_claim.get("point_gain") or {})
                        bonus_amount = (bonus.get("totalPoints")
                                        or bonus.get("total_points") or 0)
                        asyncio.create_task(self._send_discord_webhook(webhook_url, {
                            "embeds": [{
                                "title": "💰 Bonus Chest Claimed!",
                                "color": 0xffd700,
                                "fields": [
                                    {"name": "Channel", "value": channel_login, "inline": True},
                                    {"name": "Bonus", "value": f"+{bonus_amount} pts" if bonus_amount else "Claimed", "inline": True},
                                    {"name": "Balance", "value": f"{points:,} pts", "inline": True},
                                ],
                            }]
                        }))
                except Exception as claim_e:
                    logger.debug(f"GQL claim failed for {channel_login}: {claim_e}")
            await self._twitch.gui._broadcaster.emit("channel_points_update", {
                "channel_id": channel_id,
                "channel_login": channel_login,
                "balance": points,
                "claimed_amount": claimed_amount,
            })
            # Persist balance
            if points:
                _pfile = _get_points_file()
                history = json_load(_pfile, {}, merge=False)
                history[channel_login] = points
                json_save(_pfile, history)
        except Exception as e:
            logger.warning(f"Could not fetch channel points balance for {channel_login}: {e}")

    @task_wrapper
    async def process_notifications(self, user_id: int, message: JsonType) -> None:
        """
        Process websocket notification updates.

        Handles notification for drop rewards that are ready to claim.

        Args:
            user_id: The user ID that sent the notification
            message: The websocket message payload
        """
        if message["type"] == "create-notification":
            data: JsonType = message["data"]["notification"]
            if data["type"] == "user_drop_reward_reminder_notification":
                self._twitch.change_state(State.INVENTORY_FETCH)
                await self._twitch.gql_request(
                    GQL_OPERATIONS["NotificationsDelete"].with_variables(
                        {"input": {"id": data["id"]}}
                    )
                )
