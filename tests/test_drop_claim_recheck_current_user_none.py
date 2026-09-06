import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.services.message_handlers import MessageHandlerService


class TestDropClaimRecheckCurrentUserNone(unittest.IsolatedAsyncioTestCase):
    """
    Same root cause as the watch_loop crash (see
    test_watch_loop_current_user_none.py): the "CurrentDrop" GQL query can
    come back with currentUser=None on a transient Twitch-side "server
    error" for that field. process_drops()'s post-claim re-check loop had
    the identical unguarded `context["data"]["currentUser"]["dropCurrentSession"]`
    access and would crash the same way.
    """

    async def test_current_user_none_breaks_recheck_loop_without_crashing(self):
        twitch = MagicMock()
        drop = MagicMock()
        drop.id = "drop-1"
        drop.update_claim = MagicMock()
        drop.claim = AsyncMock(return_value=True)
        drop.display = MagicMock()
        campaign = MagicMock()
        drop.campaign = campaign

        twitch._drops = {"drop-1": drop}
        watching_channel = MagicMock()
        watching_channel.id = 999
        twitch.watching_channel.get_with_default.return_value = watching_channel
        twitch.gql_request = AsyncMock(return_value={"data": {"currentUser": None}})
        campaign.can_earn.return_value = False

        service = MessageHandlerService(twitch)

        message = {"type": "drop-claim", "data": {"drop_id": "drop-1", "drop_instance_id": "inst-1"}}

        with (
            patch("asyncio.sleep", new=AsyncMock()),
            patch("src.services.message_handlers.finalize_drop_claim") as finalize_mock,
        ):
            # Must not raise TypeError: 'NoneType' object is not subscriptable
            await service.process_drops(user_id=123, message=message)

        # Only queried once - drop_data resolved to None, breaking the retry
        # loop on the first attempt instead of looping 8 times or crashing.
        twitch.gql_request.assert_awaited_once()
        finalize_mock.assert_called_once()
        twitch.change_state.assert_called_once()


if __name__ == "__main__":
    unittest.main()
