"""Open Reward Campaigns where this fork differs from upstream: claims, history, cache, UI data."""

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.gql_client import GQLClient
from src.config import GQL_OPERATIONS
from src.exceptions import GQLException, RequestException
from src.models import RewardCampaign
from src.services import drop_history
from src.services.inventory_service import InventoryService
from src.web.managers.inventory import InventoryManager
from tests.test_reward_campaigns import (
    _build,
    _channel,
    _data,
    _drop,
    _gql,
    _iso,
    _progress_response,
    _twitch,
    CLAIM_ID,
    GROUP_ID,
)


CLAIM = GQL_OPERATIONS["ClaimDrop"].with_variables({"input": {"dropInstanceID": CLAIM_ID}})
CLAIM.single_attempt = True
SUCCESS = {"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}}


class _Reply:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class _Context:
    def __init__(self, result):
        self._result = result

    async def __aenter__(self):
        if isinstance(self._result, Exception):
            raise self._result
        return _Reply(self._result)

    async def __aexit__(self, *args):
        return False


def _gql_client(*results):
    http = MagicMock()
    http.request = MagicMock(side_effect=[_Context(r) for r in results])
    validated = MagicMock()
    validated.headers.return_value = {}
    auth = MagicMock()
    auth.validate = AsyncMock(return_value=validated)
    return GQLClient(http, auth, MagicMock(USER_AGENT="agent")), http


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    {"errors": [{"message": "service timeout"}]},
    {"data": {}, "error": "x", "message": "y"},
    ["not a dict"],
    RequestException("Unconfirmed HTTP response"),
])
async def test_single_attempt_mutation_is_sent_once(failure):
    client, http = _gql_client(failure, SUCCESS)
    with pytest.raises((GQLException, RequestException)):
        await client.request(CLAIM)
    assert http.request.call_count == 1
    assert http.request.call_args.kwargs["single_attempt"] is True


@pytest.mark.asyncio
async def test_single_attempt_flag_is_not_part_of_the_json_body():
    client, http = _gql_client(SUCCESS)
    assert await client.request(CLAIM) == SUCCESS
    assert "single_attempt" not in http.request.call_args.kwargs["json"]
    assert GQL_OPERATIONS["ClaimDrop"].single_attempt is False


@pytest.mark.asyncio
async def test_regular_requests_still_retry_service_timeouts(monkeypatch):
    monkeypatch.setattr("src.api.gql_client.asyncio.sleep", AsyncMock())
    client, http = _gql_client({"errors": [{"message": "service timeout"}]}, SUCCESS)
    assert await client.request(GQL_OPERATIONS["Inventory"]) == SUCCESS
    assert http.request.call_count == 2


@pytest.mark.asyncio
async def test_reward_claim_goes_out_once_and_is_not_repeated():
    twitch = _twitch()
    drop = _drop(_build({"status": "CLAIMABLE", "currentMinutesWatched": 60}, twitch=twitch))
    twitch.gql_request = AsyncMock(side_effect=RequestException("Unconfirmed HTTP response"))
    assert not await drop.claim()
    assert not await drop.claim()
    assert twitch.gql_request.await_count == 1
    assert twitch.gql_request.await_args.args[0].single_attempt is True


@pytest.mark.asyncio
async def test_a_daily_reward_claim_is_not_short_circuited_by_the_finalized_set():
    twitch = _twitch()
    drop = _drop(_build({"status": "CLAIMABLE", "currentMinutesWatched": 60}, twitch=twitch))
    twitch._claim_finalized_drops.add(drop.id)  # yesterday's claim of the same drop ID
    twitch.gql_request = AsyncMock(return_value=SUCCESS)
    assert await drop.claim() is True
    assert twitch.gql_request.await_count == 1
    twitch.print.assert_not_called()  # grants are announced from grantCount instead


