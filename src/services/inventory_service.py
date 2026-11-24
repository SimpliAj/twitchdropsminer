"""
Inventory service for managing campaigns, drops, and inventory fetching.

This service handles fetching campaign data from Twitch's GraphQL API,
managing the inventory state, and determining active campaigns.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import TYPE_CHECKING, Any, TypeVar

import aiohttp
from dateutil.parser import isoparse

from src.api import GQLClient
from src.config import GQL_OPERATIONS
from src.config.constants import GQLRawQuery
from src.config.operations import (
    REWARD_CAMPAIGNS_QUERY,
    REWARD_COMPLETED_QUERY,
    REWARD_HINTS_QUERY,
    REWARD_PROGRESS_QUERY,
)
from src.config.reward_priority import RewardPriority
from src.exceptions import ExitRequest, GQLException, MinerException
from src.i18n import _
from src.models import DropsCampaign, RewardCampaign
from src.models.reward_campaign import RewardLedger
from src.services.public_catalog import campaign_from_feed, fetch_public_campaigns, link_states
from src.utils import chunk


if TYPE_CHECKING:
    from collections.abc import Callable

    from src.config import JsonType
    from src.core.client import Twitch
    from src.models.channel import Channel


logger = logging.getLogger("TwitchDrops")
_T = TypeVar("_T")


def _as_list(value: object) -> list[Any]:
    """Treat anything that is not a list as empty, so a malformed entry is skipped."""
    return value if isinstance(value, list) else []


def _has_id(entry: object) -> bool:
    """Whether the entry is a dict with a non-empty string ID."""
    return isinstance(entry, dict) and isinstance(entry.get("id"), str) and bool(entry["id"])


# Failures a malformed or slow reward reply can raise; none may block regular drops.
REWARD_ERRORS = (MinerException, aiohttp.ClientError, KeyError, IndexError, TypeError,
                 ValueError, AttributeError, TimeoutError)


class InventoryService:
    """
    Service responsible for inventory and campaign management.

    Handles:
    - Fetching campaign details from GraphQL
    - Fetching inventory (in-progress campaigns)
    - Determining active campaign for a channel
    - Managing campaign data and claimed benefits
    """

    def __init__(self, twitch: Twitch) -> None:
        """
        Initialize the inventory service.

        Args:
            twitch: The Twitch client instance
        """
        self._twitch = twitch
        # Reward grants, claims and stale readings per group; outlives reloads and failed ones.
        self._reward_ledger = RewardLedger()
        self._reward_user: str | int | None = None

    async def fetch_campaigns(
        self, campaigns_chunk: list[tuple[str, JsonType]]
    ) -> dict[str, JsonType]:
        """
        Fetch detailed campaign data for a chunk of campaign IDs.

        Args:
            campaigns_chunk: List of (campaign_id, campaign_data) tuples

        Returns:
            Dictionary mapping campaign IDs to their detailed data
        """
        campaign_ids: dict[str, JsonType] = dict(campaigns_chunk)
        auth_state = await self._twitch.get_auth()

        response_list_raw = await self._twitch.gql_request(
            [
                GQL_OPERATIONS["CampaignDetails"].with_variables(
                    {"channelLogin": str(auth_state.user_id), "dropID": cid}
                )
                for cid in campaign_ids
            ]
        )

        # Ensure we have a list
        response_list: list[JsonType] = (
            response_list_raw if isinstance(response_list_raw, list) else [response_list_raw]
        )

        # Twitch answers "dropCampaign": null for a campaign this account may not
        # see (the public feed lists everyone's campaigns) -- skip those instead
        # of crashing the whole inventory fetch.
        fetched_data: dict[str, JsonType] = {}
        for response_json in response_list:
            user = (response_json.get("data") or {}).get("user")
            campaign_data = user.get("dropCampaign") if user else None
            if campaign_data is not None:
                fetched_data[campaign_data["id"]] = campaign_data
        campaign_ids = {cid: v for cid, v in campaign_ids.items() if cid in fetched_data}
        if len(fetched_data) < len(response_list):
            logger.info(
                f"CampaignDetails: {len(fetched_data)} of {len(response_list)} campaigns "
                f"available to this account (rest returned null)"
            )
            if not fetched_data:
                logger.debug(f"CampaignDetails sample response: {str(response_list[0])[:400]}")

        return GQLClient.merge_data(campaign_ids, fetched_data)

    @staticmethod
    def _parse_reward_progress(response: JsonType) -> tuple[dict[str, JsonType], dict[str, str]]:
        """Return reward group readings and the in-progress campaign ID, both keyed by group ID."""
        in_progress = response["data"]["currentUser"]["inventory"][
            "viewerRewardDropCampaignsInProgress"
        ]
        readings: dict[str, JsonType] = {}
        campaign_ids: dict[str, str] = {}
        for campaign in _as_list(in_progress):
            if not isinstance(campaign, dict):
                continue
            for group in _as_list(campaign.get("rewardGroups")):
                if _has_id(group) and isinstance(group.get("self"), dict):
                    readings[group["id"]] = group["self"]
                    if isinstance(campaign.get("id"), str):
                        campaign_ids[group["id"]] = campaign["id"]
        return readings, campaign_ids

    async def _fetch_reward_progress(self) -> tuple[dict[str, JsonType], dict[str, str]]:
        """Query the viewer's reward group progress (see _parse_reward_progress)."""
        response = await self._twitch.gql_request(GQLRawQuery(REWARD_PROGRESS_QUERY))
        return self._parse_reward_progress(response)

    @staticmethod
    def _parse_reward_hints(response: JsonType) -> tuple[dict[str, str], set[str]]:
        """Return window end times by group ID and the IDs of rewards Twitch reports as badges.

        The two hints are read independently, so a missing one never drops the other.
        """
        windows: dict[str, str] = {}
        badges: set[str] = set()
        in_progress = response["data"]["currentUser"]["inventory"][
            "viewerRewardDropCampaignsInProgress"
        ]
        for campaign in _as_list(in_progress):
            if not isinstance(campaign, dict):
                continue
            for group in _as_list(campaign.get("rewardGroups")):
                if not isinstance(group, dict):
                    continue
                reading = group.get("self")
                window = reading.get("currentWindow") if isinstance(reading, dict) else None
                if _has_id(group) and isinstance(window, dict) and window.get("expiresAt") is not None:
                    windows[group["id"]] = window["expiresAt"]
                for reward in _as_list(group.get("rewards")):
                    if not _has_id(reward):
                        continue
                    detail = reward.get("dropDetail")
                    if isinstance(detail, dict) and isinstance(detail.get("badge"), dict):
                        badges.add(reward["id"])
        return windows, badges

    async def _optional_reward_query(
        self, query: str, parse: Callable[[JsonType], _T], empty: _T, unavailable: str
    ) -> _T:
        """Run an optional reward query; any failure only loses its result (logged at info)."""
        try:
            response = await asyncio.wait_for(
                self._twitch.gql_request(GQLRawQuery(query)), timeout=5
            )
            return parse(response)
        except ExitRequest:
            raise
        except REWARD_ERRORS:
            logger.info(unavailable)
            return empty

    async def _fetch_reward_hints(self) -> tuple[dict[str, str], set[str]]:
        """Return (window end times, badge reward IDs); any failure only loses these hints."""
        return await self._optional_reward_query(
            REWARD_HINTS_QUERY, self._parse_reward_hints, ({}, set()),
            "Reward hints are unavailable; a granted goal is treated as done",
        )

    @staticmethod
    def _parse_completed_reward_campaigns(response: JsonType) -> set[str]:
        """Return the IDs of reward campaigns Twitch lists with status COMPLETED."""
        completed = response["data"]["currentUser"]["inventory"]["completedRewardCampaigns"]
        return {
            entry["id"]
            for entry in _as_list(completed)
            if _has_id(entry) and entry.get("status") == "COMPLETED"
        }

    async def _fetch_completed_reward_campaigns(self) -> set[str]:
        """Return completed reward campaign IDs; any failure only loses this list."""
        return await self._optional_reward_query(
            REWARD_COMPLETED_QUERY, self._parse_completed_reward_campaigns, set(),
            "Completed Open Reward Campaigns are unavailable; none are skipped",
        )

    async def fetch_reward_campaigns(self) -> list[RewardCampaign]:
        """Build watch-based sitewide Open Reward Campaigns; failures never block regular drops."""
        try:
            # The GQL client retries some service errors without a cap, so bound the whole call.
            return await asyncio.wait_for(self._load_reward_campaigns(), timeout=15)
        except ExitRequest:
            raise
        except REWARD_ERRORS as exc:
            if "IntegrityCheckFailed" in str(exc):
                self._twitch._auth_state.reject_integrity_token()
                logger.warning(
                    "Open Reward Campaigns failed the integrity check; regular drops are unaffected"
                )
            else:
                logger.warning(
                    f"Open Reward Campaigns could not be loaded ({type(exc).__name__}); "
                    "regular drops are unaffected"
                )
            return []

    async def _load_reward_campaigns(self) -> list[RewardCampaign]:
        progress_response, response = await self._twitch.gql_request(
            [GQLRawQuery(REWARD_PROGRESS_QUERY), GQLRawQuery(REWARD_CAMPAIGNS_QUERY)]
        )
        progress, campaign_ids = self._parse_reward_progress(progress_response)
        # Concurrent, so two slow optional queries stay within the 15 s bound of the whole load.
        hints_task = asyncio.create_task(self._fetch_reward_hints())
        completed_task = asyncio.create_task(self._fetch_completed_reward_campaigns())
        try:
            (windows, badges), completed = await asyncio.gather(hints_task, completed_task)
        finally:
            # gather propagates a child's exception while its sibling may still be running.
            hints_task.cancel()
            completed_task.cancel()
            await asyncio.gather(hints_task, completed_task, return_exceptions=True)
        logger.info(
            "Reward inventory: %d group readings, %d windows, %d badges, %d completed campaigns",
            len(progress), len(windows), len(badges), len(completed),
        )
        user_id = (await self._twitch.get_auth()).user_id
        if self._reward_user is not None and self._reward_user != user_id:
            # Another account must not inherit grants, stale readings or holds.
            self._reward_ledger.reset()
        self._reward_user = user_id
        campaigns: list[RewardCampaign] = []
        build_campaign = partial(
            RewardCampaign.from_reward_campaign, self._twitch, progress_by_group=progress,
            user_id=user_id, windows=windows, campaign_ids=campaign_ids,
            badge_rewards=badges, ledger=self._reward_ledger,
        )
        # shortcut: game-specific reward campaigns may need an account link, skipped
        # until one is verified to progress without it.
        for data in response["data"]["rewardCampaignsAvailableToUser"] or []:
            if not isinstance(data, dict):
                continue
            try:
                if not data.get("isSitewide"):
                    continue
                campaign = build_campaign(data)
                if campaign is None:
                    continue
                # A newly finished group can lose selection to another lane on the same load.
                # Keep its real grant count and reward metadata before discarding that lane.
                for group in data["rewardGroups"]:
                    if not _has_id(group):
                        continue
                    gid = group["id"]
                    if gid in campaign.timed_drops or gid not in self._reward_ledger.announced:
                        continue
                    reading = progress.get(gid)
                    if reading and reading.get("grantCount") != self._reward_ledger.announced[gid]:
                        previous = build_campaign({**data, "rewardGroups": [group]})
                        if previous is not None:
                            previous.queue_new_grants()
                if data["id"] in completed and not campaign.earnable_from_reading:
                    # Built, then dropped: Twitch lists it as completed and no reading says otherwise.
                    campaign.queue_new_grants()
                    logger.info('Open Reward Campaign "%s" is completed', campaign.name)
                    continue
                campaigns.append(campaign)
                drop = next(iter(campaign.drops))
                reading = progress.get(drop.id) or {}
                logger.info(
                    'Open Reward Campaign "%s" (%s): goal %d min, %d min watched, status %s, '
                    "earned %d times%s",
                    campaign.name, campaign.game.name, drop.required_minutes,
                    drop.real_current_minutes, reading.get("status", "NOT_STARTED"),
                    campaign.reward_grants,
                    f", next window {drop.starts_at.astimezone().strftime('%Y-%m-%d %H:%M')}"
                    if drop.starts_at > datetime.now(timezone.utc) else "",
                )
            # Deliberately not REWARD_ERRORS: only a malformed campaign is skipped here; network
            # errors, timeouts and exit requests belong to the caller.
            except (KeyError, TypeError, ValueError, AttributeError):
                logger.warning("Skipping a broken Open Reward Campaign")
        return campaigns

    async def refresh_reward_progress(
        self,
        campaigns: list[RewardCampaign],
        channel: Channel,
        is_current: Callable[[], bool] | None = None,
    ) -> bool:
        """Poll reward progress once for these campaigns; False means the caller should fall back.

        is_current is checked after the request: a reply for a channel that is no longer
        the watched one is dropped without applying (and without a fallback).
        """
        revisions = {campaign.id: campaign.progress_revision for campaign in campaigns}
        try:
            progress, _campaign_ids = await asyncio.wait_for(
                self._fetch_reward_progress(), timeout=5
            )
            for campaign in campaigns:
                # Announcing a previous campaign's grants may have yielded during a switch.
                if is_current is not None and not is_current():
                    return True
                if (
                    self._twitch._campaigns.get(campaign.id) is not campaign
                    or campaign.progress_revision != revisions[campaign.id]
                ):
                    # A newer observation or inventory replacement superseded this reply.
                    continue
                try:
                    drop_id = next(iter(campaign.timed_drops))
                    campaign.apply_reward_progress(progress.get(drop_id), channel)
                    await campaign.announce_new_grants()
                except ExitRequest:
                    raise
                except REWARD_ERRORS:
                    # Only this campaign is unobserved: estimate it, and leave the ones
                    # already applied alone.
                    logger.warning("Reward progress for %s could not be applied", campaign.name)
                    campaign.bump_minutes(channel)
        except ExitRequest:
            raise
        except REWARD_ERRORS:
            return False
        return True

    async def fetch_inventory(self) -> None:
        """
        Fetch the complete inventory including campaigns and drops.

        This method:
        1. Fetches in-progress campaigns (inventory)
        2. Fetches available campaigns
        3. Fetches detailed data for each campaign
        4. Creates DropsCampaign objects
        5. Updates GUI with campaign information
        6. Sets up maintenance triggers for campaign timing changes
        """
        try:
            await self._fetch_inventory()
        except GQLException as exc:
            # "failed integrity check" / IntegrityCheckFailed on dropCampaigns:
            # ClientType.WEB's GQL requests need a real Client-Integrity token
            # (see _AuthState._ensure_integrity_token) that a browser JS
            # challenge mints; acquisition can fail or not have completed yet
            # (slow, or environment-specific).
            # Letting this escape used to crash the entire miner outright on
            # every single occurrence (reported live: right after a fresh,
            # successful login, before anything else got a chance to run) --
            # treat it the same as the already-handled "currentUser is None"
            # transient case instead: log and let the periodic reload (the
            # finally: below reschedules it regardless) retry later, once a
            # token is acquired or the next attempt simply succeeds.
            if "IntegrityCheckFailed" in str(exc):
                logger.warning(
                    "Inventory fetch failed Twitch's integrity check -- will retry on the "
                    "next scheduled reload instead of crashing. This is expected until a "
                    "Client-Integrity token has been acquired (see the startup/login logs)."
                )
            else:
                raise
        finally:
            # Always reschedule the maintenance heartbeat, even if the fetch above
            # raised partway through. It used to only be re-armed on a clean finish,
            # so a single transient GQL/network error would silently kill the periodic
            # reload for the rest of the session, with nothing left to ever retry it.
            if self._twitch._mnt_task is not None and not self._twitch._mnt_task.done():
                self._twitch._mnt_task.cancel()
            self._twitch._mnt_task = asyncio.create_task(
                self._twitch._maintenance_service.run_maintenance_task()
            )

    async def _fetch_inventory(self) -> None:
        status_update = self._twitch.gui.status.update
        status_update(_.t["gui"]["status"]["fetching_inventory"])

        # fetch in-progress campaigns (inventory)
        response = await self._twitch.gql_request(GQL_OPERATIONS["Inventory"])
        current_user: JsonType | None = response["data"]["currentUser"]
        if current_user is None:
            # Twitch can answer with a "server error" GQL error scoped to the whole
            # "currentUser" field while still returning HTTP 200 (no exception raised)
            # - see watch_service.watch_loop for the full explanation. The maintenance
            # heartbeat gets rescheduled regardless (see the finally: above), so this
            # just delays the fetch to the next periodic retry instead of crashing.
            raise GQLException("Inventory query returned currentUser=None (transient Twitch-side error)")
        inventory: JsonType = current_user["inventory"]
        ongoing_campaigns: list[JsonType] = inventory["dropCampaignsInProgress"] or []

        # this contains claimed benefit edge IDs, not drop IDs
        claimed_benefits: dict[str, datetime] = {
            b["id"]: isoparse(b["lastAwardedAt"]) for b in inventory["gameEventDrops"]
        }

        inventory_data: dict[str, JsonType] = {c["id"]: c for c in ongoing_campaigns}

        # fetch general available campaigns data (campaigns)
        applicable_statuses = ("ACTIVE", "UPCOMING")
        available_campaigns: dict[str, JsonType] = {}
        public_records: dict[str, JsonType] = {}
        try:
            response = await self._twitch.gql_request(GQL_OPERATIONS["Campaigns"])
            current_user = response["data"]["currentUser"]
            if current_user is None:
                raise GQLException(
                    "Campaigns query returned currentUser=None (transient Twitch-side error)"
                )
            self._twitch._auth_state.mark_integrity_accepted()
            available_list: list[JsonType] = current_user["dropCampaigns"] or []
            available_campaigns = {
                c["id"]: c
                for c in available_list
                if c["status"] in applicable_statuses  # that are currently not expired
            }
        except GQLException as exc:
            if "IntegrityCheckFailed" not in str(exc):
                raise
            self._twitch._auth_state.reject_integrity_token()
            # The account-bound campaign list is integrity-gated, but the list
            # itself needs no account: take ids from the public feed and let
            # the CampaignDetails queries below fill in the rest.
            logger.warning("Campaigns query failed the integrity check, using the public drops feed")
            public = await fetch_public_campaigns(
                await self._twitch.get_session(), self._twitch.settings.proxy
            )
            public_records = {c["id"]: c for c in public}
            # Ids only: the feed's records are shaped differently from
            # CampaignDetails (e.g. allow.channels [] vs null), and merging the
            # two raised "Inconsistent merge data". The details are the truth;
            # the feed records are only used below for campaigns without any.
            available_campaigns = {cid: {} for cid in public_records}
            if not available_campaigns and not inventory_data:
                raise

        # fetch detailed data for each campaign, in chunks
        status_update(_.t["gui"]["status"]["fetching_campaigns"])
        fetch_campaigns_tasks: list[asyncio.Task[Any]] = [
            asyncio.create_task(self.fetch_campaigns(campaigns_chunk))
            for campaigns_chunk in chunk(available_campaigns.items(), 20)
        ]

        try:
            for coro in asyncio.as_completed(fetch_campaigns_tasks):
                try:
                    chunk_campaigns_data = await coro
                except GQLException as exc:
                    # A chunk's CampaignDetails query can keep failing even after
                    # gql_client's own retries are exhausted (e.g. Twitch-side
                    # "service error" that's specific to one campaign). Losing
                    # detail for that one chunk of ~20 campaigns shouldn't take
                    # down the entire inventory fetch (and thus the whole miner) -
                    # log it and keep going with everything else that succeeded.
                    logger.error(f"Failed to fetch campaign details for a chunk, skipping it: {exc}")
                    if "IntegrityCheckFailed" in str(exc):
                        self._twitch._auth_state.reject_integrity_token()
                    continue
                # merge the inventory and campaigns datas together
                inventory_data = GQLClient.merge_data(inventory_data, chunk_campaigns_data)
        except Exception:
            # asyncio.as_completed doesn't cancel tasks on errors
            for task in fetch_campaigns_tasks:
                task.cancel()
            raise

        # Twitch no longer answers the account-bound campaign queries for some
        # identities (dropCampaigns/CampaignDetails come back null), but the public
        # feed carries the full campaign data: use it for every campaign that has
        # no details yet, letting real inventory (progress) data win when merged.
        states = link_states(inventory["gameEventDrops"])
        for campaign_id, record in public_records.items():
            existing = inventory_data.get(campaign_id)
            if existing is not None and "allow" in existing:
                continue
            forged = campaign_from_feed(record, states)
            try:
                inventory_data[campaign_id] = (
                    GQLClient.merge_data(existing, forged) if existing else forged
                )
            except Exception as exc:
                logger.warning(f"Could not merge feed data for campaign {campaign_id}: {exc}")
                inventory_data[campaign_id] = existing or forged

        # filter out invalid campaigns
        for campaign_id in list(inventory_data.keys()):
            if inventory_data[campaign_id]["game"] is None:
                del inventory_data[campaign_id]

        # use the merged data to create campaign objects
        campaigns: list[DropsCampaign] = [
            DropsCampaign(self._twitch, campaign_data, claimed_benefits)
            for campaign_data in inventory_data.values()
        ]
        campaigns.extend(await self.fetch_reward_campaigns())
        campaigns.sort(key=lambda c: c.active, reverse=True)
        campaigns.sort(key=lambda c: c.upcoming and c.starts_at or c.ends_at)
        campaigns.sort(key=lambda c: c.eligible, reverse=True)

        self._twitch._drops.clear()
        self._twitch._campaigns.clear()
        self._twitch.gui.inv.clear()
        self._twitch.inventory.clear()
        self._twitch._mnt_triggers.clear()
        switch_triggers: set[datetime] = set()
        next_hour = datetime.now(timezone.utc) + timedelta(hours=1)

        # add the campaigns to the internal inventory
        for campaign in campaigns:
            self._twitch._drops.update({drop.id: drop for drop in campaign.drops})
            if campaign.can_earn_within(next_hour):
                switch_triggers.update(campaign.time_triggers)
            self._twitch.inventory.append(campaign)
            self._twitch._campaigns[campaign.id] = campaign

        # concurrently add the campaigns into the GUI
        # NOTE: this fetches pictures from the CDN, so might be slow without a cache
        status_update(
            _.t["gui"]["status"]["adding_campaigns"].format(counter=f"(0/{len(campaigns)})")
        )
        add_campaign_tasks: list[asyncio.Task[None]] = [
            asyncio.create_task(self._twitch.gui.inv.add_campaign(campaign))
            for campaign in campaigns
        ]

        try:
            for i, coro in enumerate(asyncio.as_completed(add_campaign_tasks), start=1):
                await coro
                status_update(
                    _.t["gui"]["status"]["adding_campaigns"].format(
                        counter=f"({i}/{len(campaigns)})"
                    )
                )
                # this is needed here explicitly, because cache reads from disk don't raise this
                from src.config import State

                if self._twitch._state == State.EXIT:
                    raise ExitRequest()
        except Exception:
            # asyncio.as_completed doesn't cancel tasks on errors
            for task in add_campaign_tasks:
                task.cancel()
            raise

        self._twitch._mnt_triggers.extend(sorted(switch_triggers))

        # trim out all triggers that we're already past
        now = datetime.now(timezone.utc)
        while self._twitch._mnt_triggers and self._twitch._mnt_triggers[0] <= now:
            self._twitch._mnt_triggers.popleft()

        # Announced only now, so the GUI awaits above cannot interleave with the rebuild.
        for campaign in campaigns:
            if isinstance(campaign, RewardCampaign):
                campaign.queue_new_grants()
        await self._reward_ledger.announce_pending()

        # NOTE: maintenance task is restarted by fetch_inventory's finally block,
        # regardless of whether this method finishes cleanly or raises.

    def get_active_campaign(
        self, channel: Channel | None = None, *, exclude_rewards: bool = False
    ) -> DropsCampaign | None:
        """
        Determine the active campaign for a given channel (or watching channel).

        Returns the campaign with the least remaining minutes that can be earned
        on the specified channel. This is used to determine which drop is actively
        being progressed.

        Args:
            channel: The channel to check (defaults to watching channel)
            exclude_rewards: Skip Open Reward Campaigns, whose progress is polled separately

        Returns:
            The active DropsCampaign, or None if no campaign can be earned
        """
        if not self._twitch.wanted_games:
            return None

        watching_channel = self._twitch.watching_channel.get_with_default(channel)
        if watching_channel is None:
            # if we aren't watching anything, we can't earn any drops
            return None

        campaigns: list[DropsCampaign] = []
        for campaign in self._twitch.inventory:
            if campaign.can_earn(watching_channel) and not (
                exclude_rewards and isinstance(campaign, RewardCampaign)
            ):
                campaigns.append(campaign)

        if campaigns:
            priority = RewardPriority(self._twitch.settings)
            if priority.enabled:
                campaigns.sort(key=lambda c: (*priority.campaign_key(c), c.remaining_minutes))
            else:
                campaigns.sort(key=lambda c: c.remaining_minutes)
            return campaigns[0]

        return None
