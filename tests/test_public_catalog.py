import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from src.exceptions import GQLException
from src.services.inventory_service import InventoryService
from src.services.public_catalog import fetch_public_campaigns, parse_catalog


NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def _rec(cid, status="ACTIVE"):
    return {
        "id": cid, "status": status, "name": f"camp {cid}",
        "game": {"id": "66170", "displayName": "Warframe"},
        "startAt": "2026-10-05T10:00:00Z", "endAt": "2026-10-06T10:00:00Z",
        "allow": {"isEnabled": True, "channels": []},
        "timeBasedDrops": [{
            "id": f"d{cid}", "name": "drop", "startAt": "2026-10-05T10:00:00Z",
            "endAt": "2026-10-06T10:00:00Z", "requiredMinutesWatched": 30,
            "preconditionDrops": None, "benefitEdges": [],
        }],
    }


def _payload(updated="2026-10-05T11:59:00Z"):
    return {
        "lastUpdatedAt": updated,
        "data": [
            {"rewards": [_rec("a"), _rec("b", "EXPIRED")]},
            {"rewards": [_rec("c", "UPCOMING"), _rec("a")]},
            {},
        ],
    }


class TestParseCatalog(unittest.TestCase):
    def test_keeps_active_and_upcoming_deduped(self):
        ids = [c["id"] for c in parse_catalog(_payload(), NOW)]
        self.assertEqual(sorted(ids), ["a", "c"])

    def test_stale_feed_rejected(self):
        self.assertEqual(parse_catalog(_payload("2026-10-05T10:00:00Z"), NOW), [])

    def test_garbage_rejected(self):
        self.assertEqual(parse_catalog({"nope": 1}, NOW), [])
        self.assertEqual(parse_catalog(None, NOW), [])


class TestFetch(unittest.IsolatedAsyncioTestCase):
    async def test_network_error_returns_empty(self):
        session = MagicMock()
        session.get.side_effect = OSError("down")
        self.assertEqual(await fetch_public_campaigns(session), [])


class TestInventoryFallback(unittest.IsolatedAsyncioTestCase):
    def _twitch(self, campaigns_exc):
        twitch = MagicMock()
        twitch.gui.status.update = MagicMock()
        inv = {"data": {"currentUser": {"inventory": {"dropCampaignsInProgress": [], "gameEventDrops": []}}}}
        twitch.gql_request = AsyncMock(side_effect=[inv, campaigns_exc])
        twitch.get_session = AsyncMock(return_value=MagicMock())
        twitch._mnt_triggers = []
        twitch.gui.inv.add_campaign = AsyncMock()
        twitch._drops, twitch._campaigns = {}, {}
        return twitch

    async def test_integrity_failure_uses_public_feed(self):
        twitch = self._twitch(GQLException("IntegrityCheckFailed"))
        service = InventoryService(twitch)
        service.fetch_campaigns = AsyncMock(return_value={})
        with (
            patch("src.services.inventory_service.fetch_public_campaigns",
                  AsyncMock(return_value=[_rec("a")])),
            patch("src.services.inventory_service.discover_campaigns_via_browser",
                  AsyncMock(return_value=[])),
        ):
            await service._fetch_inventory()
        (chunk_arg,), _kw = service.fetch_campaigns.call_args
        self.assertEqual([k for k, _v in chunk_arg], ["a"])

    async def test_other_gql_error_still_raises(self):
        service = InventoryService(self._twitch(GQLException("service error")))
        with self.assertRaises(GQLException):
            await service._fetch_inventory()


class TestHardening(unittest.TestCase):
    def test_bad_ids_dropped(self):
        p = {"lastUpdatedAt": "2026-10-05T11:59:00Z",
             "data": [{"rewards": [{**_rec("z"), "id": 5}, _rec("x" * 500),
                                   _rec("ok"), {"id": "bad", "status": "ACTIVE"}]}]}
        self.assertEqual([c["id"] for c in parse_catalog(p, NOW)], ["ok"])


