import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ENTRY = Path(__file__).resolve().parents[1] / "packaging" / "windows" / "entry.py"


def _load_entry():
    spec = importlib.util.spec_from_file_location("tdm_windows_entry", ENTRY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WindowsEntryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"TDM_DATA_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.entry = _load_entry()
        self.log = Path(self.tmp.name) / "logs" / "startup.log"

    def test_crash_is_logged_and_shown_then_reraised(self):
        with (
            patch.object(self.entry.runpy, "run_module", side_effect=RuntimeError("boom")),
            patch.object(self.entry, "_show_error") as show,
            self.assertRaises(RuntimeError),
        ):
            self.entry.main()
        text = self.log.read_text(encoding="utf-8")
        self.assertIn("starting", text)
        self.assertIn("CRASH", text)
        self.assertIn("RuntimeError: boom", text)
        self.assertIn("boom", show.call_args.args[0])

    def test_a_silent_early_exit_leaves_a_trace(self):
        with (
            patch.object(self.entry.runpy, "run_module", side_effect=SystemExit(0)),
            patch.object(self.entry, "_show_error") as show,
            self.assertRaises(SystemExit),
        ):
            self.entry.main()
        self.assertIn("exited with code 0", self.log.read_text(encoding="utf-8"))
        show.assert_not_called()

    def test_nonzero_exit_is_shown_but_a_clean_exit_is_not(self):
        with (
            patch.object(self.entry.runpy, "run_module", side_effect=SystemExit(1)),
            patch.object(self.entry, "_show_error") as show,
            self.assertRaises(SystemExit),
        ):
            self.entry.main()
        self.assertIn("exit code 1", show.call_args.args[0])

    def test_normal_run_is_noted(self):
        with patch.object(self.entry.runpy, "run_module"):
            self.entry.main()
        self.assertIn("returned normally", self.log.read_text(encoding="utf-8"))

    def test_message_box_is_windows_only(self):
        with patch.object(sys, "platform", "linux"):
            self.entry._show_error("details")  # must do nothing and not raise


if __name__ == "__main__":
    unittest.main()
