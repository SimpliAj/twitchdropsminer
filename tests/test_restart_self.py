import unittest
from unittest.mock import patch

from src.web.app import _restart_self


class TestRestartSelf(unittest.TestCase):
    @patch("src.web.app.shutil.which", return_value="/usr/local/bin/pm2")
    @patch("src.web.app.subprocess.Popen")
    @patch("src.web.app.os.execv")
    def test_uses_pm2_when_available(self, mock_execv, mock_popen, mock_which):
        _restart_self()
        mock_popen.assert_called_once_with(["pm2", "restart", "twitchdrops"])
        mock_execv.assert_not_called()

    @patch("src.web.app.shutil.which", return_value=None)
    @patch("src.web.app.subprocess.Popen")
    @patch("src.web.app.os.execv")
    def test_falls_back_to_execv_when_pm2_missing(self, mock_execv, mock_popen, mock_which):
        # Regression test for #5: subprocess.Popen(["pm2", ...]) raised an
        # unhandled FileNotFoundError inside the fire-and-forget asyncio task
        # when pm2 isn't on PATH (e.g. plain Docker deployments without pm2).
        _restart_self()
        mock_popen.assert_not_called()
        mock_execv.assert_called_once()


if __name__ == "__main__":
    unittest.main()
