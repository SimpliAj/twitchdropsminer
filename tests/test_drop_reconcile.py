import unittest
from unittest.mock import AsyncMock, MagicMock

from src.exceptions import GQLException
from src.services.watch_service import WatchService


def _setup(reply, remaining=5):
    channel = MagicMock(id=7)
    channel._watch_current.return_value = True
    campaign = MagicMock(progress_revision=0)
    drop = MagicMock(id="d1", campaign=campaign, remaining_minutes=remaining)
    drop.can_earn.return_value = True
    twitch = MagicMock()
    twitch._drops = {"d1": drop}
    twitch.gui.progress._current_drop = drop
    twitch.gql_request = AsyncMock(side_effect=reply if isinstance(reply, Exception) else None,
                                   return_value=None if isinstance(reply, Exception) else reply)
    return WatchService(twitch), twitch, channel, drop, campaign


def _reply(minutes=42):
    return {"data": {"currentUser": {"dropCurrentSession": {"dropID": "d1", "currentMinutesWatched": minutes}}}}


class TestReconcile(unittest.IsolatedAsyncioTestCase):
    async def test_applies_reply(self):
        svc, _t, ch, drop, _c = _setup(_reply())
        self.assertTrue(await svc.reconcile_drop_progress(ch, object()))
        drop.update_minutes.assert_called_once_with(42)

    async def test_skips_when_no_time_remaining(self):
        svc, t, ch, drop, _c = _setup(_reply(), remaining=0)
        self.assertFalse(await svc.reconcile_drop_progress(ch, object()))
        t.gql_request.assert_not_called()

    async def test_stale_reply_dropped_after_newer_observation(self):
        svc, t, ch, drop, campaign = _setup(_reply())

        async def slow(*_a, **_k):
            campaign.progress_revision += 1  # websocket update arrived meanwhile
            return _reply()

        t.gql_request = slow
        self.assertFalse(await svc.reconcile_drop_progress(ch, object()))
        drop.update_minutes.assert_not_called()

    async def test_replaced_drop_object_dropped(self):
        svc, t, ch, drop, _c = _setup(_reply())

        async def slow(*_a, **_k):
            t._drops = {"d1": MagicMock(campaign=MagicMock(progress_revision=0))}
            return _reply()

        t.gql_request = slow
        self.assertFalse(await svc.reconcile_drop_progress(ch, object()))

    async def test_empty_and_failed_replies_change_nothing(self):
        for reply in ({"data": {"currentUser": None}},
                      {"data": {"currentUser": {"dropCurrentSession": None}}},
                      GQLException("boom")):
            svc, _t, ch, drop, _c = _setup(reply)
            self.assertFalse(await svc.reconcile_drop_progress(ch, object()))
            drop.update_minutes.assert_not_called()
            drop.campaign.bump_minutes.assert_not_called()
