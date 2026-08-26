"""
Regression tests for the channel-points-tracking case-sensitivity bug: the same
streamer (e.g. "jynxzi" vs "Jynxzi") was tracked as two separate dict keys,
splitting their channel points balance instead of being treated as one channel.
"""
import json
import unittest
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.utils import merge_case_variant_keys


class TestMergeCaseVariantKeys(unittest.TestCase):
    def test_no_duplicates_returns_equivalent_dict(self):
        data = {"jynxzi": 100, "shroud": 50}
        self.assertEqual(merge_case_variant_keys(data), {"jynxzi": 100, "shroud": 50})

    def test_numeric_values_default_to_max(self):
        data = {"jynxzi": 7932, "Jynxzi": 4790}
        self.assertEqual(merge_case_variant_keys(data), {"jynxzi": 7932})

    def test_numeric_values_default_to_max_regardless_of_order(self):
        data = {"Jynxzi": 4790, "jynxzi": 7932}
        self.assertEqual(merge_case_variant_keys(data), {"jynxzi": 7932})

    def test_three_way_case_variants_merge_into_one(self):
        data = {"ELoTRiX": 10, "elotrix": 30, "Elotrix": 20}
        self.assertEqual(merge_case_variant_keys(data), {"elotrix": 30})

    def test_custom_combine_function_used_on_conflict(self):
        data = {"a": [1, 2], "A": [3]}
        merged = merge_case_variant_keys(data, combine=lambda a, b: a + b)
        self.assertEqual(sorted(merged["a"]), [1, 2, 3])

    def test_non_numeric_conflict_without_combine_keeps_last_value(self):
        data = {"a": "first", "A": "second"}
        self.assertEqual(merge_case_variant_keys(data), {"a": "second"})


class TestIdleChannelLoginNormalized(unittest.IsolatedAsyncioTestCase):
    """The idle-watch path used to preserve whatever case the user typed into
    idle_channels (e.g. "Jynxzi"), while drop-farming channels always got the
    canonical lowercase login from campaign data — splitting the same channel's
    tracked data across two keys."""

    async def test_user_typed_mixed_case_login_is_lowercased(self):
        from src.core.client import Twitch

        twitch = MagicMock()
        twitch.gui.channels = MagicMock()
        twitch.gql_request = AsyncMock(return_value={
            "data": {"user": {
                "id": "123",
                "displayName": "Jynxzi",
                "stream": {"id": "999", "viewersCount": 42},
                "broadcastSettings": {
                    "title": "stream",
                    "game": {"id": 1, "name": "Just Chatting", "boxArtURL": "http://img"},
                },
            }}
        })

        channel = await Twitch._fetch_idle_channel_by_login(twitch, "Jynxzi")

        self.assertIsNotNone(channel)
        self.assertEqual(channel._login, "jynxzi")
        # The GQL lookup itself should also use the normalized login.
        called_ops = twitch.gql_request.call_args[0][0]
        self.assertEqual(called_ops["variables"]["channel"], "jynxzi")


@pytest.fixture
def isolated_account_dir(tmp_path, monkeypatch):
    import src.web.app as app_module
    monkeypatch.setattr(app_module, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(app_module, "_WEB_CONFIG_FILE", tmp_path / "web_config.json")
    return tmp_path


def test_load_channel_points_history_merges_existing_duplicates(isolated_account_dir):
    import src.web.app as app_module

    cp_file = isolated_account_dir / "channel_points.json"
    cp_file.write_text(json.dumps({"jynxzi": 7932, "Jynxzi": 4790, "shroud": 12}))

    history = app_module._load_channel_points_history()

    assert history == {"jynxzi": 7932, "shroud": 12}


class TestChannelPointsEndpointPersistsLowercaseKey(unittest.IsolatedAsyncioTestCase):
    async def test_get_channel_points_endpoint_persists_lowercase_key(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        import src.web.app as app_module

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cp_file = tmp_path / "channel_points.json"
            cp_file.write_text(json.dumps({"jynxzi": 500}))

            fake_gui_manager = MagicMock()
            fake_gui_manager._twitch.gql_request = AsyncMock(return_value={
                "data": {"community": {"channel": {"self": {"communityPoints": {"balance": 900}}}}}
            })

            with patch.object(app_module, "_DATA_DIR", tmp_path), \
                    patch.object(app_module, "_WEB_CONFIG_FILE", tmp_path / "web_config.json"), \
                    patch.object(app_module, "gui_manager", fake_gui_manager):
                result = await app_module.get_channel_points("Jynxzi")

            self.assertEqual(result["balance"], 900)
            saved = json.loads(cp_file.read_text())
            self.assertEqual(saved, {"jynxzi": 900})
            self.assertNotIn("Jynxzi", saved)


if __name__ == "__main__":
    unittest.main()
