"""
Coverage for the Analytics "subscribed channels only" filter (Discord feedback
from XTimt: the Channel Points table / Points Over Time dropdown showed every
channel that ever accumulated points — 250+ entries — instead of just the
channels the account actually holds a Twitch subscription to).

Channel points accrue from watching regardless of subscription status, so
"channels with points history" and "channels I'm subscribed to" are genuinely
separate data sources. Subscription status is checked via Helix
/subscriptions/user (viewer/broadcaster pair check — works with a normal user
token, unlike the broadcaster-only /subscriptions list endpoint), since the
GQL subscribedChannels/subscriptionBenefits fields return empty/null for this
app's token even for real, live-verified subscriptions.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def json(self):
        return self._payload


class _FakeRequestContext:
    def __init__(self, payload, status=200):
        self._payload = payload
        self._status = status

    async def __aenter__(self):
        return _FakeResponse(self._payload, self._status)

    async def __aexit__(self, *args):
        return False


class TestFetchSubscribedChannels(unittest.IsolatedAsyncioTestCase):
    def _make_twitch(self, users_payload, sub_responses: dict):
        """sub_responses: broadcaster_id -> (payload, status)"""
        from src.config.client_info import ClientType

        twitch = MagicMock()
        twitch._client_type = ClientType.ANDROID_APP
        auth = MagicMock()
        auth.user_id = "111"
        auth.access_token = "tok"
        twitch.get_auth = AsyncMock(return_value=auth)

        def request(method, url, **kwargs):
            if "helix/users" in url:
                return _FakeRequestContext(users_payload)
            for bid, (payload, status) in sub_responses.items():
                if f"broadcaster_id={bid}" in url:
                    return _FakeRequestContext(payload, status)
            raise AssertionError(f"Unexpected URL: {url}")

        twitch._http_client = MagicMock()
        twitch._http_client.request = MagicMock(side_effect=request)
        return twitch

    async def test_empty_input_returns_empty_without_requests(self):
        from src.core.client import Twitch

        twitch = self._make_twitch({"data": []}, {})
        result = await Twitch._fetch_subscribed_channels(twitch, [])
        self.assertEqual(result, [])
        twitch._http_client.request.assert_not_called()

    async def test_only_actually_subscribed_channels_are_returned(self):
        from src.core.client import Twitch

        users_payload = {
            "data": [
                {"id": "1", "login": "streamera"},
                {"id": "2", "login": "streamerb"},
                {"id": "3", "login": "streamerc"},
            ]
        }
        sub_responses = {
            "1": ({"data": [{"broadcaster_id": "1", "is_gift": False}]}, 200),
            "2": ({"data": []}, 200),
            "3": ({"error": "Not Found"}, 404),
        }
        twitch = self._make_twitch(users_payload, sub_responses)

        result = await Twitch._fetch_subscribed_channels(
            twitch, ["StreamerA", "streamerb", "StreamerC"]
        )

        self.assertEqual(result, ["streamera"])

    async def test_unresolvable_login_is_silently_skipped(self):
        """A channel that Helix can't resolve to a user id (deleted/renamed)
        should not crash the whole check — just isn't included."""
        from src.core.client import Twitch

        users_payload = {"data": [{"id": "1", "login": "streamera"}]}
        sub_responses = {"1": ({"data": []}, 200)}
        twitch = self._make_twitch(users_payload, sub_responses)

        result = await Twitch._fetch_subscribed_channels(
            twitch, ["streamera", "ghostchannel"]
        )
        self.assertEqual(result, [])

    async def test_auth_failure_returns_empty_list_not_exception(self):
        from src.core.client import Twitch

        twitch = MagicMock()
        twitch.get_auth = AsyncMock(side_effect=RuntimeError("boom"))
        result = await Twitch._fetch_subscribed_channels(twitch, ["streamera"])
        self.assertEqual(result, [])


