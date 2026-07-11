import base64
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.exceptions import RequestException
from src.models.channel import Channel, Stream


def _decode_spade_events(payload: dict) -> list:
    return json.loads(base64.b64decode(payload["data"]).decode("utf8"))


class _FakeResponse:
    def __init__(self, status: int):
        self.status = status


class _FakeRequestContext:
    def __init__(self, status: int | None = None, exc: Exception | None = None):
        self._status = status
        self._exc = exc

    async def __aenter__(self):
        if self._exc is not None:
            raise self._exc
        return _FakeResponse(self._status)

    async def __aexit__(self, *args):
        return False


def _make_stream(twitch) -> Stream:
    channel = MagicMock(spec=Channel)
    channel.id = 67890
    channel._login = "example_channel"
    channel._twitch = twitch
    return Stream(
        channel,
        id=24680,
        game={"id": "13579", "name": "Example Game"},
        viewers=100,
        title="Example Stream",
    )


class TestSpadeWatchEvents(unittest.IsolatedAsyncioTestCase):
    def test_spade_payload_contains_minute_watched_event(self):
        twitch = MagicMock()
        twitch._auth_state.user_id = "12345"
        stream = _make_stream(twitch)

        events = _decode_spade_events(stream._spade_payload)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "minute-watched")
        properties = events[0]["properties"]
        self.assertEqual(properties["broadcast_id"], "24680")
        self.assertEqual(properties["channel_id"], "67890")
        self.assertEqual(properties["channel"], "example_channel")
        self.assertEqual(properties["game"], "Example Game")
        self.assertEqual(properties["game_id"], "13579")
        self.assertEqual(properties["minutes_logged"], 1)
        self.assertEqual(properties["location"], "channel")
        self.assertEqual(properties["player"], "site")
        self.assertIs(properties["is_live"], True)
        self.assertIs(properties["live"], True)
        # user_id must be cast to int even if the auth state stores it as a string
        self.assertEqual(properties["user_id"], 12345)
        self.assertIsInstance(properties["user_id"], int)
        self.assertRegex(properties["client_time"], r"^\d{4}-\d{2}-\d{2}T.*Z$")

    def test_spade_payload_client_time_is_not_cached(self):
        twitch = MagicMock()
        twitch._auth_state.user_id = 12345
        stream = _make_stream(twitch)

        first = _decode_spade_events(stream._spade_payload)[0]["properties"]["client_time"]
        second_raw = stream._spade_payload
        second = _decode_spade_events(second_raw)[0]["properties"]["client_time"]

        # Not asserting the timestamps differ (that'd be flaky at sub-second
        # resolution) — asserting the payload is rebuilt fresh each access
        # rather than frozen from the first read (regression: this was a
        # cached_property before, which froze client_time to a stale value).
        self.assertEqual(first, second)
        self.assertIsNot(stream._spade_payload, second_raw)

    async def test_send_watch_posts_to_spade_and_returns_true_for_204(self):
        twitch = MagicMock()
        twitch.gui.channels = MagicMock()
        twitch._auth_state.user_id = 12345
        twitch.request = MagicMock(return_value=_FakeRequestContext(status=204))
        channel = Channel(twitch, id=67890, login="example_channel")
        channel._stream = Stream(
            channel,
            id=24680,
            game={"id": "13579", "name": "Example Game"},
            viewers=100,
            title="Example Stream",
        )
        channel._spade_url = "https://spade.twitch.tv/"

        result = await channel.send_watch()

        self.assertTrue(result)
        twitch.request.assert_called_once()
        args, kwargs = twitch.request.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], "https://spade.twitch.tv/")
        self.assertIn("data", kwargs)

    async def test_send_watch_returns_false_for_non_204_status(self):
        twitch = MagicMock()
        twitch.gui.channels = MagicMock()
        twitch._auth_state.user_id = 12345
        twitch.request = MagicMock(return_value=_FakeRequestContext(status=500))
        channel = Channel(twitch, id=67890, login="example_channel")
        channel._stream = Stream(
            channel,
            id=24680,
            game={"id": "13579", "name": "Example Game"},
            viewers=100,
            title="Example Stream",
        )
        channel._spade_url = "https://spade.twitch.tv/"

        self.assertFalse(await channel.send_watch())

    async def test_send_watch_returns_false_without_stream(self):
        twitch = MagicMock()
        twitch.gui.channels = MagicMock()
        channel = Channel(twitch, id=67890, login="example_channel")

        self.assertFalse(await channel.send_watch())

    async def test_send_watch_returns_false_when_request_fails(self):
        twitch = MagicMock()
        twitch.gui.channels = MagicMock()
        twitch._auth_state.user_id = 12345
        twitch.request = MagicMock(return_value=_FakeRequestContext(exc=RequestException()))
        channel = Channel(twitch, id=67890, login="example_channel")
        channel._stream = Stream(
            channel,
            id=24680,
            game={"id": "13579", "name": "Example Game"},
            viewers=100,
            title="Example Stream",
        )
        channel._spade_url = "https://spade.twitch.tv/"

        self.assertFalse(await channel.send_watch())

    async def test_send_watch_fetches_spade_url_when_missing(self):
        twitch = MagicMock()
        twitch.gui.channels = MagicMock()
        twitch._auth_state.user_id = 12345
        twitch.request = MagicMock(return_value=_FakeRequestContext(status=204))
        channel = Channel(twitch, id=67890, login="example_channel")
        channel._stream = Stream(
            channel,
            id=24680,
            game={"id": "13579", "name": "Example Game"},
            viewers=100,
            title="Example Stream",
        )
        channel._spade_url = None
        fetch_mock = AsyncMock(return_value="https://spade.twitch.tv/fetched")

        with patch.object(Channel, "get_spade_url", fetch_mock):
            result = await channel.send_watch()

        self.assertTrue(result)
        fetch_mock.assert_awaited_once()
        self.assertEqual(channel._spade_url, "https://spade.twitch.tv/fetched")


if __name__ == "__main__":
    unittest.main()
