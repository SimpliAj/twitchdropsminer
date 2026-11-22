import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from src.desktop import parallel
from src.web import app as web_app


def _request(headers=None, host="127.0.0.1"):
    request = MagicMock()
    request.headers = headers or {}
    request.client.host = host
    return request


class SlotTests(unittest.TestCase):
    def test_skips_registered_and_busy_ports_and_existing_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "TwitchDropsMiner"
            (Path(tmp) / "TwitchDropsMiner2").mkdir()
            with (
                patch.object(parallel, "DATA_DIR", data),
                patch.object(parallel, "port_in_use", side_effect=lambda p: p == 8082),
            ):
                port, folder = parallel.pick_slot([{"n": 1, "port": 8080}, {"n": 2, "port": 8081}])
        self.assertEqual(port, 8083)
        self.assertEqual(folder, Path(tmp) / "TwitchDropsMiner3")


class LaunchTests(unittest.TestCase):
    def test_child_env_and_args(self):
        with (
            patch.dict(os.environ, {parallel.RESTARTED_ENV: "1"}),
            patch.object(sys, "argv", ["app", "--no-tray", "--minimized", "-v"]),
            patch.object(parallel.subprocess, "Popen") as popen,
        ):
            parallel.launch(8081, Path("/d/TwitchDropsMiner2"), "Account 2", minimized=True)
        args, kwargs = popen.call_args
        self.assertEqual(args[0], [sys.executable, "--no-tray", "--minimized"])
        env = kwargs["env"]
        self.assertEqual(env["TDM_PORT"], "8081")
        self.assertEqual(env["TDM_DATA_DIR"], str(Path("/d/TwitchDropsMiner2")))
        self.assertEqual(env["TDM_LABEL"], "Account 2")
        self.assertEqual(env[parallel.SPAWNED_ENV], "1")
        self.assertEqual(env["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertNotIn(parallel.RESTARTED_ENV, env)

    def test_autostart_only_starts_spawned_copies_that_do_not_answer(self):
        instances = [
            {"n": 1, "port": 8080},
            {"n": 2, "port": 8081, "spawned": True, "data_dir": "/d/2", "label": "Account 2"},
            {"n": 3, "port": 8082, "spawned": True, "data_dir": "/d/3", "label": "Account 3"},
            {"n": 4, "base_url": "http://localhost:9000"},
        ]
        with (
            patch.object(parallel, "port_in_use", side_effect=lambda p: p == 8081),
            patch.object(parallel, "launch") as launch,
        ):
            parallel.autostart(instances, minimized=True)
        launch.assert_called_once_with(8082, Path("/d/3"), "Account 3", True)


class EndpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / "instances.json"
        self.file.write_text(json.dumps({"instances": [{"n": 1, "port": 8080, "label": "Account 1"}]}))
        for p in (
            patch.object(web_app, "_INSTANCES_FILE", self.file),
            patch.object(web_app, "IS_FROZEN", True),
            patch.object(web_app, "_get_password", return_value=""),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    async def test_spawn_launches_and_registers(self):
        with (
            patch.object(web_app.parallel_accounts, "pick_slot", return_value=(8081, Path("/d/TwitchDropsMiner2"))),
            patch.object(web_app.parallel_accounts, "launch") as launch,
        ):
            result = await web_app.spawn_instance(_request())
        launch.assert_called_once_with(8081, Path("/d/TwitchDropsMiner2"), "Account 2")
        entry = result["instances"][1]
        self.assertEqual(entry["base_url"], "http://127.0.0.1:8081")
        self.assertTrue(entry["spawned"])
        self.assertEqual(entry["data_dir"], str(Path("/d/TwitchDropsMiner2")))
        self.assertEqual(json.loads(self.file.read_text())["instances"][1]["port"], 8081)

    async def test_spawn_refused_outside_the_desktop_app(self):
        with patch.object(web_app, "IS_FROZEN", False), self.assertRaises(HTTPException) as ctx:
            await web_app.spawn_instance(_request())
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_spawn_refused_for_bot_and_fleet_headers(self):
        for header in ("X-Bot-Token", "X-Fleet-Password"):
            with self.assertRaises(HTTPException) as ctx:
                await web_app.spawn_instance(_request({header: "x"}))
            self.assertEqual(ctx.exception.status_code, 403)

    async def test_remove_spawned_asks_the_copy_to_close(self):
        entry = {"n": 2, "port": 8081, "base_url": "http://127.0.0.1:8081", "spawned": True, "label": "Account 2"}
        self.file.write_text(json.dumps({"instances": [{"n": 1, "port": 8080}, entry]}))
        post = AsyncMock()
        session = MagicMock()
        session.__aenter__.return_value.post = post
        with patch("aiohttp.ClientSession", return_value=session):
            result = await web_app.remove_instance(2)
        self.assertEqual(post.call_args.args[0], "http://127.0.0.1:8081/api/close")
        self.assertEqual([i["n"] for i in result["instances"]], [1])

    async def test_remove_spawned_survives_an_unreachable_copy(self):
        entry = {"n": 2, "port": 8081, "spawned": True, "label": "Account 2"}
        self.file.write_text(json.dumps({"instances": [{"n": 1, "port": 8080}, entry]}))
        with patch("aiohttp.ClientSession", side_effect=OSError):
            result = await web_app.remove_instance(2)
        self.assertEqual(len(result["instances"]), 1)

    async def test_instances_flag_spawn_support(self):
        self.assertTrue((await web_app.get_instances())["spawn_supported"])
