#!/usr/bin/env python3
"""Run this on your own device (not the server) to log in to Twitch for TDM.

Why this exists: Twitch's own bot/integrity check routinely flags
datacenter IP ranges, so TDM's embedded server-side login (the noVNC
panel in the dashboard) can fail with "Your browser is not currently
supported" no matter what TDM itself does about it. This script drives
your own, already-installed Google Chrome on your own device instead --
a real residential IP and a real browser, the same thing Twitch expects
from any other logged-in user -- and sends the resulting session to your
TDM instance automatically once you've logged in.

Requirements: Python 3.10+, Google Chrome installed, and the `playwright`
and `requests` packages (`pip install playwright requests` is enough --
`playwright install` is NOT needed, this uses your own installed Chrome,
not Playwright's bundled one).

Usage:
    python tdm_login_helper.py --url http://localhost:8080
    python tdm_login_helper.py --url https://your-tdm-domain.example --password yourpassword

--url is your TDM instance's address (wherever its dashboard is reachable
from this device -- same host or a remote one, doesn't matter). --password
is your TDM dashboard password, if you set one (see WEB_PASSWORD in TDM's
own README); omit it if you didn't set one.
"""

from __future__ import annotations

import argparse
import sys
import time

try:
    import requests
except ImportError:
    print("Missing dependency: pip install requests", file=sys.stderr)
    sys.exit(1)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("Missing dependency: pip install playwright", file=sys.stderr)
    sys.exit(1)

LOGIN_URL = "https://www.twitch.tv/login"
COOKIE_DOMAIN = "twitch.tv"
COOKIE_NAME = "auth-token"
DEVICE_ID_COOKIE_NAME = "unique_id"
LOGIN_TIMEOUT_SEC = 600  # 10 minutes to complete login, same as TDM's own
POLL_INTERVAL_SEC = 1.5


def wait_for_login(context) -> dict[str, str]:
    print("Log in to Twitch in the Chrome window that just opened. Waiting...")
    deadline = time.monotonic() + LOGIN_TIMEOUT_SEC
    while time.monotonic() < deadline:
        cookies = context.cookies()
        found = {c["name"]: c["value"] for c in cookies if c["domain"].endswith(COOKIE_DOMAIN)}
        if COOKIE_NAME in found:
            return {
                "auth_token": found[COOKIE_NAME],
                "unique_id": found.get(DEVICE_ID_COOKIE_NAME, ""),
            }
        time.sleep(POLL_INTERVAL_SEC)
    raise TimeoutError(f"No login detected within {LOGIN_TIMEOUT_SEC}s")


def submit_session(base_url: str, password: str | None, auth_token: str, unique_id: str) -> None:
    headers = {"X-Fleet-Password": password} if password else {}
    url = base_url.rstrip("/") + "/api/login/browser/helper-session"
    response = requests.post(
        url, json={"auth_token": auth_token, "unique_id": unique_id}, headers=headers, timeout=10
    )
    if response.status_code == 409:
        raise RuntimeError(
            "TDM has no login attempt in progress -- open its dashboard first "
            "(it starts one automatically whenever a login is needed) and try again."
        )
    if response.status_code == 401:
        raise RuntimeError("TDM rejected the request -- check --password matches its dashboard password.")
    response.raise_for_status()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="Your TDM instance's address, e.g. http://localhost:8080")
    parser.add_argument("--password", default=None, help="TDM dashboard password, if one is set")
    args = parser.parse_args()

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel="chrome", headless=False)
        except Exception as exc:
            print(
                f"Failed to launch Google Chrome: {exc}\n"
                "Make sure Google Chrome (not just Chromium) is installed on this device.",
                file=sys.stderr,
            )
            return 1

        try:
            context = browser.new_context()
            page = context.new_page()
            page.goto(LOGIN_URL, wait_until="domcontentloaded")
            try:
                cookies = wait_for_login(context)
            except TimeoutError as exc:
                print(str(exc), file=sys.stderr)
                return 1

            print("Login detected, sending the session to TDM...")
            try:
                submit_session(args.url, args.password, cookies["auth_token"], cookies["unique_id"])
            except Exception as exc:
                print(f"Failed to send the session to TDM: {exc}", file=sys.stderr)
                return 1

            print("Done -- TDM should now show you as logged in.")
            return 0
        finally:
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
