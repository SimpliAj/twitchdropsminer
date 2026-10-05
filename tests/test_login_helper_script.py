import importlib.util
import pathlib
import unittest
from unittest.mock import MagicMock

_SPEC = importlib.util.spec_from_file_location(
    "tdm_login_helper", pathlib.Path(__file__).parent.parent / "scripts" / "tdm_login_helper.py"
)
helper = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(helper)


class TestLaunchBrowser(unittest.TestCase):
    def _playwright(self, failing: set):
        pw = MagicMock()

        def launch(channel, headless):
            if channel in failing:
                raise RuntimeError(f"{channel} not installed\nlong playwright noise")
            return f"browser:{channel}"

        pw.chromium.launch.side_effect = launch
        return pw

    def test_falls_back_chrome_then_edge_then_bundled_chromium(self):
        pw = self._playwright({"chrome", "msedge"})
        self.assertEqual(helper.launch_browser(pw, None), "browser:None")
        self.assertEqual([c.kwargs["channel"] for c in pw.chromium.launch.call_args_list],
                         ["chrome", "msedge", None])

    def test_prefers_chrome_when_available(self):
        self.assertEqual(helper.launch_browser(self._playwright(set()), None), "browser:chrome")

    def test_explicit_browser_does_not_fall_back(self):
        pw = self._playwright({"msedge"})
        self.assertIsNone(helper.launch_browser(pw, "edge"))
        self.assertEqual(pw.chromium.launch.call_count, 1)

    def test_all_failing_returns_none_with_a_helpful_message(self):
        pw = self._playwright({"chrome", "msedge", None})
        with unittest.mock.patch("sys.stderr") as err:
            self.assertIsNone(helper.launch_browser(pw, None))
        self.assertTrue(err.write.called)


if __name__ == "__main__":
    unittest.main()
