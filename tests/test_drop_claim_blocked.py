import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from src.exceptions import GQLException
from src.models import drop as drop_module
from src.models.drop import TimedDrop
from src.services import drop_history

INTEGRITY = GQLException(
    "[{'message': 'failed integrity check', 'path': ['claimDropRewards'], "
    "'extensions': {'code': 'IntegrityCheckFailed'}}]"
)


def _make_drop(drop_id="d1", claim_id="inst-1", minutes=0):
    now = datetime.now(timezone.utc)
    twitch = MagicMock()
    twitch.settings.blacklisted_drop_ids = []
    twitch.settings.drop_name_blacklist = []
    twitch.settings.discord_webhook_drops = "https://discord.com/api/webhooks/x/y"
    twitch._message_handler_service._send_discord_webhook = AsyncMock()
    campaign = MagicMock()
    campaign._twitch = twitch
    campaign.ignored = False
    campaign.ends_at = now + timedelta(days=1)
    campaign.game.name = "Some Game"
    data = {
        "id": drop_id,
        "name": "Some Drop",
        "benefitEdges": [],
        "startAt": (now - timedelta(days=1)).isoformat(),
        "endAt": (now + timedelta(days=1)).isoformat(),
        "preconditionDrops": None,
        "requiredMinutesWatched": 10,
        "self": {"dropInstanceID": claim_id, "isClaimed": False, "currentMinutesWatched": minutes},
    }
    return TimedDrop(campaign, data, {}), twitch


