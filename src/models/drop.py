from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from dateutil.parser import isoparse

from src.config.constants import MAX_EXTRA_MINUTES
from src.config.operations import GQL_OPERATIONS
from src.exceptions import GQLException
from src.i18n import _
from src.models.benefit import Benefit
from src.services import drop_minutes_cache
from src.services.drop_history import finalize_drop_claim, notify_manual_claim


if TYPE_CHECKING:
    from src.config.constants import JsonType
    from src.core.client import Twitch
    from src.models.campaign import DropsCampaign
    from src.models.channel import Channel


logger = logging.getLogger("TwitchDrops")
DIMS_PATTERN = re.compile(r"-\d+x\d+(?=\.(?:jpg|png|gif)$)", re.I)
INVENTORY_URL = "https://www.twitch.tv/drops/inventory"
BLOCKED_CLAIM_RETRY_SECONDS = 30 * 60
# drop id -> monotonic time of the last integrity-blocked claim; module-level because
# every inventory fetch rebuilds the drop objects, which would reset the throttle
_blocked_claims: dict[str, float] = {}


def remove_dimensions(url: str) -> str:
    """Remove dimension suffix from Twitch image URLs (e.g., -285x380.jpg)."""
    return DIMS_PATTERN.sub("", url)


class BaseDrop:
    def __init__(
        self, campaign: DropsCampaign, data: JsonType, claimed_benefits: dict[str, datetime]
    ):
        self._twitch: Twitch = campaign._twitch
        self.id: str = data["id"]
        self.name: str = data["name"]
        self.campaign: DropsCampaign = campaign
        self.benefits: list[Benefit] = [Benefit(b) for b in (data["benefitEdges"] or [])]
        self.starts_at: datetime = isoparse(data["startAt"])
        self.ends_at: datetime = isoparse(data["endAt"])
        self.claim_id: str | None = None
        self.is_claimed: bool = False
        if "self" in data:
            self.claim_id = data["self"]["dropInstanceID"]
            self.is_claimed = data["self"]["isClaimed"]
        elif (
            # If there's no self edge available, we can use claimed_benefits to determine
            # (with pretty good certainty) if this drop has been claimed or not.
            # To do this, we check if the benefitEdges appear in claimed_benefits, and then
            # deref their "lastAwardedAt" timestamps into a list to check against.
            # If the benefits were claimed while the drop was active,
            # the drop has been claimed too.
            (
                dts := [
                    claimed_benefits[bid]
                    for benefit in self.benefits
                    if (bid := benefit.id) in claimed_benefits
                ]
            )
            and all(self.starts_at <= dt < self.ends_at for dt in dts)
        ):
            self.is_claimed = True
        self.precondition_drops: list[str] = [d["id"] for d in (data["preconditionDrops"] or [])]
        self.required_subs: int = data.get("requiredSubs", 0) or 0

    def __repr__(self) -> str:
        if self.is_claimed:
            additional = ", claimed=True"
        elif self.can_earn():
            additional = ", can_earn=True"
        else:
            additional = ""
        return f"Drop({self.rewards_text()}{additional})"

    @property
    def claim_blocked(self) -> bool:
        # Twitch answered IntegrityCheckFailed to the claim: only the user can claim it
        return self.id in _blocked_claims

    @property
    def preconditions_met(self) -> bool:
        campaign = self.campaign
        return all(campaign.timed_drops[pid].is_claimed for pid in self.precondition_drops)

    def _on_state_changed(self) -> None:
        raise NotImplementedError

    @property
    def excluded(self) -> bool:
        # The user doesn't want this drop. Watching its game still earns it
        # minutes (Twitch credits every running campaign of that game), so it
        # is kept out of "the drop we're mining" and never auto-claimed --
        # unless a wanted drop needs it claimed first to unlock.
        settings = self._twitch.settings
        name = self.name.lower()
        unwanted = (
            self.campaign.ignored
            or self.id in getattr(settings, "blacklisted_drop_ids", [])
            or any(kw.strip() and kw.lower() in name for kw in getattr(settings, "drop_name_blacklist", []))
        )
        return unwanted and all(
            drop.excluded for drop in self.campaign.drops if self.id in drop.precondition_drops
        )

    def _base_earn_conditions(self) -> bool:
        # define when a drop can be earned or not
        return (
            self.preconditions_met  # preconditions are met
            and not self.is_claimed  # isn't already claimed
            # has at least one benefit, or participates in a preconditions chain
            and (bool(self.benefits) or self.id in self.campaign.preconditions_chain())
        )

    def _base_can_earn(self) -> bool:
        # cross-participates in can_earn and can_earn_within handling, where a timeframe is added
        return (
            self._base_earn_conditions()
            # is within the timeframe
            and self.starts_at <= datetime.now(timezone.utc) < self.ends_at
        )

    def _can_earn_within(self, stamp: datetime) -> bool:
        # NOTE: This does not check the campaign's eligibility or active status
        return (
            self._base_earn_conditions()
            and self.ends_at > datetime.now(timezone.utc)
            and self.starts_at < stamp
        )

    def can_earn(self, channel: Channel | None = None, ignore_channel_status: bool = False) -> bool:
        return self._base_can_earn() and self.campaign._base_can_earn(
            channel, ignore_channel_status
        )

    @property
    def can_claim(self) -> bool:
        # https://help.twitch.tv/s/article/mission-based-drops?language=en_US#claiming
        # "If you are unable to claim the Drop in time, you will be able to claim it
        # from the Drops Inventory page until 24 hours after the Drops campaign has ended."
        return (
            self.claim_id is not None
            and not self.is_claimed
            and datetime.now(timezone.utc) < self.campaign.ends_at + timedelta(hours=24)
        )

    def update_claim(self, claim_id: str) -> None:
        """Update the claim ID for this drop."""
        self.claim_id = claim_id

    async def generate_claim(self) -> None:
        # claim IDs now appear to be constructed from other IDs we have access to
        # Format: UserID#CampaignID#DropID
        # NOTE: This marks a drop as a ready-to-claim, so we may want to later ensure
        # its mining progress is finished first
        auth_state = await self.campaign._twitch.get_auth()
        self.claim_id = f"{auth_state.user_id}#{self.campaign.id}#{self.id}"

    def rewards_text(self, delim: str = ", ") -> str:
        return delim.join(benefit.name for benefit in self.benefits)

    def has_wanted_unclaimed_benefits(self, allowed_benefits: dict[str, bool]) -> bool:
        return len(self.get_wanted_unclaimed_benefits(allowed_benefits)) > 0

    def get_wanted_unclaimed_benefits(self, allowed_benefits: dict[str, bool]) -> list[str]:
        if self.is_claimed:
            return []
        return [benefit.name for benefit in self.benefits if benefit.is_wanted(allowed_benefits)]

    async def claim(self) -> bool:
        if self.excluded:
            logger.info(f"Not claiming blacklisted/ignored drop: {self.name} ({self.id})")
            return False
        failed_at = _blocked_claims.get(self.id)
        if failed_at is not None and time.monotonic() - failed_at < BLOCKED_CLAIM_RETRY_SECONDS:
            return False
        result = await self._claim()
        if result:
            self.is_claimed = result
            if _blocked_claims.pop(self.id, None) is not None:
                self._on_state_changed()
            claim_text = (
                f"{self.campaign.game.name}\n"
                f"{self.rewards_text()} "
                f"({self.campaign.claimed_drops}/{self.campaign.total_drops})"
            )
            # two different claim texts, becase a new line after the game name
            # looks ugly in the output window - replace it with a space
            self._twitch.print(
                _.t["status"]["claimed_drop"].format(drop=claim_text.replace("\n", " "))
            )
        elif not self.claim_blocked:
            logger.error(f"Drop claim has potentially failed! Drop ID: {self.id}")
        return result

    def _claim_failed(self, error: str) -> None:
        if "IntegrityCheckFailed" not in error:
            logger.warning(f"Claiming drop {self.name} ({self.id}) failed: {error}")
            return
        first = not self.claim_blocked
        _blocked_claims[self.id] = time.monotonic()
        # no-op without a cached token; otherwise a fresh one is minted for the next attempt
        self._twitch._auth_state.reject_integrity_token()
        if not first:
            logger.debug(f"Claiming drop {self.name} ({self.id}) is still integrity-blocked")
            return
        logger.warning(
            f"Drop {self.name} ({self.campaign.game.name}) needs a manual claim at {INVENTORY_URL} "
            "-- Twitch requires an integrity check this miner can't pass"
        )
        self._on_state_changed()
        notify_manual_claim(self._twitch, self.campaign, self)

    async def _claim(self) -> bool:
        """
        Returns True if the claim succeeded, False otherwise.
        """
        if self.is_claimed:
            return True
        if not self.can_claim:
            return False
        try:
            response = await self._twitch.gql_request(
                GQL_OPERATIONS["ClaimDrop"].with_variables(
                    {"input": {"dropInstanceID": self.claim_id}}
                )
            )
        except GQLException as exc:
            # regardless of the error, we have to assume
            # the claiming operation has potentially failed
            self._claim_failed(str(exc))
            return False
        data = response["data"]
        if "errors" in data and data["errors"]:
            self._claim_failed(str(data["errors"]))
            return False
        elif "claimDropRewards" in data:
            if not data["claimDropRewards"]:
                logger.warning(f"Claiming drop {self.name} ({self.id}) returned no result")
                return False
            status = data["claimDropRewards"]["status"]
            if status in ("ELIGIBLE_FOR_ALL", "DROP_INSTANCE_ALREADY_CLAIMED"):
                return True
            logger.warning(f"Claiming drop {self.name} ({self.id}) returned status {status}")
        return False


