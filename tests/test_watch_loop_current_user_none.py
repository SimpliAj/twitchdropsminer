import asyncio
import contextlib
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.services.watch_service import WatchService


class _FakeChannel:
    def __init__(self):
        self.online = True
        self.id = 111
        self.name = "some_channel"
        self._login = "some_channel"
        self._stream = None

    async def send_watch(self):
        return True


class TestWatchLoopCurrentUserNone(unittest.IsolatedAsyncioTestCase):
    """
    Regression test for the crash report:

        File "src/services/watch_service.py", line 311, in watch_loop
            drop_data: JsonType | None = context["data"]["currentUser"]["dropCurrentSession"]
        TypeError: 'NoneType' object is not subscriptable

    Twitch's GQL backend can answer the "CurrentDrop" query with a "server
    error" GQL error scoped to the whole "currentUser" field while still
    returning HTTP 200 (no exception raised by GQLClient - see
    gql_client.py's "server error" path-nullification). watch_loop must fall
    back to the existing "active campaign" heuristic instead of crashing on
    the None subscript - a crash here is a @task_wrapper(critical=True) task,
    which tears down the *entire* application (looking like a clean exit,
    since it goes through Twitch.close() -> State.EXIT -> exit_status=0),
    not just this one watch cycle.
    """

    async def test_current_user_none_falls_back_to_active_campaign_without_crashing(self):
        channel = _FakeChannel()

        twitch = MagicMock()
        twitch.watching_channel.get = AsyncMock(return_value=channel)
        twitch.gui.progress.minute_almost_done.return_value = True
        # This is the exact response shape that crashed: a "server error" GQL
        # error nullified the whole "currentUser" object, not just the
        # "dropCurrentSession" field underneath it.
        twitch.gql_request = AsyncMock(return_value={"data": {"currentUser": None}})
        twitch.settings.claim_channel_points = False
        twitch._idle_channels_set = set()
        twitch._watching_restart = asyncio.Event()

        active_campaign = MagicMock()
        active_campaign.game = "Some Game"
        active_campaign.first_drop = None
        twitch._inventory_service.get_active_campaign.return_value = active_campaign

        service = WatchService(twitch)

        # Collapse the loop's internal 20s progress-wait to instant, while
        # still using the real asyncio.sleep(0) under the hood so the event
        # loop keeps actually yielding between the test and the task below
        # (a bare AsyncMock() replacement doesn't yield the same way and
        # starves the task of scheduling turns).
        real_sleep = asyncio.sleep

        async def _fast_sleep(_delay, *_a, **_kw):
            return await real_sleep(0)

        with patch("asyncio.sleep", new=_fast_sleep):
            task = asyncio.create_task(service.watch_loop())
            try:
                for _ in range(200):
                    if task.done():
                        # surfaces the TypeError instead of looping forever
                        task.result()
                        self.fail("watch_loop exited before reaching the fallback")
                    if active_campaign.bump_minutes.called:
                        break
                    await real_sleep(0)
                else:
                    self.fail("watch_loop never reached the active-campaign fallback")
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        # No TypeError was raised (the task would have re-raised it on await
        # above and failed the test) and the existing fallback ran instead.
        active_campaign.bump_minutes.assert_called_once_with(channel)


if __name__ == "__main__":
    unittest.main()