def test_finalize_drop_claim_leaves_reward_grants_to_the_ledger(monkeypatch):
    saved = MagicMock()
    monkeypatch.setattr(drop_history, "save_drop_claim", saved)
    twitch = _twitch()
    drop_history.finalize_drop_claim(twitch, MagicMock(), _drop(_build({"status": "CLAIMABLE"}, twitch=twitch)))
    saved.assert_not_called()
    assert not twitch._claim_finalized_drops


def test_repeating_goal_does_not_reuse_the_remembered_minutes(monkeypatch):
    monkeypatch.setattr("src.models.drop.drop_minutes_cache.get", lambda drop_id, api: 60)
    update = MagicMock()
    monkeypatch.setattr("src.models.drop.drop_minutes_cache.update", update)
    twitch = _twitch()
    drop = _drop(_build({"status": "IN_PROGRESS", "currentMinutesWatched": 3}, twitch=twitch))
    assert drop.real_current_minutes == 3
    drop.update_minutes(10)
    update.assert_not_called()


def test_badge_reward_at_the_goal_is_claimed_not_assumed_granted():
    twitch = _twitch()
    campaign = RewardCampaign.from_reward_campaign(
        twitch, _data(), {GROUP_ID: {"status": "CLAIMABLE", "currentMinutesWatched": 60}},
        "uid", None, None, {"r1"},
    )
    drop = _drop(campaign)
    assert not drop.is_claimed and drop.can_claim


def test_reward_names_are_matched_by_the_drop_name_blacklist():
    twitch = _twitch(drop_name_blacklist=["dvd chest"])
    drop = _drop(_build(twitch=twitch))  # the event title is "GTAVI Launch Celebration"
    assert drop.excluded


def test_grants_are_saved_as_numbered_history_entries(monkeypatch):
    saved = MagicMock(return_value=True)
    monkeypatch.setattr(drop_history, "save_drop_claim", saved)
    campaign = _build()
    drop = _drop(campaign)
    assert drop.record_grant(3) is True
    saved.assert_called_once_with(
        "Grand Theft Auto VI", "GTAVI Launch Celebration #3", "GTA DVD Chest", None,
    )


def test_history_file_gets_one_entry_per_grant(tmp_path, monkeypatch):
    monkeypatch.setattr(drop_history, "_account_data_dir", lambda: tmp_path)
    campaign = _build()
    drop = _drop(campaign)
    assert drop.record_grant(1) and drop.record_grant(2)
    history = json.loads((tmp_path / "drops_history.json").read_text())
    assert [e["drop"] for e in history] == ["GTAVI Launch Celebration #2", "GTAVI Launch Celebration #1"]
    assert history[0]["image_url"] is None
    assert json.loads((tmp_path / "drops_total.json").read_text())["count"] == 2


@pytest.mark.asyncio
async def test_grant_announcement_prints_and_sends_the_claim_webhook_without_artwork():
    twitch = _twitch(discord_webhook_drops="https://discord.com/api/webhooks/x/y")
    twitch._message_handler_service = SimpleNamespace(_send_discord_webhook=AsyncMock())
    campaign = _build()
    campaign._twitch = twitch
    drop = _drop(campaign)
    drop._twitch = twitch
    await drop.announce_grant(2)
    assert "#2" in twitch.print.call_args.args[0]
    import asyncio

    await asyncio.sleep(0)
    embed = twitch._message_handler_service._send_discord_webhook.await_args.args[1]["embeds"][0]
    assert embed["title"] == "🎁 Drop Claimed!"
    assert "thumbnail" not in embed
    assert {"name": "Drop", "value": "GTAVI Launch Celebration #2", "inline": True} in embed["fields"]