class TimedDrop(BaseDrop):
    def __init__(
        self, campaign: DropsCampaign, data: JsonType, claimed_benefits: dict[str, datetime]
    ):
        super().__init__(campaign, data, claimed_benefits)
        api_minutes: int = "self" in data and data["self"]["currentMinutesWatched"] or 0
        self.required_minutes: int = data["requiredMinutesWatched"]
        self.real_current_minutes: int = drop_minutes_cache.get(self.id, api_minutes)
        self.extra_current_minutes: int = 0
        self._immediate_claim_started: bool = False
        if self.is_claimed:
            # claimed drops may report inconsistent current minutes, so we need to overwrite them
            self.real_current_minutes = self.required_minutes
        elif (
            not self.is_claimed
            and self.real_current_minutes >= self.required_minutes > 0
            and self._is_auto_granted()
        ):
            # badges/emotes are auto-granted by Twitch — no explicit claim needed
            self.is_claimed = True
            self.real_current_minutes = self.required_minutes

    def __repr__(self) -> str:
        if self.is_claimed:
            additional = ", claimed=True"
        elif self.can_earn():
            additional = ", can_earn=True"
        else:
            additional = ""
        if 0 < self.current_minutes < self.required_minutes:
            minutes = f", {self.current_minutes}/{self.required_minutes}"
        else:
            minutes = ""
        return f"Drop({self.rewards_text()}{minutes}{additional})"

    @property
    def current_minutes(self) -> int:
        return self.real_current_minutes + self.extra_current_minutes

    @property
    def remaining_minutes(self) -> int:
        return self.required_minutes - self.current_minutes

    @property
    def total_required_minutes(self) -> int:
        return self.required_minutes + max(
            (
                self.campaign.timed_drops[pid].total_required_minutes
                for pid in self.precondition_drops
            ),
            default=0,
        )

    @property
    def total_remaining_minutes(self) -> int:
        return self.remaining_minutes + max(
            (
                self.campaign.timed_drops[pid].total_remaining_minutes
                for pid in self.precondition_drops
            ),
            default=0,
        )

    @property
    def progress(self) -> float:
        if self.current_minutes <= 0 or self.required_minutes <= 0:
            return 0.0
        elif self.current_minutes >= self.required_minutes:
            return 1.0
        return self.current_minutes / self.required_minutes

    @property
    def availability(self) -> float:
        import math

        now = datetime.now(timezone.utc)
        if self.required_minutes > 0 and self.total_remaining_minutes > 0 and now < self.ends_at:
            return ((self.ends_at - now).total_seconds() / 60) / self.total_remaining_minutes
        return math.inf

    def _base_earn_conditions(self) -> bool:
        return (
            super()._base_earn_conditions()
            and self.required_minutes > 0
            # Watching can't unlock a sub-gated tier — Twitch only grants these to
            # existing subscribers, so treat them as unearnable rather than mining
            # minutes into a claim that will never happen.
            and self.required_subs <= 0
            # Watch time is already maxed out — more watching won't do anything for
            # this drop, whether or not Twitch has flipped isClaimed yet. Without this,
            # a drop stuck waiting on server-side claim confirmation keeps getting
            # picked as the active earn target (first_drop/channel selection) forever.
            and self.current_minutes < self.required_minutes
            # NOTE: This may be a bad idea, as it invalidates the can_earn status
            # and provides no way to recover from this state until the next reload.
            and self.extra_current_minutes < MAX_EXTRA_MINUTES
        )

    def _on_state_changed(self) -> None:
        self._twitch.gui.inv.update_drop(self)

    def _is_auto_granted(self) -> bool:
        return bool(self.benefits) and all(b.type.is_badge_or_emote() for b in self.benefits)

    def _try_immediate_claim(self) -> None:
        """
        Claim as soon as we locally cross 100% progress, instead of waiting for the
        next scheduled inventory fetch (up to an hour away) to notice via GAMES_UPDATE.
        Without this, a drop that's actually done just sits there — still picked as
        `first_drop`/blocking its precondition chain since `is_claimed` never flips —
        while the watch loop keeps uselessly bumping it.
        """
        if self.is_claimed or self._immediate_claim_started or self.progress < 1.0:
            return
        self._immediate_claim_started = True

        async def _do_claim() -> None:
            if self.claim_id is None:
                await self.generate_claim()
            claimed = await self.claim()
            if claimed:
                finalize_drop_claim(self._twitch, self.campaign, self)

        asyncio.create_task(_do_claim())

    def _update_real_minutes(self, delta: int) -> None:
        if delta == 0 or self.real_current_minutes + delta < 0 or not self.can_earn():
            return
        if self.real_current_minutes + delta < self.required_minutes:
            self.real_current_minutes += delta
        else:
            self.real_current_minutes = self.required_minutes
            if self._is_auto_granted():
                self.is_claimed = True
        self.extra_current_minutes = 0
        drop_minutes_cache.update(self.id, self.real_current_minutes)
        self._on_state_changed()
        self._try_immediate_claim()

    def _bump_minutes(self, channel: Channel | None) -> bool:
        if self.can_earn(channel):
            self.extra_current_minutes += 1
            self._on_state_changed()
            self._try_immediate_claim()
            if self.extra_current_minutes >= MAX_EXTRA_MINUTES:
                return True
        return False

    async def claim(self) -> bool:
        result = await super().claim()
        if result:
            self.real_current_minutes = self.required_minutes
            self.extra_current_minutes = 0
        self._on_state_changed()
        return result

    def display(self, *, countdown: bool = True, subone: bool = False) -> None:
        """Display this drop in the GUI with progress information."""
        self._twitch.gui.display_drop(self, countdown=countdown, subone=subone)

    def update_minutes(self, new_minutes: int) -> None:
        """Update the current watched minutes for this drop."""
        delta: int = new_minutes - self.real_current_minutes
        if delta == 0:
            return
        elif self.real_current_minutes + delta < 0:
            delta = -self.real_current_minutes
        elif self.real_current_minutes + delta > self.required_minutes:
            delta = self.required_minutes - self.real_current_minutes
        self.campaign._update_real_minutes(delta)
