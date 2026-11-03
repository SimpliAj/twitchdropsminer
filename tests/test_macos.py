import os
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.auth import browser_login
from src.config import paths
from src.desktop import autostart, runtime
from src.desktop.autostart import AutostartManager, MacAutostartManager
from src.desktop.instance import SingleInstanceGuard


class DataDirTests(unittest.TestCase):
    def test_frozen_mac_app_uses_application_support(self):
        with (
            patch.object(paths, "IS_FROZEN", True),
            patch.object(paths._sys, "platform", "darwin"),
            patch.object(Path, "home", return_value=Path("/Users/me")),
            patch.dict(os.environ, {"APPDATA": "/ignored"}),
        ):
            self.assertEqual(
                paths._default_data_dir(), Path("/Users/me/Library/Application Support/TwitchDropsMiner")
            )

    def test_source_run_on_mac_keeps_project_data_dir(self):
        with patch.object(paths, "IS_FROZEN", False), patch.object(paths._sys, "platform", "darwin"):
            self.assertEqual(paths._default_data_dir(), paths.PROJECT_ROOT / "data")

    def test_frozen_linux_unchanged(self):
        with (
            patch.object(paths, "IS_FROZEN", True),
            patch.object(paths._sys, "platform", "linux"),
            patch.dict(os.environ, {"APPDATA": "/appdata"}),
        ):
            self.assertEqual(paths._default_data_dir(), Path("/appdata/TwitchDropsMiner"))


class MacBrowserLookupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name

    def _install(self, root: str, name: str) -> str:
        path = Path(root) / f"{name}.app" / "Contents" / "MacOS" / name
        path.parent.mkdir(parents=True)
        path.touch()
        return str(path)

    def test_nothing_installed(self):
        with patch.object(browser_login.os.path, "exists", side_effect=lambda p: False):
            self.assertIsNone(browser_login.find_mac_browser(self.home))

    def test_user_applications_folder(self):
        brave = self._install(os.path.join(self.home, "Applications"), "Brave Browser")
        self.assertEqual(browser_login.find_mac_browser(self.home), brave)

    def test_chrome_preferred_over_edge_and_brave(self):
        installed = {
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        }
        with patch.object(browser_login.os.path, "exists", side_effect=lambda p: p in installed):
            self.assertEqual(
                browser_login.find_mac_browser(self.home),
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            )

    def test_chromium_is_found_last(self):
        path = "/Applications/Chromium.app/Contents/MacOS/Chromium"
        with patch.object(browser_login.os.path, "exists", side_effect=lambda p: p == path):
            self.assertEqual(browser_login.find_mac_browser(self.home), path)

    def test_missing_browser_gives_a_clear_error(self):
        async def run():
            with (
                patch.object(browser_login, "_IS_MAC", True),
                patch.object(browser_login, "_IS_WINDOWS", False),
                patch.object(browser_login, "find_mac_browser", return_value=None),
                patch.object(browser_login, "async_playwright", None),
            ):
                await browser_login._plain_chrome_binary()

        import asyncio

        with self.assertRaisesRegex(browser_login.LoginException, "Chrome, Edge or Brave"):
            asyncio.run(run())

    def test_mac_counts_as_having_a_desktop(self):
        with patch.object(browser_login, "_IS_MAC", True):
            self.assertEqual(browser_login._detect_real_display(), 0)


class MacAutostartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.agents = Path(self.tmp.name) / "Library" / "LaunchAgents"
        self.exe = "/Applications/TwitchDropsMiner.app/Contents/MacOS/TwitchDropsMiner"
        self.manager = MacAutostartManager(command=[self.exe, "--minimized"], agents_dir=self.agents)

    def test_factory_returns_mac_manager_on_darwin_only(self):
        with patch.object(autostart.sys, "platform", "darwin"):
            self.assertIsInstance(AutostartManager(), MacAutostartManager)
        with patch.object(autostart.sys, "platform", "linux"):
            self.assertNotIsInstance(AutostartManager(), MacAutostartManager)

    def test_enable_writes_launch_agent(self):
        self.assertFalse(self.manager.is_enabled())
        self.manager.set_enabled(True)
        data = plistlib.loads(self.manager.plist_path.read_bytes())
        self.assertEqual(data["Label"], "io.github.simpliaj.twitchdropsminer")
        self.assertEqual(data["ProgramArguments"], [self.exe, "--minimized"])
        self.assertTrue(data["RunAtLoad"])
        self.assertTrue(self.manager.is_enabled())
        self.assertEqual(self.manager.status()["enabled"], True)

    def test_disable_removes_it_and_is_idempotent(self):
        self.manager.enable()
        self.manager.disable()
        self.manager.disable()
        self.assertFalse(self.manager.plist_path.exists())
        self.assertEqual(self.manager.status(), {"supported": True, "enabled": False, "command": None})

    def test_stale_entry_is_repaired_but_never_created(self):
        self.assertFalse(self.manager.repair_if_stale())
        self.assertFalse(self.manager.plist_path.exists())
        self.agents.mkdir(parents=True)
        self.manager.plist_path.write_bytes(plistlib.dumps({"Label": "x", "ProgramArguments": ["/old/app"]}))
        self.assertTrue(self.manager.repair_if_stale())
        self.assertFalse(self.manager.repair_if_stale())

    def test_unreadable_plist_is_reported(self):
        self.agents.mkdir(parents=True)
        self.manager.plist_path.write_text("not a plist")
        self.assertIn("error", self.manager.status())

    def test_default_command_for_the_frozen_app(self):
        manager = MacAutostartManager(agents_dir=self.agents)
        with patch.object(autostart, "IS_FROZEN", True), patch.object(autostart.sys, "executable", self.exe):
            self.assertEqual(manager.build_argv(), [self.exe, "--minimized"])

    def test_write_failure_is_an_autostart_error(self):
        blocker = Path(self.tmp.name) / "file"
        blocker.touch()
        manager = MacAutostartManager(command=["x"], agents_dir=blocker / "sub")
        with self.assertRaises(autostart.AutostartError):
            manager.enable()


class MacInstanceGuardTests(unittest.TestCase):
    def test_second_instance_on_the_same_port_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp, patch("src.config.paths.DATA_DIR", Path(tmp)):
            with patch.object(sys, "platform", "darwin"):
                first, second, other_port = SingleInstanceGuard(18123), SingleInstanceGuard(18123), SingleInstanceGuard(18124)
                self.assertTrue(first.acquire())
                self.assertFalse(second.acquire())
                self.assertTrue(other_port.acquire())
                first.release()
                self.assertTrue(second.acquire())
                second.release()
                other_port.release()


class MacRestartTests(unittest.TestCase):
    def test_frozen_mac_app_reexecs_the_bundle_executable_with_a_clean_bootloader(self):
        exe = "/Applications/TwitchDropsMiner.app/Contents/MacOS/TwitchDropsMiner"
        with (
            patch.object(runtime, "IS_FROZEN", True),
            patch.object(runtime.sys, "platform", "darwin"),
            patch.object(runtime.sys, "executable", exe),
            patch.object(runtime.sys, "argv", [exe, "--minimized"]),
            patch.object(runtime.os, "execve") as execve,
        ):
            runtime.restart_process()
        path, argv, env = execve.call_args.args
        self.assertEqual((path, argv), (exe, [exe, "--minimized"]))
        self.assertEqual(env["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertEqual(env[runtime.RESTARTED_ENV], "1")

    def test_no_tray_on_mac(self):
        with patch.object(runtime, "IS_FROZEN", True), patch.object(runtime.sys, "platform", "darwin"):
            self.assertFalse(runtime.DesktopOptions.parse([]).use_tray)
            self.assertTrue(runtime.DesktopOptions.parse([]).open_browser)
