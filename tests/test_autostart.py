import subprocess
import unittest
from unittest.mock import patch

from src.desktop import autostart as autostart_module
from src.desktop.autostart import (
    APPROVED_KEY,
    RUN_KEY,
    AutostartError,
    AutostartManager,
)


class FakeWinreg:
    """Just enough of the winreg API, backed by a dict."""

    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values: dict[tuple[str, str], object] = {}
        self.fail_writes = False

    class _Key:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def OpenKey(self, _root, path, _reserved=0, _access=0):  # noqa: N802
        if not any(p == path for p, _ in self.values) and path == APPROVED_KEY:
            raise FileNotFoundError(path)
        return self._Key(path)

    def CreateKeyEx(self, _root, path, _reserved=0, _access=0):  # noqa: N802
        if self.fail_writes:
            raise PermissionError("denied")
        return self._Key(path)

    def QueryValueEx(self, key, name):  # noqa: N802
        try:
            return self.values[(key.path, name)], self.REG_SZ
        except KeyError:
            raise FileNotFoundError(name) from None

    def SetValueEx(self, key, name, _reserved, _kind, value):  # noqa: N802
        self.values[(key.path, name)] = value

    def DeleteValue(self, key, name):  # noqa: N802
        try:
            del self.values[(key.path, name)]
        except KeyError:
            raise FileNotFoundError(name) from None


class AutostartManagerTests(unittest.TestCase):
    def setUp(self):
        self.reg = FakeWinreg()
        self.command = '"C:\\Apps\\TDM\\TwitchDropsMiner.exe" --minimized'
        self.manager = AutostartManager(command=self.command, registry=self.reg)

    def test_disabled_by_default(self):
        self.assertTrue(self.manager.supported)
        self.assertFalse(self.manager.is_enabled())
        self.assertEqual(self.manager.status()["enabled"], False)

    def test_enable_writes_run_entry_for_current_user(self):
        self.manager.enable()
        self.assertEqual(self.reg.values[(RUN_KEY, "TwitchDropsMiner")], self.command)
        self.assertTrue(self.manager.is_enabled())

    def test_disable_removes_entry_and_is_idempotent(self):
        self.manager.enable()
        self.manager.disable()
        self.manager.disable()
        self.assertFalse(self.manager.is_enabled())
        self.assertNotIn((RUN_KEY, "TwitchDropsMiner"), self.reg.values)

    def test_set_enabled_toggles(self):
        self.manager.set_enabled(True)
        self.assertTrue(self.manager.is_enabled())
        self.manager.set_enabled(False)
        self.assertFalse(self.manager.is_enabled())

    def test_task_manager_disable_is_respected(self):
        self.manager.enable()
        # Task Manager -> Startup apps -> Disable writes a blob starting with an odd byte.
        self.reg.values[(APPROVED_KEY, "TwitchDropsMiner")] = bytes([3]) + bytes(11)
        self.assertFalse(self.manager.is_enabled())
        status = self.manager.status()
        self.assertTrue(status["disabled_in_task_manager"])
        self.assertFalse(status["enabled"])

    def test_enable_clears_task_manager_disable(self):
        self.reg.values[(APPROVED_KEY, "TwitchDropsMiner")] = bytes([3]) + bytes(11)
        self.manager.enable()
        self.assertTrue(self.manager.is_enabled())
        self.assertNotIn((APPROVED_KEY, "TwitchDropsMiner"), self.reg.values)

    def test_approved_entry_with_even_byte_counts_as_enabled(self):
        self.manager.enable()
        self.reg.values[(APPROVED_KEY, "TwitchDropsMiner")] = bytes([2]) + bytes(11)
        self.assertTrue(self.manager.is_enabled())

    def test_stale_entry_is_detected_and_repaired(self):
        self.reg.values[(RUN_KEY, "TwitchDropsMiner")] = '"D:\\old\\TwitchDropsMiner.exe" --minimized'
        self.assertTrue(self.manager.is_stale())
        self.assertTrue(self.manager.repair_if_stale())
        self.assertEqual(self.reg.values[(RUN_KEY, "TwitchDropsMiner")], self.command)
        self.assertFalse(self.manager.repair_if_stale())

    def test_repair_never_creates_an_entry_the_user_did_not_ask_for(self):
        self.assertFalse(self.manager.repair_if_stale())
        self.assertNotIn((RUN_KEY, "TwitchDropsMiner"), self.reg.values)

    def test_write_failure_is_reported_as_autostart_error(self):
        self.reg.fail_writes = True
        with self.assertRaises(AutostartError):
            self.manager.enable()

    def test_unsupported_off_windows(self):
        manager = AutostartManager(command=self.command)
        with patch.object(autostart_module.sys, "platform", "linux"):
            self.assertFalse(manager.supported)
            self.assertFalse(manager.is_enabled())
            self.assertEqual(manager.status(), {"supported": False, "enabled": False})
            with self.assertRaises(AutostartError):
                manager.set_enabled(True)


class BuildCommandTests(unittest.TestCase):
    def test_frozen_build_launches_the_exe_minimized(self):
        manager = AutostartManager(registry=FakeWinreg())
        exe = "C:\\Program Files\\TDM\\TwitchDropsMiner.exe"
        with (
            patch.object(autostart_module, "IS_FROZEN", True),
            patch.object(autostart_module.sys, "executable", exe),
        ):
            command = manager.build_command()
        self.assertEqual(command, subprocess.list2cmdline([exe]) + " --minimized")
        self.assertTrue(command.startswith('"C:\\Program Files'))  # spaces are quoted

    def test_source_run_uses_main_py_with_minimized_flag(self):
        manager = AutostartManager(registry=FakeWinreg())
        with (
            patch.object(autostart_module, "IS_FROZEN", False),
            patch.object(autostart_module.sys, "executable", "/usr/bin/python3"),
        ):
            command = manager.build_command()
        self.assertIn("main.py", command)
        self.assertTrue(command.endswith("--minimized"))


if __name__ == "__main__":
    unittest.main()
