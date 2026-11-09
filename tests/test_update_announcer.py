import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.services import update_announcer as ua

HOOK = "https://discord.com/api/webhooks/x/y"


class _Resp:
    def __init__(self, status=204):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class TestVersionCompare(unittest.TestCase):
    def test_numeric_not_string(self):
        self.assertTrue(ua.is_newer("1.8.19", "1.8.9"))
        self.assertFalse(ua.is_newer("1.8.9", "1.8.19"))
        self.assertFalse(ua.is_newer("1.8.19", "1.8.19"))
        self.assertTrue(ua.is_newer("1.10.0", "1.9.9"))
        self.assertFalse(ua.is_newer("", "1.0.0"))


class TestAnnounce(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for p in (
            patch.object(ua, "ANNOUNCED_FILE", Path(tmp.name) / "v.txt"),
            patch.object(ua, "__version__", "1.8.9"),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.session = MagicMock()
        self.session.post = MagicMock(return_value=_Resp())
        cs = patch.object(ua.aiohttp, "ClientSession")
        client_session = cs.start()
        client_session.return_value.__aenter__ = AsyncMock(return_value=self.session)
        client_session.return_value.__aexit__ = AsyncMock(return_value=False)
        self.addCleanup(cs.stop)

    def _latest(self, **kw):
        return patch.object(
            ua,
            "fetch_latest_release",
            AsyncMock(**kw) if kw else AsyncMock(return_value={"version": "1.8.19", "url": "u", "notes": ""}),
        )

    async def test_announces_once_per_version(self):
        with self._latest() as latest:
            await ua.announce_new_release(HOOK)
            await ua.announce_new_release(HOOK)
        self.assertEqual(self.session.post.call_count, 1)
        self.assertEqual(latest.await_count, 2)

    async def test_no_webhook_no_request(self):
        with self._latest() as latest:
            await ua.announce_new_release("")
        latest.assert_not_called()
        self.session.post.assert_not_called()

    async def test_failed_lookup_is_harmless(self):
        with self._latest(side_effect=OSError("down")):
            await ua.announce_new_release(HOOK)
        self.session.post.assert_not_called()

    async def test_rejected_post_is_not_recorded(self):
        self.session.post.return_value = _Resp(404)
        with self._latest():
            await ua.announce_new_release(HOOK)
        self.assertFalse(ua.ANNOUNCED_FILE.exists())

    async def test_not_newer_is_silent(self):
        with self._latest(return_value={"version": "1.8.9", "url": "u", "notes": ""}):
            await ua.announce_new_release(HOOK)
        self.session.post.assert_not_called()