class TestClaimBlocked(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        drop_history._manual_claim_notified.clear()
        drop_module._blocked_claims.clear()

    async def test_integrity_failure_blocks_rejects_token_and_notifies_once(self):
        drop, twitch = _make_drop()
        twitch.gql_request = AsyncMock(side_effect=INTEGRITY)
        self.assertFalse(await drop.claim())
        self.assertTrue(drop.claim_blocked)
        twitch._auth_state.reject_integrity_token.assert_called_once()
        await asyncio.sleep(0)
        twitch._message_handler_service._send_discord_webhook.assert_called_once()
        embed = twitch._message_handler_service._send_discord_webhook.call_args.args[1]["embeds"][0]
        self.assertEqual(embed["title"], "⚠️ Drop needs manual claim")

    async def test_blocked_drop_is_throttled_then_retried_and_cleared(self):
        drop, twitch = _make_drop()
        twitch.gql_request = AsyncMock(side_effect=INTEGRITY)
        await drop.claim()
        await drop.claim()
        self.assertEqual(twitch.gql_request.await_count, 1)  # throttled
        drop_module._blocked_claims[drop.id] -= drop_module.BLOCKED_CLAIM_RETRY_SECONDS + 1
        twitch.gql_request = AsyncMock(
            return_value={"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}}
        )
        self.assertTrue(await drop.claim())
        self.assertFalse(drop.claim_blocked)
        await asyncio.sleep(0)
        # a rebuilt drop object (next inventory fetch) keeps the throttle
        rebuilt, twitch3 = _make_drop(drop_id="d3")
        twitch3.gql_request = AsyncMock(side_effect=INTEGRITY)
        await rebuilt.claim()
        again, twitch4 = _make_drop(drop_id="d3")
        twitch4.gql_request = AsyncMock(side_effect=INTEGRITY)
        self.assertTrue(again.claim_blocked)
        await again.claim()
        twitch4.gql_request.assert_not_awaited()
        # retry failing again must not send a second webhook
        drop2, twitch2 = _make_drop(drop_id="d2")
        twitch2.gql_request = AsyncMock(side_effect=INTEGRITY)
        await drop2.claim()
        drop_module._blocked_claims[drop2.id] -= drop_module.BLOCKED_CLAIM_RETRY_SECONDS + 1
        await drop2.claim()
        await asyncio.sleep(0)
        self.assertEqual(twitch2.gql_request.await_count, 2)
        twitch2._message_handler_service._send_discord_webhook.assert_called_once()

    async def test_other_errors_do_not_block(self):
        drop, twitch = _make_drop()
        twitch.gql_request = AsyncMock(side_effect=GQLException("service error"))
        self.assertFalse(await drop.claim())
        self.assertFalse(drop.claim_blocked)
        twitch._auth_state.reject_integrity_token.assert_not_called()


class TestImmediateClaimWithPresetClaimId(unittest.IsolatedAsyncioTestCase):
    async def test_spawns_one_task_even_though_claim_id_is_preset(self):
        drop, twitch = _make_drop(claim_id="inst-1", minutes=10)
        drop.claim = AsyncMock(return_value=False)
        with patch("src.models.drop.asyncio.create_task", wraps=asyncio.create_task) as spawn:
            drop._try_immediate_claim()
            drop._try_immediate_claim()
            self.assertEqual(spawn.call_count, 1)
        await asyncio.sleep(0)
        drop.claim.assert_awaited_once()
        self.assertEqual(drop.claim_id, "inst-1")  # real id kept, not replaced by a synthetic one


class TestPortResolution(unittest.TestCase):
    def test_log_file_name_honors_tdm_port_then_port(self):
        from src.__main__ import _log_file_name

        def name(env):
            with patch.dict(os.environ, env, clear=True):
                return _log_file_name()

        self.assertEqual(name({}), "TDM.log")
        self.assertEqual(name({"PORT": "8080"}), "TDM.log")
        self.assertEqual(name({"PORT": "9090"}), "TDM-9090.log")
        self.assertEqual(name({"TDM_PORT": "8082", "PORT": "9090"}), "TDM-8082.log")


class TestFinalizeManualClaim(unittest.TestCase):
    def setUp(self):
        drop_module._blocked_claims.clear()
        self.addCleanup(drop_module._blocked_claims.clear)

    def _run(self, is_claimed, blocked):
        drop, twitch = _make_drop()
        drop.is_claimed = is_claimed
        twitch._claim_finalized_drops = set()
        if blocked:
            drop_module._blocked_claims[drop.id] = 1.0
        with patch("src.services.drop_history.save_drop_claim") as save, \
                patch("src.services.drop_history._send_drop_webhook"):
            result = drop_history.finalize_manual_claim(twitch, drop.campaign, drop)
        return drop, result, save

    def test_blocked_drop_now_claimed_is_recorded_and_unblocked(self):
        drop, result, save = self._run(is_claimed=True, blocked=True)
        self.assertTrue(result)
        save.assert_called_once()
        self.assertNotIn(drop.id, drop_module._blocked_claims)

    def test_still_unclaimed_stays_blocked(self):
        drop, result, save = self._run(is_claimed=False, blocked=True)
        self.assertFalse(result)
        save.assert_not_called()
        self.assertIn(drop.id, drop_module._blocked_claims)

    def test_claimed_but_never_blocked_is_ignored(self):
        _, result, save = self._run(is_claimed=True, blocked=False)
        self.assertFalse(result)
        save.assert_not_called()


class TestClaimAnnouncedOnce(unittest.IsolatedAsyncioTestCase):
    """The inventory refetch right after a claim can still list the drop as
    unclaimed; claiming it again logged a second "Drop claimed" line."""

    def setUp(self):
        drop_module._blocked_claims.clear()

    async def test_finalized_drop_is_not_claimed_or_announced_again(self):
        drop, twitch = _make_drop()
        twitch._claim_finalized_drops = set()
        twitch.gql_request = AsyncMock(
            return_value={"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}}
        )
        self.assertTrue(await drop.claim())
        twitch._claim_finalized_drops.add(drop.id)

        refetched, _ = _make_drop()
        refetched._twitch = twitch
        self.assertTrue(await refetched.claim())
        self.assertTrue(refetched.is_claimed)
        self.assertEqual(twitch.gql_request.await_count, 1)
        self.assertEqual(twitch.print.call_count, 1)
