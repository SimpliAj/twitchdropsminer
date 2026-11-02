import unittest
import asyncio

import pytest

from src.web.host_guard import HostAllowlist, HostGuardMiddleware


@pytest.mark.parametrize("host", [
    "100.98.219.67:8080", "127.0.0.1", "[::1]:8082", "localhost:8080", "a.localhost",
    "tdm", "tdm:8080", "nas.local", "x.home.arpa",
])
def test_trusted(host):
    assert HostAllowlist().is_trusted(host)


@pytest.mark.parametrize("host", ["", "evil.example.com", "evil.example.com:8080", "a b", "[zz]", "x.com."])
def test_rejected(host):
    assert not HostAllowlist().is_trusted(host)


def test_allowed_hosts_and_public_base_url():
    g = HostAllowlist("tdm.example.com, Other.org", "https://pub.example.net:8443")
    for h in ("tdm.example.com:8080", "other.org", "pub.example.net", "pub.example.net:8443"):
        assert g.is_trusted(h)
    assert not g.is_trusted("evil.example.com")


def test_bad_allowed_hosts():
    with pytest.raises(ValueError):
        HostAllowlist("https://x.com")


def _call(scope):
    sent = []

    async def app(scope, receive, send):
        sent.append("app")

    async def send(msg):
        sent.append(msg)

    asyncio.run(HostGuardMiddleware(app, HostAllowlist())(scope, None, send))
    return sent


def test_middleware_blocks_and_passes():
    bad = _call({"type": "http", "headers": [(b"host", b"evil.example.com")]})
    assert bad[0]["status"] == 421 and b"ALLOWED_HOSTS" in bad[1]["body"]
    assert _call({"type": "http", "headers": [(b"host", b"100.98.219.67:8080")]}) == ["app"]
    ws = _call({"type": "websocket", "headers": [(b"host", b"evil.example.com")]})
    assert ws[0]["type"] == "websocket.close"
    assert _call({"type": "lifespan"}) == ["app"]


class TestCrossOrigin(unittest.IsolatedAsyncioTestCase):
    async def _call(self, method="POST", origin=None, host="127.0.0.1:8080", kind="http", allowed=""):
        from src.web.host_guard import HostAllowlist, HostGuardMiddleware

        reached, sent = [], []

        async def app(scope, receive, send):
            reached.append(scope)

        async def send(message):
            sent.append(message)

        headers = [(b"host", host.encode())]
        if origin is not None:
            headers.append((b"origin", origin.encode()))
        scope = {"type": kind, "headers": headers}
        if kind == "http":
            scope["method"] = method
        await HostGuardMiddleware(app, HostAllowlist(allowed))(scope, None, send)
        return bool(reached), sent

    async def test_same_origin_post_passes(self):
        ok, _ = await self._call(origin="http://127.0.0.1:8080")
        self.assertTrue(ok)

    async def test_tls_proxy_scheme_difference_passes(self):
        ok, _ = await self._call(origin="https://tdm.example.com", host="tdm.example.com", allowed="tdm.example.com")
        self.assertTrue(ok)

    async def test_proxy_rewriting_host_passes_for_allowed_origin(self):
        ok, _ = await self._call(origin="https://tdm.example.com", host="127.0.0.1:8080", allowed="tdm.example.com")
        self.assertTrue(ok)

    async def test_foreign_site_post_is_refused(self):
        ok, sent = await self._call(origin="https://evil.example")
        self.assertFalse(ok)
        self.assertEqual(sent[0]["status"], 403)

    async def test_other_port_is_refused(self):
        ok, _ = await self._call(origin="http://127.0.0.1:9999")
        self.assertFalse(ok)

    async def test_null_origin_is_refused(self):
        ok, _ = await self._call(origin="null")
        self.assertFalse(ok)

    async def test_foreign_websocket_is_closed(self):
        ok, sent = await self._call(kind="websocket", origin="https://evil.example")
        self.assertFalse(ok)
        self.assertEqual(sent[0], {"type": "websocket.close", "code": 1008})

    async def test_foreign_read_is_refused_but_requests_without_origin_pass(self):
        self.assertFalse((await self._call(method="GET", origin="https://evil.example"))[0])
        self.assertTrue((await self._call(method="GET", origin=None))[0])
        self.assertTrue((await self._call(origin=None))[0])
