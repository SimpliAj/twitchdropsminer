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
