import unittest
from unittest.mock import patch

from fastapi import HTTPException

from src.web import app as web_app


class TestInstanceBaseUrl(unittest.TestCase):
    def test_uses_base_url_when_present(self):
        inst = {"n": 3, "base_url": "http://localhost:9001/", "label": "Manual"}
        self.assertEqual(web_app._instance_base_url(inst), "http://localhost:9001")

    def test_builds_from_port_when_no_base_url(self):
        inst = {"n": 2, "port": 8082, "data_dir": "data2", "pm2_name": "twitchdrops2"}
        self.assertEqual(web_app._instance_base_url(inst), "http://127.0.0.1:8082")

    def test_defaults_to_8080_when_port_missing(self):
        self.assertEqual(web_app._instance_base_url({"n": 1}), "http://127.0.0.1:8080")


class TestComputeBulkList(unittest.TestCase):
    def test_add_mode_appends_new_case_insensitive(self):
        current = ["Overwatch", "VALORANT"]
        result = web_app._compute_bulk_list(current, ["valorant", "Fortnite"], "add")
        self.assertEqual(result, ["Overwatch", "VALORANT", "Fortnite"])

    def test_add_mode_ignores_blank_values(self):
        result = web_app._compute_bulk_list(["A"], ["  ", "B", ""], "add")
        self.assertEqual(result, ["A", "B"])

    def test_remove_mode_drops_case_insensitive_matches(self):
        current = ["Overwatch", "VALORANT", "Fortnite"]
        result = web_app._compute_bulk_list(current, ["valorant"], "remove")
        self.assertEqual(result, ["Overwatch", "Fortnite"])

    def test_replace_mode_ignores_current_and_dedupes(self):
        result = web_app._compute_bulk_list(["Old Game"], ["A", "B", "A"], "replace")
        self.assertEqual(result, ["A", "B"])


class TestBulkApplySettingsValidation(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_non_bulk_editable_field(self):
        req = web_app.BulkSettingsRequest(targets=[1], field="proxy", values=["x"])
        with self.assertRaises(HTTPException) as ctx:
            await web_app.bulk_apply_settings(req)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_rejects_bad_mode(self):
        req = web_app.BulkSettingsRequest(targets=[1], field="games_to_watch", values=["x"], mode="wipe")
        with self.assertRaises(HTTPException) as ctx:
            await web_app.bulk_apply_settings(req)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_rejects_empty_targets(self):
        req = web_app.BulkSettingsRequest(targets=[], field="games_to_watch", values=["x"])
        with self.assertRaises(HTTPException) as ctx:
            await web_app.bulk_apply_settings(req)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_unknown_target_reported_without_raising(self):
        with patch.object(
            web_app,
            "_load_instances_registry",
            return_value={"instances": [{"n": 1, "port": 8080}]},
        ):
            req = web_app.BulkSettingsRequest(targets=[99], field="games_to_watch", values=["x"])
            result = await web_app.bulk_apply_settings(req)
        self.assertEqual(len(result["results"]), 1)
        self.assertFalse(result["results"][0]["success"])
        self.assertEqual(result["results"][0]["error"], "Unknown instance")


if __name__ == "__main__":
    unittest.main()
