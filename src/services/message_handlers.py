"""
Message handler service for processing websocket updates.

This service handles all websocket message types including drop progress,
drop claims, notifications, stream state changes, and broadcast updates.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import aiohttp

import json as _json_mod

from src.config import CALL, DATA_DIR, GQL_OPERATIONS, State
from src.i18n import _
from src.services.drop_history import finalize_drop_claim
from src.utils import json_load, json_save, task_wrapper

_WEB_CONFIG_FILE = DATA_DIR / "web_config.json"


def _get_active_account() -> str:
    try:
        cfg = _json_mod.loads(_WEB_CONFIG_FILE.read_text()) if _WEB_CONFIG_FILE.exists() else {}
        return cfg.get("active_account") or ""
    except Exception:
        return ""


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


def _save_last_chest(channel_login: str, bonus: int) -> None:
    from datetime import datetime, timezone
    channel_login = channel_login.lower()
    p = _get_points_file().parent / "last_chest.json"
    try:
        data = _json_mod.loads(p.read_text()) if p.exists() else {}
    except Exception:
        data = {}
    data[channel_login] = {"bonus": bonus, "ts": datetime.now(timezone.utc).isoformat()}
    try:
        p.write_text(_json_mod.dumps(data, indent=2))
    except Exception:
        pass


def _get_last_webhook_notified(channel_login: str) -> int:
    channel_login = channel_login.lower()
    p = _get_points_file().parent / "last_webhook_notify.json"
    try:
        data = _json_mod.loads(p.read_text()) if p.exists() else {}
        return int(data.get(channel_login, 0))
    except Exception:
        return 0


def _set_last_webhook_notified(channel_login: str, balance: int) -> None:
    channel_login = channel_login.lower()
    p = _get_points_file().parent / "last_webhook_notify.json"
    try:
        data = _json_mod.loads(p.read_text()) if p.exists() else {}
    except Exception:
        data = {}
    data[channel_login] = balance
    try:
        p.write_text(_json_mod.dumps(data, indent=2))
    except Exception:
        pass


def _get_streaks_file() -> "Path":
    return _get_points_file().parent / "watch_streaks.json"


def _mark_streak_claimed(channel_login: str) -> None:
    from datetime import date
    channel_login = channel_login.lower()
    p = _get_streaks_file()
    try:
        data = _json_mod.loads(p.read_text()) if p.exists() else {}
    except Exception:
        data = {}
    data[channel_login] = {"active": True, "last_claimed_date": date.today().isoformat()}
    try:
        p.write_text(_json_mod.dumps(data, indent=2))
    except Exception:
        pass


def get_streak_state(channel_login: str) -> dict:
    channel_login = channel_login.lower()
    p = _get_streaks_file()
    try:
        data = _json_mod.loads(p.read_text()) if p.exists() else {}
        return data.get(channel_login, {})
    except Exception:
        return {}


def _update_daily_points_server(delta: int, data_dir: "Path") -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("Europe/Vienna")).strftime("%Y-%m-%d")
    p = data_dir / "daily_points.json"
    try:
        d = _json_mod.loads(p.read_text()) if p.exists() else {}
        if d.get("date") != today:
            d = {"date": today, "total": 0}
        d["total"] = d.get("total", 0) + delta
        p.write_text(_json_mod.dumps(d))
    except Exception:
        pass


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
            self._twitch._prediction_service.mark_channel_stale_pending(channel.name)

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
            claimed = await drop.claim()
            drop.display()
            if claimed:
                finalize_drop_claim(self._twitch, campaign, drop)

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
                async with session.post(
                    url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status >= 300:
                        body = await response.text()
                        logger.warning(
                            f"Discord webhook rejected (status {response.status}): {body[:500]}"
                        )
        except Exception as e:
            logger.warning(f"Discord webhook failed: {e}")

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
            if claimed_amount:
                _save_last_chest(channel_login, claimed_amount)
            # Detect watch streak reward
            reward_type = (
                message.get("data", {}).get("claim", {}).get("point_earn_reason")
                or message.get("data", {}).get("type", "")
            )
            if reward_type in ("WATCH_STREAK", "watch-streak"):
                if channel:
                    _mark_streak_claimed(channel.name)
                    logger.info(f"Watch streak claimed on {channel.name}")
            # Send Discord webhook for WebSocket-path claim
            webhook_url = self._twitch.settings.discord_webhook_points
            if webhook_url and claimed_amount:
                _acct_cp = _get_active_account()
                # Fetch current balance to compute watch points since last notification
                _bal_after = 0
                try:
                    _br = await self._twitch.gql_request(
                        GQL_OPERATIONS["ChannelPointsContext"].with_variables({"channelLogin": channel_login})
                    )
                    _bal_after = (_br.get("data") or {}).get("community", {}).get("channel", {}).get("self", {}).get("communityPoints", {}).get("balance", 0)
                except Exception:
                    pass
                _last_notified = _get_last_webhook_notified(channel_login)
                _watch_pts = max(0, _bal_after - (_last_notified or _bal_after) - claimed_amount) if _last_notified else 0
                _fields = [
                    {"name": "Channel", "value": channel_login, "inline": True},
                    {"name": "🎁 Bonus Chest", "value": f"+{claimed_amount} pts", "inline": True},
                ]
                if _watch_pts > 0:
                    _fields.append({"name": "📺 From watching", "value": f"+{_watch_pts} pts", "inline": True})
                if _bal_after:
                    _fields.append({"name": "Balance", "value": f"{_bal_after:,} pts", "inline": True})
                _cp_embed: dict = {"title": "💰 Channel Points", "color": 0x9147FF, "fields": _fields}
                if _acct_cp:
                    _cp_embed["footer"] = {"text": f"Account: {_acct_cp}"}
                if _bal_after:
                    _set_last_webhook_notified(channel_login, _bal_after)
                asyncio.create_task(self._send_discord_webhook(webhook_url, {"embeds": [_cp_embed]}))
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
            cp = None
            try:
                cp = data["community"]["channel"]["self"]["communityPoints"]
            except (KeyError, TypeError):
                pass
            cp_enabled = cp is not None
            cp = cp or {}
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
                    # Re-fetch balance after claim to compute actual bonus delta
                    bonus_amount = 0
                    new_points = points
                    try:
                        new_resp = await self._twitch.gql_request(
                            GQL_OPERATIONS["ChannelPointsContext"].with_variables(
                                {"channelLogin": channel_login}
                            )
                        )
                        new_data = new_resp.get("data") or {}
                        new_cp = {}
                        try:
                            new_cp = new_data["community"]["channel"]["self"]["communityPoints"]
                        except (KeyError, TypeError):
                            pass
                        new_points = new_cp.get("balance", points)
                        bonus_amount = max(0, new_points - points)
                        points = new_points
                    except Exception:
                        pass
                    if bonus_amount:
                        claimed_amount += bonus_amount
                        _save_last_chest(channel_login, bonus_amount)
                    # Discord webhook for channel points claim
                    webhook_url = self._twitch.settings.discord_webhook_points
                    if webhook_url:
                        _acct_gql = _get_active_account()
                        _last_notified_gql = _get_last_webhook_notified(channel_login)
                        _watch_gql = max(0, new_points - (_last_notified_gql or new_points) - bonus_amount) if _last_notified_gql else 0
                        _gql_fields = [
                            {"name": "Channel", "value": channel_login, "inline": True},
                            {"name": "🎁 Bonus Chest", "value": f"+{bonus_amount} pts" if bonus_amount else "Claimed", "inline": True},
                        ]
                        if _watch_gql > 0:
                            _gql_fields.append({"name": "📺 From watching", "value": f"+{_watch_gql} pts", "inline": True})
                        _gql_fields.append({"name": "Balance", "value": f"{new_points:,} pts", "inline": True})
                        _gql_embed: dict = {"title": "💰 Channel Points", "color": 0x9147FF, "fields": _gql_fields}
                        if _acct_gql:
                            _gql_embed["footer"] = {"text": f"Account: {_acct_gql}"}
                        _set_last_webhook_notified(channel_login, new_points)
                        asyncio.create_task(self._send_discord_webhook(webhook_url, {"embeds": [_gql_embed]}))
                except Exception as claim_e:
                    logger.debug(f"GQL claim failed for {channel_login}: {claim_e}")
            await self._twitch.gui._broadcaster.emit("channel_points_update", {
                "channel_id": channel_id,
                "channel_login": channel_login,
                "balance": points,
                "claimed_amount": claimed_amount,
                "cp_enabled": cp_enabled,
            })
            # Persist balance + update server-side daily points counter
            if points:
                _pfile = _get_points_file()
                history = json_load(_pfile, {}, merge=False)
                _login_key = channel_login.lower()
                old_balance = history.get(_login_key, 0)
                if old_balance > 0 and points > old_balance:
                    _update_daily_points_server(points - old_balance, _pfile.parent)
                history[_login_key] = points
                json_save(_pfile, history)
                # Append timestamped snapshot for analytics
                _ts_file = _pfile.parent / "channel_points_ts.json"
                try:
                    _ts_data = _json_mod.loads(_ts_file.read_text()) if _ts_file.exists() else {}
                    _snapshots = _ts_data.get(_login_key, [])
                    _snapshots.append({"ts": datetime.now(timezone.utc).isoformat(), "balance": points})
                    if len(_snapshots) > 1000:
                        _snapshots = _snapshots[-1000:]
                    _ts_data[_login_key] = _snapshots
                    _ts_file.write_text(_json_mod.dumps(_ts_data))
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Could not fetch channel points balance for {channel_login}: {e}")

    @task_wrapper
    async def process_moments(self, channel_id: int, message: JsonType) -> None:
        if not self._twitch.settings.claim_moments:
            return
        msg_type = message.get("type", "")
        if msg_type not in ("active", "COMMUNITY_MOMENT_CALLOUT_CREATED"):
            return
        moment_id = (
            message.get("data", {}).get("moment_id")
            or message.get("data", {}).get("momentID")
        )
        if not moment_id:
            return
        try:
            await self._twitch.gql_request(
                GQL_OPERATIONS["ClaimMoment"].with_variables(
                    {"input": {"momentID": moment_id}}
                )
            )
            logger.info(f"Claimed moment {moment_id} on channel {channel_id}")
            channel = self._twitch.channels.get(channel_id)
            channel_name = channel.name if channel else str(channel_id)
            webhook_url = self._twitch.settings.discord_webhook_points
            if webhook_url:
                embed = {
                    "title": "⭐ Moment Claimed!",
                    "color": 0x9147FF,
                    "fields": [{"name": "Channel", "value": channel_name, "inline": True}],
                }
                asyncio.create_task(self._send_discord_webhook(webhook_url, {"embeds": [embed]}))
        except Exception as e:
            logger.warning(f"Failed to claim moment {moment_id}: {e}")

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
