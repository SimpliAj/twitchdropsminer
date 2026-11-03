"""CI checks on a macOS runner (run from the repository root, with the build venv).

  mac_probe.py --login    the Twitch login window opens in every installed browser and stays open
  mac_probe.py --hidden   the integrity browser mints an anonymous token (mandatory) and no
                          on-screen window appears (reported; fails only with --strict-visibility)

Needs pyobjc-framework-Quartz, which is a probe-only dependency (the app itself uses ctypes).
"""

import asyncio
import os
import sys

sys.path.insert(0, os.getcwd())

from src.auth import browser_login as bl  # noqa: E402
from src.auth.plain_chrome import PlainChrome  # noqa: E402
from src.config.client_info import ClientType  # noqa: E402

BROWSERS = {
    "chrome": "Google Chrome",
    "edge": "Microsoft Edge",
    "brave": "Brave Browser",
}


def find(app: str) -> str | None:
    path = f"/Applications/{app}.app/Contents/MacOS/{app}"
    return path if os.path.exists(path) else None


def onscreen_windows() -> list[str]:
    """Normal-layer, on-screen windows owned by a Chromium browser (any of BROWSERS)."""
    import Quartz  # noqa: PLC0415

    found = []
    for win in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []:
        owner = str(win.get("kCGWindowOwnerName", ""))
        if owner in BROWSERS.values() and win.get("kCGWindowLayer") == 0:
            bounds = win.get("kCGWindowBounds") or {}
            found.append(f"{owner}:{win.get('kCGWindowName', '')!r}:{int(bounds.get('Width', 0))}x{int(bounds.get('Height', 0))}")
    return found


async def probe_login(name: str, binary: str) -> bool:
    chrome = PlainChrome(binary, bl.LOGIN_URL, None)
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


async def probe_hidden_mint(name: str, binary: str) -> tuple[bool, bool]:
    """Returns (token minted, nothing visible)."""
    seen: set[str] = set()
    before = set(onscreen_windows())

    async def watch() -> None:
        while True:
            seen.update(w for w in onscreen_windows() if w not in before)
            await asyncio.sleep(0.2)

    chrome = PlainChrome(binary, "about:blank", None, quiet=True, hidden=True)
    watcher = asyncio.ensure_future(watch())
    await chrome.start()
    try:
        target = next(t for t in (await chrome.call("Target.getTargets"))["result"]["targetInfos"] if t["type"] == "page")
        session = (await chrome.call("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}))["result"]["sessionId"]
        await chrome.call("Page.enable", {}, session)
        await chrome.call("Page.navigate", {"url": bl.INTEGRITY_SDK_PAGE}, session)
        while (await chrome.next_event(timeout=30)).get("method") != "Page.loadEventFired":
            pass
        page = await chrome.call("Runtime.evaluate", {"expression": "globalThis"}, session)
        reply = await chrome.call("Runtime.callFunctionOn", {
            "objectId": page["result"]["result"]["objectId"],
            "functionDeclaration": bl._SDK_MINT_JS,
            "arguments": [{"value": {"client-id": ClientType.WEB.CLIENT_ID}}, {"value": bl.INTEGRITY_SDK_URL}],
            "awaitPromise": True, "returnByValue": True,
        }, session, timeout=bl.INTEGRITY_MINT_TIMEOUT_SEC + 5)
        value = reply.get("result", {}).get("result", {}).get("value") or {}
        got_token = value.get("status") == 200 and bool((value.get("data") or {}).get("token"))
        await asyncio.sleep(1)
        print(f"{name} hidden: on-screen browser windows seen during the mint: {sorted(seen)}; "
              f"anonymous integrity: status={value.get('status')} failure={value.get('failure')} token={got_token}")
        return got_token, not seen
    finally:
        watcher.cancel()
        await chrome.close()


async def main(mode: str, strict: bool) -> int:
    ok = True
    for name, app in BROWSERS.items():
        binary = find(app)
        if binary is None:
            print(f"{name}: not installed, skipped")
            continue
        if mode == "--login":
            ok = await probe_login(name, binary) and ok
        else:
            token, invisible = await probe_hidden_mint(name, binary)
            ok = token and ok
            if not invisible:
                print(f"::warning::{name}: a browser window was visible on screen during the hidden mint")
                ok = ok and not strict
    return 0 if ok else 1


if __name__ == "__main__":
    mode = "--hidden" if "--hidden" in sys.argv else "--login"
    sys.exit(asyncio.run(main(mode, "--strict-visibility" in sys.argv)))
