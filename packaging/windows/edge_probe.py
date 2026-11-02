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


if __name__ == "__main__" and "--hidden" not in sys.argv:
    sys.exit(asyncio.run(main()))


def visible_browser_windows() -> list[str]:
    """Titles of visible Chromium/Edge top-level windows on the user's (default) desktop."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    found: list[str] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            if cls.value.startswith("Chrome_WidgetWin"):
                title = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(hwnd, title, 512)
                if title.value:
                    found.append(title.value)
        return True

    user32.EnumWindows(each, 0)
    return found


async def probe_hidden_mint(name: str, binary: str) -> bool:
    """The integrity browser runs on the invisible desktop, Kasada passes there (an anonymous
    /integrity call gets a token) and no window shows on the user's screen."""
    from src.auth import browser_login as bl
    from src.config.client_info import ClientType

    before = set(visible_browser_windows())  # earlier probes may leave windows behind
    chrome = PlainChrome(binary, "about:blank", None, quiet=True, hidden=True)
    await chrome.start()
    try:
        print(f"{name} hidden: process type {type(chrome._proc).__name__}")
        target = next(t for t in (await chrome.call("Target.getTargets"))["result"]["targetInfos"] if t["type"] == "page")
        session = (await chrome.call("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}))["result"]["sessionId"]
        await chrome.call("Page.enable", {}, session)
        await chrome.call("Page.navigate", {"url": bl.INTEGRITY_SDK_PAGE}, session)
        while (await chrome.next_event(timeout=30)).get("method") != "Page.loadEventFired":
            pass
        shown = [t for t in visible_browser_windows() if t not in before]
        page = await chrome.call("Runtime.evaluate", {"expression": "globalThis"}, session)
        reply = await chrome.call("Runtime.callFunctionOn", {
            "objectId": page["result"]["result"]["objectId"],
            "functionDeclaration": bl._SDK_MINT_JS,
            "arguments": [{"value": {"client-id": ClientType.WEB.CLIENT_ID}}, {"value": bl.INTEGRITY_SDK_URL}],
            "awaitPromise": True, "returnByValue": True,
        }, session, timeout=bl.INTEGRITY_MINT_TIMEOUT_SEC + 5)
        value = reply.get("result", {}).get("result", {}).get("value") or {}
        got_token = value.get("status") == 200 and bool((value.get("data") or {}).get("token"))
        print(f"{name} hidden: visible browser windows on screen: {shown}; "
              f"anonymous integrity: status={value.get('status')} failure={value.get('failure')} token={got_token}")
        return not shown and got_token
    finally:
        await chrome.close()


async def main_hidden() -> int:
    ok = True
    for name, parts in BROWSERS.items():
        binary = find(*parts)
        if binary is not None:
            ok = await probe_hidden_mint(name, binary) and ok
    return 0 if ok else 1


if __name__ == "__main__" and "--hidden" in sys.argv:
    sys.exit(asyncio.run(main_hidden()))