class _FakeContent:
    def __init__(self, data):
        self._data = data

    async def iter_chunked(self, n):
        for i in range(0, len(self._data), 7):  # tiny chunks, like a real stream
            yield self._data[i:i + 7]


class _FakeResp:
    status = 200

    def __init__(self, data):
        self.content = _FakeContent(data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class TestChunkedBody(unittest.IsolatedAsyncioTestCase):
    async def test_body_split_over_many_chunks_is_read_completely(self):
        import json
        from datetime import datetime, timezone

        payload = _payload(datetime.now(timezone.utc).isoformat())
        session = MagicMock()
        session.get.return_value = _FakeResp(json.dumps(payload).encode())
        ids = [c["id"] for c in await fetch_public_campaigns(session)]
        self.assertEqual(sorted(ids), ["a", "c"])


class TestNullCampaignDetails(unittest.IsolatedAsyncioTestCase):
    async def test_null_dropcampaign_is_skipped_not_fatal(self):
        twitch = MagicMock()
        twitch.get_auth = AsyncMock(return_value=MagicMock(user_id=1))
        full = {"id": "a", "game": {"id": "g"}}
        twitch.gql_request = AsyncMock(return_value=[
            {"data": {"user": {"dropCampaign": full}}},
            {"data": {"user": {"dropCampaign": None}}},
            {"data": {"user": None}},
        ])
        service = InventoryService(twitch)
        out = await service.fetch_campaigns([("a", {"id": "a", "status": "ACTIVE"}),
                                             ("b", {"id": "b", "status": "ACTIVE"}),
                                             ("c", {"id": "c", "status": "ACTIVE"})])
        self.assertEqual(list(out), ["a"])


class TestFeedBecomesCampaign(unittest.IsolatedAsyncioTestCase):
    async def test_feed_record_builds_a_drops_campaign(self):
        twitch = MagicMock()
        twitch.gui.status.update = MagicMock()
        twitch._client_type = object()
        twitch.settings.proxy = ""
        inv = {"data": {"currentUser": {"inventory": {"dropCampaignsInProgress": [], "gameEventDrops": []}}}}
        twitch.gql_request = AsyncMock(side_effect=[inv, GQLException("IntegrityCheckFailed")])
        twitch.get_session = AsyncMock(return_value=MagicMock())
        twitch._mnt_triggers = []
        twitch.gui.inv.add_campaign = AsyncMock()
        twitch._drops, twitch._campaigns = {}, {}
        service = InventoryService(twitch)
        service.fetch_campaigns = AsyncMock(return_value={})  # details come back null
        with (
            patch("src.services.inventory_service.fetch_public_campaigns", AsyncMock(return_value=[_rec("a")])),
            patch("src.services.inventory_service.discover_campaigns_via_browser", AsyncMock(return_value=[])),
        ):
            await service._fetch_inventory()
        self.assertEqual([c.id for c in twitch._campaigns.values()] or list(twitch._campaigns), ["a"])


class TestAllowNormalised(unittest.TestCase):
    def test_missing_channels_key_is_filled(self):
        from src.services.public_catalog import campaign_from_feed

        rec = _rec("a")
        rec["allow"] = {"isEnabled": True}
        self.assertEqual(campaign_from_feed(rec)["allow"]["channels"], [])
        rec.pop("allow")
        self.assertEqual(campaign_from_feed(rec)["allow"]["channels"], [])


class TestLinkStates(unittest.TestCase):
    def test_link_state_comes_from_reward_history_not_a_guess(self):
        from src.services.public_catalog import campaign_from_feed, link_states

        history = [
            {"requiredAccountLink": "https://link.smite2.com/", "isConnected": True},
            {"requiredAccountLink": "https://other.example/link", "isConnected": False},
            {"requiredAccountLink": None, "isConnected": True},
        ]
        states = link_states(history)
        linked, unlinked, unknown = _rec("a"), _rec("b"), _rec("c")
        linked["accountLinkURL"] = "https://link.smite2.com"
        unlinked["accountLinkURL"] = "https://other.example/link/"
        unknown["accountLinkURL"] = "https://never-seen.example/"
        self.assertTrue(campaign_from_feed(linked, states)["self"]["isAccountConnected"])
        self.assertFalse(campaign_from_feed(unlinked, states)["self"]["isAccountConnected"])
        self.assertFalse(campaign_from_feed(unknown, states)["self"]["isAccountConnected"])


class TestFeedIsUntrusted(unittest.TestCase):
    def _parse(self, rec):
        p = {"lastUpdatedAt": "2026-10-05T11:59:00Z", "data": [{"rewards": [rec]}]}
        return parse_catalog(p, NOW)

    def test_script_urls_are_dropped_not_passed_to_the_dashboard(self):
        rec = _rec("a")
        rec["accountLinkURL"] = "javascript:alert(1)"
        rec["game"]["boxArtURL"] = "data:text/html,x"
        rec["timeBasedDrops"][0]["benefitEdges"] = [
            {"benefit": {"id": "b", "name": "n", "distributionType": "BADGE",
                         "imageAssetURL": "javascript:alert(1)"}}]
        out = self._parse(rec)[0]
        self.assertEqual(out["accountLinkURL"], "")
        self.assertEqual(out["game"]["boxArtURL"], "")
        self.assertEqual(out["timeBasedDrops"][0]["benefitEdges"][0]["benefit"]["imageAssetURL"], "")

    def test_forged_account_state_is_stripped(self):
        rec = _rec("a")
        rec["self"] = {"isAccountConnected": True}
        rec["timeBasedDrops"][0]["self"] = {"isClaimed": True, "dropInstanceID": "x", "currentMinutesWatched": 9999}
        out = self._parse(rec)[0]
        self.assertNotIn("self", out)
        self.assertNotIn("self", out["timeBasedDrops"][0])

    def test_bad_channel_name_or_oversize_rejects_the_record(self):
        rec = _rec("a")
        rec["allow"] = {"isEnabled": True, "channels": [{"id": "1", "name": "../evil?x=1"}]}
        self.assertEqual(self._parse(rec), [])
        big = _rec("b")
        big["timeBasedDrops"] = big["timeBasedDrops"] * 500
        self.assertEqual(self._parse(big), [])
        weird = _rec("c")
        weird["name"] = "x" * 5000
        self.assertEqual(self._parse(weird), [])

    def test_extra_keys_are_not_copied(self):
        rec = _rec("a")
        rec["__proto__"] = {"x": 1}
        rec["description"] = "<script>"
        self.assertNotIn("description", self._parse(rec)[0])


class TestUnknownLink(unittest.TestCase):
    def _campaign(self, allow, states, url="https://never-seen.example/"):
        from src.models.campaign import DropsCampaign
        from src.services.public_catalog import campaign_from_feed

        twitch = MagicMock()
        twitch.settings.allow_unknown_link = allow
        rec = _rec("a")
        rec["accountLinkURL"] = url
        return DropsCampaign(twitch, campaign_from_feed(rec, states), {})

    def test_unknown_is_not_eligible_by_default(self):
        c = self._campaign(False, {})
        self.assertTrue(c.link_unknown)
        self.assertFalse(c.eligible)

    def test_unknown_becomes_eligible_when_the_user_allows_it(self):
        c = self._campaign(True, {})
        self.assertTrue(c.eligible)
        self.assertTrue(c.link_assumed)

    def test_known_unlinked_stays_ineligible_even_when_allowed(self):
        c = self._campaign(True, {"never-seen.example": False})
        self.assertFalse(c.link_unknown)
        self.assertFalse(c.eligible)

    def test_known_linked_is_eligible(self):
        c = self._campaign(False, {"never-seen.example": True})
        self.assertTrue(c.eligible)
        self.assertFalse(c.link_assumed)
