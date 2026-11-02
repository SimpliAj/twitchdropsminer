"""CI check: the Twitch login window opens in Edge and Chrome and stays open.

Run from the repository root on a Windows runner: python packaging/windows/edge_probe.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.getcwd())

LOGIN_URL = "https://www.twitch.tv/login"  # same as src/auth/browser_login.py (that imports Playwright)
from src.auth.plain_chrome import PlainChrome  # noqa: E402


BROWSERS = {
    "edge": ("Microsoft", "Edge", "msedge.exe"),
    "chrome": ("Google", "Chrome", "chrome.exe"),
}


def find(vendor: str, app: str, exe: str) -> str | None:
    for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(env_name)
        if base:
            path = os.path.join(base, vendor, app, "Application", exe)
            if os.path.exists(path):
                return path
    return None


async def probe(name: str, binary: str) -> bool:
    chrome = PlainChrome(binary, LOGIN_URL, None)
    try:
        await chrome.start()
    except Exception as exc:
        print(f"{name}: start failed: {exc!r}")
        return False
    try:
        for second in range(20):
            await asyncio.sleep(1)
            if not chrome.alive:
                print(f"{name}: browser process gone after {second + 1}s (exit code {chrome.exit_code})")
                return False
        targets = (await chrome.call("Target.getTargets"))["result"]["targetInfos"]
        urls = [t.get("url") for t in targets if t.get("type") == "page"]
        print(f"{name}: alive after 20s, pages: {urls}")
        return any("twitch.tv" in (u or "") for u in urls)
    finally:
        await chrome.close()


async def main() -> int:
    ok = True
    for name, parts in BROWSERS.items():
        binary = find(*parts)
        if binary is None:
            print(f"{name}: not installed, skipped")
            continue
        ok = await probe(name, binary) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