class TestSubscribedChannelsEndpoint(unittest.IsolatedAsyncioTestCase):
    async def test_returns_fresh_data_and_writes_cache(self):
        import src.web.app as app_module

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "channel_points.json").write_text(
                json.dumps({"streamera": 100, "streamerb": 50})
            )
            fake_twitch = MagicMock()
            fake_twitch._fetch_subscribed_channels = AsyncMock(return_value=["streamera"])

            with patch.object(app_module, "_DATA_DIR", tmp_path), \
                    patch.object(app_module, "_WEB_CONFIG_FILE", tmp_path / "web_config.json"), \
                    patch.object(app_module, "gui_manager", MagicMock()), \
                    patch.object(app_module, "twitch_client", fake_twitch):
                result = await app_module.get_subscribed_channels(refresh=False)

            self.assertEqual(result["channels"], ["streamera"])
            self.assertFalse(result["cached"])
            cache_file = tmp_path / "subscribed_channels.json"
            self.assertTrue(cache_file.exists())
            cached = json.loads(cache_file.read_text())
            self.assertEqual(cached["channels"], ["streamera"])

    async def test_fresh_cache_is_served_without_refetching(self):
        import src.web.app as app_module

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "channel_points.json").write_text(json.dumps({"a": 1}))
            (tmp_path / "subscribed_channels.json").write_text(
                json.dumps({"ts": time.time(), "channels": ["a"]})
            )
            fake_twitch = MagicMock()
            fake_twitch._fetch_subscribed_channels = AsyncMock(
                side_effect=AssertionError("should not be called — cache is fresh")
            )

            with patch.object(app_module, "_DATA_DIR", tmp_path), \
                    patch.object(app_module, "_WEB_CONFIG_FILE", tmp_path / "web_config.json"), \
                    patch.object(app_module, "gui_manager", MagicMock()), \
                    patch.object(app_module, "twitch_client", fake_twitch):
                result = await app_module.get_subscribed_channels(refresh=False)

            self.assertEqual(result["channels"], ["a"])
            self.assertTrue(result["cached"])

    async def test_stale_cache_used_as_fallback_on_fetch_failure(self):
        import src.web.app as app_module

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "channel_points.json").write_text(json.dumps({"a": 1}))
            old_ts = time.time() - 999999
            (tmp_path / "subscribed_channels.json").write_text(
                json.dumps({"ts": old_ts, "channels": ["a"]})
            )
            fake_twitch = MagicMock()
            fake_twitch._fetch_subscribed_channels = AsyncMock(
                side_effect=RuntimeError("Twitch is down")
            )

            with patch.object(app_module, "_DATA_DIR", tmp_path), \
                    patch.object(app_module, "_WEB_CONFIG_FILE", tmp_path / "web_config.json"), \
                    patch.object(app_module, "gui_manager", MagicMock()), \
                    patch.object(app_module, "twitch_client", fake_twitch):
                result = await app_module.get_subscribed_channels(refresh=False)

            # Falls back to the stale cache instead of raising / erroring the UI out.
            self.assertEqual(result["channels"], ["a"])
            self.assertTrue(result.get("stale"))

    async def test_refresh_query_param_bypasses_fresh_cache(self):
        import src.web.app as app_module

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "channel_points.json").write_text(json.dumps({"a": 1}))
            (tmp_path / "subscribed_channels.json").write_text(
                json.dumps({"ts": time.time(), "channels": ["a"]})
            )
            fake_twitch = MagicMock()
            fake_twitch._fetch_subscribed_channels = AsyncMock(return_value=["b"])

            with patch.object(app_module, "_DATA_DIR", tmp_path), \
                    patch.object(app_module, "_WEB_CONFIG_FILE", tmp_path / "web_config.json"), \
                    patch.object(app_module, "gui_manager", MagicMock()), \
                    patch.object(app_module, "twitch_client", fake_twitch):
                result = await app_module.get_subscribed_channels(refresh=True)

            self.assertEqual(result["channels"], ["b"])
            self.assertFalse(result["cached"])


if __name__ == "__main__":
    unittest.main()
