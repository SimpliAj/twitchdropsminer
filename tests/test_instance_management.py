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


if __name__ == "__main__":
    unittest.main()
