import unittest
from unittest.mock import MagicMock, patch

from src.utils.string_utils import is_masked_proxy_url, mask_proxy_url, mask_text
from src.web.managers.settings import SettingsManager

REAL = "http://user:s3cret@proxy.example:3128"
MASKED = "http://user:****@proxy.example:3128"


class TestMaskFunctions(unittest.TestCase):
    def test_mask(self):
        self.assertEqual(mask_proxy_url(REAL), MASKED)
        self.assertEqual(mask_proxy_url("socks5://u:p@h:1080"), "socks5://u:****@h:1080")
        self.assertEqual(mask_proxy_url("user:pw@h:8080"), "user:****@h:8080")
        self.assertEqual(mask_proxy_url("http://h:8080"), "http://h:8080")
        self.assertEqual(mask_proxy_url("http://tokenonly@h:8080"), "http://tokenonly@h:8080")
        self.assertEqual(mask_proxy_url(""), "")

    def test_password_with_at_sign(self):
        self.assertEqual(mask_proxy_url("http://u:p@ss@h:1"), "http://u:****@h:1")

    def test_is_masked(self):
        self.assertTrue(is_masked_proxy_url(MASKED))
        self.assertFalse(is_masked_proxy_url(REAL))
        self.assertFalse(is_masked_proxy_url("http://h:1"))

    def test_mask_text(self):
        self.assertEqual(mask_text(f"failed via {REAL}!", REAL), f"failed via {MASKED}!")


class TestSettingsMasking(unittest.TestCase):
    def setUp(self):
        p = patch("asyncio.create_task")
        p.start()
        self.addCleanup(p.stop)
        self.settings = MagicMock()
        self.settings.proxy = REAL
        self.console = MagicMock()
        self.manager = SettingsManager(MagicMock(), self.settings, self.console)

    def test_get_settings_masks(self):
        with patch("src.web.managers.settings.vars", create=True, return_value={"proxy": REAL}):
            self.assertEqual(self.manager.get_settings()["proxy"], MASKED)

    def test_masked_echo_keeps_password(self):
        self.manager.update_settings({"proxy": MASKED})
        self.assertEqual(self.settings.proxy, REAL)

    def test_masked_with_other_host_rejected(self):
        self.manager.update_settings({"proxy": "http://user:****@evil.example:1"})
        self.assertEqual(self.settings.proxy, REAL)

    def test_new_proxy_saved_and_log_masked(self):
        self.manager.update_settings({"proxy": "http://a:newpw@h:1"})
        self.assertEqual(self.settings.proxy, "http://a:newpw@h:1")
        logged = " ".join(str(c) for c in self.console.print.call_args_list)
        self.assertNotIn("newpw", logged)
        self.assertIn("a:****@h:1", logged)

    def test_clear(self):
        self.manager.update_settings({"proxy": ""})
        self.assertEqual(self.settings.proxy, "")
