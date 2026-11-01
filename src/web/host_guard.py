"""Host-header allowlist that blocks DNS-rebinding requests against the dashboard."""

from __future__ import annotations

import os
import re
from ipaddress import ip_address
from urllib.parse import urlsplit

_HOST_RE = re.compile(
    r"(?:\[(?P<ipv6>[0-9a-f:.]+)\]|(?P<name>[a-z0-9_-]+(?:\.[a-z0-9_-]+)*\.?))(?::[0-9]{1,5})?"
)
_NAME_RE = re.compile(r"[a-z0-9_-]+(?:\.[a-z0-9_-]+)*")

MESSAGE = (
    "Untrusted Host header. If you reach the dashboard through a public host name, "
    "set the ALLOWED_HOSTS environment variable (comma-separated host names, "
    "e.g. ALLOWED_HOSTS=tdm.example.com) or PUBLIC_BASE_URL and restart."
)


class HostAllowlist:
    """Accepts IP literals, localhost, single-label and private-suffix names, plus
    operator-allowed hosts. Arbitrary public names are what DNS rebinding needs."""

    LOCAL_SUFFIXES = (".localhost", ".local", ".home.arpa", ".internal")

    def __init__(self, allowed_hosts: str = "", public_base_url: str = ""):
        hosts: set[str] = set()
        for item in allowed_hosts.split(","):
            host = item.strip().lower().removesuffix(".")
            if not host:
                continue
            if not _NAME_RE.fullmatch(host):
                raise ValueError(
                    "ALLOWED_HOSTS must be comma-separated host names without schemes or ports"
                )
            hosts.add(host)
        if public_base_url.strip():
            try:
                base_host = urlsplit(public_base_url.strip()).hostname
            except ValueError:
                base_host = None
            if base_host:
                hosts.add(base_host.lower().removesuffix("."))
        self.allowed_hosts = frozenset(hosts)

    @classmethod
    def from_env(cls) -> HostAllowlist:
        return cls(os.environ.get("ALLOWED_HOSTS", ""), os.environ.get("PUBLIC_BASE_URL", ""))

    def is_trusted(self, host_header: str) -> bool:
        match = _HOST_RE.fullmatch((host_header or "").strip().lower())
        if match is None:
            return False
        try:
            ip_address(match["ipv6"] or match["name"])
            return True
        except ValueError:
            if match["ipv6"]:
                return False
        name = match["name"].removesuffix(".")
        return (
            "." not in name
            or name.endswith(self.LOCAL_SUFFIXES)
            or name in self.allowed_hosts
        )


class HostGuardMiddleware:
    """Pure ASGI wrapper so HTTP, Socket.IO and websocket traffic are all checked."""

    def __init__(self, app, guard: HostAllowlist | None = None):
        self.app = app
        self.guard = guard or HostAllowlist.from_env()

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            host = ""
            for key, value in scope.get("headers", []):
                if key == b"host":
                    host = value.decode("latin-1")
                    break
            if not self.guard.is_trusted(host):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                body = MESSAGE.encode()
                await send({
                    "type": "http.response.start",
                    "status": 421,
                    "headers": [
                        (b"content-type", b"text/plain; charset=utf-8"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                })
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)
