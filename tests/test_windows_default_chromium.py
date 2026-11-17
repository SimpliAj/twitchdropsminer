import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.auth import plain_chrome
from src.auth.plain_chrome import _exe_from_command, _is_chromium_install, windows_default_chromium


class TestDefaultChromium(unittest.TestCase):
    def test_exe_from_command(self):
        self.assertEqual(_exe_from_command('"C:\\A B\\chrome.exe" --single-argument %1'), "C:\\A B\\chrome.exe")
        self.assertEqual(_exe_from_command("C:\\x\\helium.exe %1"), "C:\\x\\helium.exe")

    def test_chromium_detected_by_elf_dll(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d, "chrome.exe")
            exe.write_bytes(b"")
            self.assertFalse(_is_chromium_install(str(exe)))  # e.g. Firefox: no *_elf.dll
            (Path(d) / "140.0.1").mkdir()
            Path(d, "140.0.1", "chrome_elf.dll").write_bytes(b"")
            self.assertTrue(_is_chromium_install(str(exe)))
            self.assertFalse(_is_chromium_install(str(Path(d, "missing.exe"))))

    def test_reads_the_default_browser_from_the_registry(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d, "chrome.exe")
            exe.write_bytes(b"")
            Path(d, "chrome_elf.dll").write_bytes(b"")
            values = {"ProgId": "HeliumHTML", "": f'"{exe}" --single-argument %1'}

            class Key:
                def __init__(self, *a): pass
                def __enter__(self): return self
                def __exit__(self, *a): return False

            fake = SimpleNamespace(
                HKEY_CURRENT_USER=1, HKEY_CLASSES_ROOT=2,
                OpenKey=Key, QueryValueEx=lambda k, name: (values[name], 1),
            )
            with patch.dict(sys.modules, {"winreg": fake}):
                self.assertEqual(windows_default_chromium(), str(exe))

    def test_no_registry_means_none(self):
        with patch.dict(sys.modules, {"winreg": None}):
            self.assertIsNone(plain_chrome.windows_default_chromium())
