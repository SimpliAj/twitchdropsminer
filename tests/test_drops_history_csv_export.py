import csv
import io
import json
import unittest


class TestDropsHistoryCsvExport(unittest.IsolatedAsyncioTestCase):
    """
    Regression/feature test for the CSV export ported from upstream
    TwitchDropsMiner v1.3.0's "Drop History & CSV Export" -- our fork
    already had drops_history.json + /api/drops-history + a History tab
    predating upstream's release; this endpoint (/api/drops-history/export.csv)
    was the one piece actually missing.
    """

    def setUp(self):
        import src.web.app as app_module

        self.app_module = app_module
        # _get_account_data_dir() falls back to _WEB_CONFIG_FILE's
        # "active_account" entry -- point it at a config file with no such
        # key so tests are isolated from whatever real account (if any) is
        # configured on the machine running the suite, same reasoning as
        # _DATA_DIR below.
        self._orig_data_dir = app_module._DATA_DIR
        self._orig_web_config_file = app_module._WEB_CONFIG_FILE

    def tearDown(self):
        self.app_module._DATA_DIR = self._orig_data_dir
        self.app_module._WEB_CONFIG_FILE = self._orig_web_config_file

    def _set_history(self, tmp_path, entries):
        self.app_module._DATA_DIR = tmp_path
        self.app_module._WEB_CONFIG_FILE = tmp_path / "web_config.json"  # deliberately absent
        (tmp_path / "drops_history.json").write_text(json.dumps(entries))

    async def test_exports_all_fields_as_csv(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._set_history(tmp_path, [
                {"timestamp": "2026-06-10T10:00:00+00:00", "game": "R6", "drop": "Pack",
                 "reward": "Pack 1", "image_url": "https://example.com/img.jpg"},
                {"timestamp": "2026-06-11T08:00:00+00:00", "game": "Rust", "drop": "Skin",
                 "reward": "Skin 1", "image_url": None},
            ])

            response = await self.app_module.export_drops_history_csv()

        self.assertEqual(response.media_type, "text/csv")
        self.assertIn("attachment; filename=", response.headers["Content-Disposition"])
        rows = list(csv.reader(io.StringIO(response.body.decode("utf-8"))))
        self.assertEqual(rows[0], ["timestamp", "game", "drop", "reward", "image_url"])
        self.assertEqual(rows[1], ["2026-06-10T10:00:00+00:00", "R6", "Pack", "Pack 1", "https://example.com/img.jpg"])
        self.assertEqual(rows[2], ["2026-06-11T08:00:00+00:00", "Rust", "Skin", "Skin 1", ""])

    async def test_exports_header_only_when_history_is_empty(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._set_history(tmp_path, [])

            response = await self.app_module.export_drops_history_csv()

        rows = list(csv.reader(io.StringIO(response.body.decode("utf-8"))))
        self.assertEqual(rows, [["timestamp", "game", "drop", "reward", "image_url"]])


if __name__ == "__main__":
    unittest.main()
