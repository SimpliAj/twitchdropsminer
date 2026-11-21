import subprocess
import unittest
from unittest.mock import MagicMock, patch

from src.auth import plain_chrome
from src.auth.plain_chrome import PlainChrome, kill_windows_processes_matching


class TestKillWindowsProcesses(unittest.TestCase):
    def test_noop_off_windows(self):
        with patch.object(plain_chrome.os, "name", "posix"), patch.object(subprocess, "run") as run:
            self.assertEqual(kill_windows_processes_matching("tdm-mint-"), 0)
        run.assert_not_called()

    def test_match_text_goes_in_via_env_not_the_script(self):
        with patch.object(plain_chrome.os, "name", "nt"), \
                patch.object(subprocess, "run", return_value=MagicMock(stdout="3\n")) as run:
            self.assertEqual(kill_windows_processes_matching("C:\\T\\tdm-mint-ab'c"), 3)
        args, kwargs = run.call_args
        self.assertNotIn("tdm-mint-", " ".join(args[0]))
        self.assertEqual(kwargs["env"]["TDM_KILL_MATCH"], "C:\\T\\tdm-mint-ab'c")

    def test_failure_is_harmless(self):
        with patch.object(plain_chrome.os, "name", "nt"), \
                patch.object(subprocess, "run", side_effect=OSError("no powershell")):
            self.assertEqual(kill_windows_processes_matching("x"), 0)


class TestCloseSweepsItsProfile(unittest.IsolatedAsyncioTestCase):
    async def test_close_kills_processes_of_its_profile_on_windows(self):
        chrome = PlainChrome("chrome.exe", "about:blank", profile_prefix="tdm-mint-")
        chrome._profile_dir = "C:\\Temp\\tdm-mint-123"
        with patch.object(plain_chrome.os, "name", "nt"), \
                patch.object(plain_chrome, "kill_windows_processes_matching", return_value=2) as kill, \
                patch.object(plain_chrome.shutil, "rmtree"):
            await chrome.close()
        kill.assert_called_once_with("C:\\Temp\\tdm-mint-123")
