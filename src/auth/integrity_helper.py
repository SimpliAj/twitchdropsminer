"""Mint Client-Integrity tokens for TDM instances whose own browser Twitch
won't trust -- a Raspberry Pi's Chromium only ever gets tokens that fail the
integrity check (confirmed live 2026-10-29), while the same mint on an x86
PC or server passes, and the token then works from any IP.

Run it on such a machine (it needs Chrome or Chromium), bound to its LAN or
VPN address (e.g. its Tailscale IP), never a public one:

    TDM_INTEGRITY_HELPER_KEY=<long random secret> python -m src.auth.integrity_helper --host <LAN/VPN IP> --port 8099

and point the TDM instance at it:

    TDM_INTEGRITY_HELPER_URL=http://<LAN/VPN IP>:8099
    TDM_INTEGRITY_HELPER_KEY=<the same secret>

The instance sends its Twitch auth token with each request; TDM sends it
over plain http only to loopback, private LAN and Tailscale addresses,
anything else needs https (e.g. behind a reverse proxy). Nothing is logged
or stored.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import logging
import os
import sys

from aiohttp import web

from src.auth.browser_login import INTEGRITY_HELPER_KEY_ENV, mint_integrity_token_locally


logger = logging.getLogger("TwitchDrops")


def create_app(key: str) -> web.Application:
    lock = asyncio.Lock()  # one Chrome at a time

    async def mint(request: web.Request) -> web.Response:
        given = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(given.encode(), key.encode()):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
            auth_token, device_id = str(body["auth_token"]), str(body.get("device_id") or "")
        except Exception:
            return web.json_response({"error": "bad request"}, status=400)
        async with lock:
            result = await mint_integrity_token_locally(auth_token, device_id)
        if result is None:
            return web.json_response({"error": "mint failed"}, status=502)
        token, expires_at = result
        return web.json_response({"token": token, "expires_at": expires_at.timestamp()})

    app = web.Application(client_max_size=16 * 1024)
    app.router.add_post("/mint", mint)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Mint Twitch Client-Integrity tokens for other TDM instances.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()
    key = os.environ.get(INTEGRITY_HELPER_KEY_ENV, "").strip()
    if len(key) < 16:
        sys.exit(f"Set {INTEGRITY_HELPER_KEY_ENV} to a random secret of at least 16 characters")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    web.run_app(create_app(key), host=args.host, port=args.port, access_log=None)


if __name__ == "__main__":
    main()