@pytest.mark.asyncio
async def test_integrity_failure_is_contained_and_rejects_the_token(caplog):
    twitch = _twitch()
    twitch._auth_state.reject_integrity_token = MagicMock()
    twitch.gql_request = AsyncMock(side_effect=GQLException([{"message": "IntegrityCheckFailed"}]))
    with caplog.at_level("WARNING", logger="TwitchDrops"):
        assert await InventoryService(twitch).fetch_reward_campaigns() == []
    twitch._auth_state.reject_integrity_token.assert_called_once()
    assert "integrity check" in caplog.text


@pytest.mark.asyncio
async def test_info_log_names_campaign_goal_progress_and_next_window(caplog):
    twitch = _twitch()
    reading = {"status": "CLAIMED", "grantCount": 2, "nextEligibleAt": _iso(timedelta(hours=5))}
    twitch.gql_request = _gql(_progress_response(reading))
    with caplog.at_level("INFO", logger="TwitchDrops"):
        [campaign] = await InventoryService(twitch).fetch_reward_campaigns()
    assert campaign.reward_grants == 2
    line = next(r.getMessage() for r in caplog.records if "Open Reward Campaign" in r.getMessage())
    assert "GTAVI Launch Celebration" in line and "goal 60 min" in line
    assert "earned 2 times" in line and "next window" in line


@pytest.mark.asyncio
async def test_another_account_does_not_inherit_the_ledger():
    twitch = _twitch()
    twitch.gql_request = _gql(_progress_response({"status": "IN_PROGRESS", "currentMinutesWatched": 5}))
    service = InventoryService(twitch)
    await service.fetch_reward_campaigns()
    service._reward_ledger.announced["stale"] = 9
    await service.fetch_reward_campaigns()
    assert service._reward_ledger.announced["stale"] == 9
    twitch.get_auth = AsyncMock(return_value=SimpleNamespace(user_id="other"))
    await service.fetch_reward_campaigns()
    assert "stale" not in service._reward_ledger.announced


@pytest.mark.asyncio
async def test_inventory_data_keeps_rewards_without_artwork_and_shows_times_earned():
    broadcaster = MagicMock(emit=AsyncMock())
    manager = InventoryManager(broadcaster, MagicMock())
    campaign = _build({"status": "IN_PROGRESS", "currentMinutesWatched": 5, "grantCount": 4})
    campaign.reward_grants = 4
    await manager.add_campaign(campaign)
    [drop] = manager.get_campaigns()[0]["drops"]
    assert drop["benefits"] == [{"name": "GTA DVD Chest", "type": "UNKNOWN", "image_url": None}]
    assert drop["reward_grants"] == 4


@pytest.mark.asyncio
async def test_regular_drops_have_no_times_earned():
    from tests.test_reward_campaigns import _regular

    manager = InventoryManager(MagicMock(emit=AsyncMock()), MagicMock())
    await manager.add_campaign(_regular(_twitch()))
    assert manager.get_campaigns()[0]["drops"][0]["reward_grants"] is None


def test_wanted_games_list_includes_the_reward_game_only_when_its_type_is_enabled():
    from src.services.stream_selector import StreamSelector
    from tests.test_reward_campaigns import _settings

    twitch = _twitch()
    campaign = _build(twitch=twitch)
    on = _settings("Grand Theft Auto VI", mining_benefits={"UNKNOWN": True})
    off = _settings("Grand Theft Auto VI", mining_benefits={"UNKNOWN": False})
    assert [g.name for g in StreamSelector().get_wanted_games(on, [campaign])] == ["Grand Theft Auto VI"]
    assert StreamSelector().get_wanted_games(off, [campaign]) == []


def test_channel_without_the_directory_flag_is_watchable_once_the_inventory_decides():
    from src.services.watch_service import WatchService

    twitch = _twitch()
    campaign = _build(twitch=twitch)
    twitch.inventory, twitch.wanted_games = [campaign], [campaign.game]
    channel = _channel(twitch)
    assert channel._has_earnable_drops() is True
    channel._stream.drops_enabled = channel._has_earnable_drops()
    assert WatchService(twitch).can_watch(channel)
