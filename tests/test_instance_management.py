import json
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from src.web import app as web_app


class TestInstanceManagement(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp_file = Path("/tmp/test_instances.json")
        self.tmp_file.write_text(json.dumps({"instances": [
            {"n": 1, "port": 8080, "data_dir": "data", "pm2_name": "twitchdrops", "label": "Account 1"},
        ]}))
        self._patcher = patch.object(web_app, "_INSTANCES_FILE", self.tmp_file)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self.tmp_file.unlink(missing_ok=True)

    async def test_register_instance_adds_entry_without_touching_pm2(self):
        req = web_app.RegisterInstanceRequest(host="localhost", port=8015, label="Manual A")
        result = await web_app.register_instance(req)
        instances = result["instances"]
        self.assertEqual(len(instances), 2)
        new_entry = instances[1]
        self.assertEqual(new_entry["n"], 2)
        self.assertEqual(new_entry["base_url"], "http://localhost:8015")
        self.assertEqual(new_entry["label"], "Manual A")
        self.assertNotIn("pm2_name", new_entry)

    async def test_register_instance_rejects_invalid_port(self):
        req = web_app.RegisterInstanceRequest(host="localhost", port=0, label=None)
        with self.assertRaises(HTTPException):
            await web_app.register_instance(req)

    async def test_remove_manual_instance_just_drops_it(self):
        await web_app.register_instance(
            web_app.RegisterInstanceRequest(host="localhost", port=8016, label=None)
        )
        result = await web_app.remove_instance(2)
        self.assertEqual(len(result["instances"]), 1)

    async def test_create_instance_fails_clearly_when_script_missing(self):
        # scripts/manage_instance.sh isn't shipped in the Docker image — this
        # must surface a clear error, not a bare subprocess traceback.
        with patch.object(Path, "exists", return_value=False):
            with self.assertRaises(HTTPException) as ctx:
                await web_app.create_instance()
        self.assertIn("Register existing instance", ctx.exception.detail)


class TestLegacyInstancesMigration(unittest.TestCase):
    # instances.json used to live at the repo root, which was neither
    # git-stash-safe (update.sh) nor part of the Docker volume (only ./data
    # is mounted) — every update/container-recreate silently reset manually
    # registered instances back to the two defaults. It now lives under
    # data/, which is both git-ignored and volume-mounted.
    def setUp(self):
        self.legacy_file = Path("/tmp/test_legacy_instances.json")
        self.new_file = Path("/tmp/test_new_instances.json")
        self.legacy_file.unlink(missing_ok=True)
        self.new_file.unlink(missing_ok=True)
        self._legacy_patcher = patch.object(web_app, "_LEGACY_INSTANCES_FILE", self.legacy_file)
        self._new_patcher = patch.object(web_app, "_INSTANCES_FILE", self.new_file)
        self._data_dir_patcher = patch.object(web_app, "_DATA_DIR", self.new_file.parent)
        self._legacy_patcher.start()
        self._new_patcher.start()
        self._data_dir_patcher.start()

    def tearDown(self):
        self._legacy_patcher.stop()
        self._new_patcher.stop()
        self._data_dir_patcher.stop()
        self.legacy_file.unlink(missing_ok=True)
        self.new_file.unlink(missing_ok=True)

    def test_migrates_custom_instances_from_legacy_location(self):
        custom = {"instances": [
            {"n": 1, "port": 8080, "data_dir": "data", "pm2_name": "twitchdrops", "label": "Account 1"},
            {"n": 2, "base_url": "http://localhost:8090", "label": "Manual Account 3"},
        ]}
        self.legacy_file.write_text(json.dumps(custom))
        web_app._migrate_legacy_instances_file()
        self.assertEqual(json.loads(self.new_file.read_text()), custom)

    def test_does_not_overwrite_existing_new_file(self):
        self.legacy_file.write_text(json.dumps({"instances": [{"n": 1, "label": "Legacy"}]}))
        already_there = {"instances": [{"n": 1, "label": "Already migrated"}]}
        self.new_file.write_text(json.dumps(already_there))
        web_app._migrate_legacy_instances_file()
        self.assertEqual(json.loads(self.new_file.read_text()), already_there)

    def test_noop_when_no_legacy_file(self):
        web_app._migrate_legacy_instances_file()
        self.assertFalse(self.new_file.exists())


if __name__ == "__main__":
    unittest.main()
