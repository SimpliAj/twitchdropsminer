"""A normal Chrome process observed only through its DevTools port.

Twitch detects Playwright-driven browsers: its login page answers "Your
browser is not currently supported" and integrity tokens minted there are
rejected. A Chrome started as an ordinary process with
--remote-debugging-port, read only through the browser target (cookies) or
the Network/Page domains -- never Runtime / JS injection -- is not flagged
(confirmed live 2026-10-14 on Windows, x86 Linux Docker and a VPS).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp


logger = logging.getLogger("TwitchDrops")


STALE_TMP_AGE_SEC = 2 * 3600
_STALE_TMP_PATTERNS = ("tdm-chrome-*", "org.chromium.Chromium.*", ".org.chromium.Chromium.*")


def is_stale_tmp_entry(mtime: float, owner_uid: int, now: float, uid: int | None) -> bool:
    """Old enough (a concurrent instance's live profile is always fresh) and ours."""
    return now - mtime > STALE_TMP_AGE_SEC and (uid is None or owner_uid == uid)


def _paths_in_use() -> str:
    """All running processes' command lines joined (Linux /proc only, else empty)."""
    parts = []
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            parts.append(cmdline.read_bytes().replace(b"\0", b" ").decode(errors="ignore"))
        except OSError:
            continue
    return "\n".join(parts)


def sweep_stale_chrome_tmp() -> int:
    """Delete leftover Chromium profile/temp entries in the temp dir that a
    crashed or killed run left behind. Returns how many were removed."""
    uid = os.getuid() if hasattr(os, "getuid") else None
    now = time.time()
    removed = 0
    in_use = _paths_in_use()
    for pattern in _STALE_TMP_PATTERNS:
        for path in Path(tempfile.gettempdir()).glob(pattern):
            if str(path) in in_use:
                continue  # e.g. another instance's login browser left open for hours
            try:
                st = path.lstat()
                if not is_stale_tmp_entry(st.st_mtime, st.st_uid, now, uid):
                    continue
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink()
                removed += 1
            except OSError:
                continue
    return removed


def clean_browser_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for a browser started by the packaged (PyInstaller) app.

    A frozen app carries bootloader variables (``_PYI_*``, ``_MEIPASS2``) and has its own
    ``_internal`` folder on PATH; none of that is meant for a browser, so it is removed. Outside
    a frozen build the environment is returned unchanged."""
    source = dict(os.environ if env is None else env)
    if not getattr(sys, "frozen", False):
        return source
    bundle = os.path.normcase(os.path.abspath(getattr(sys, "_MEIPASS", "") or "\0"))
    for key in list(source):
        if key.startswith(("_PYI", "PYINSTALLER_")) or key == "_MEIPASS2":
            del source[key]
    parts = [
        part
        for part in source.get("PATH", "").split(os.pathsep)
        if part and not os.path.normcase(os.path.abspath(part)).startswith(bundle)
    ]
    source["PATH"] = os.pathsep.join(parts)
    return source


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class PlainChrome:
    # A real (not headless -- Twitch flags those) window, parked off-screen and kept from being
    # throttled while minimized/occluded, for background work such as minting integrity tokens.
    _HIDDEN_ARGS = (
        "--window-position=-32000,-32000",
        "--window-size=800,600",
        "--start-minimized",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-background-timer-throttling",
        "--disable-features=CalculateNativeWinOcclusion",
    )

    def __init__(
        self,
        binary: str,
        url: str,
        env: dict[str, str] | None = None,
        *,
        no_sandbox: bool = False,
        quiet: bool = False,
        hidden: bool = False,
    ) -> None:
        self._binary = binary
        self._url = url
        self._env = env
        self._no_sandbox = no_sandbox
        self._quiet = quiet
        self._hidden = hidden
        self._proc: asyncio.subprocess.Process | None = None
        self._http: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._profile_dir: str | None = None
        self._next_id = 0
        self._events: list[dict] = []
        self._started_at = 0.0
        self._hider: asyncio.Future | None = None

    @staticmethod
    async def _keep_hidden_mac(proc: asyncio.subprocess.Process) -> None:
        """Chrome's windows cannot be parked off-screen on macOS: hide the whole app, and keep
        doing so while it runs (it re-activates itself when it opens a window)."""
        from src.auth.mac_hide import hide_pid  # noqa: PLC0415

        started = time.monotonic()
        while proc.returncode is None:
            try:
                hide_pid(proc.pid)
            except Exception:
                logger.debug("Could not hide the browser app", exc_info=True)
                return
            await asyncio.sleep(0.02 if time.monotonic() - started < 10 else 0.25)

    @property
    def uptime(self) -> float:
        """Seconds since the browser was started."""
        return time.monotonic() - self._started_at if self._started_at else 0.0

    @property
    def exit_code(self) -> int | None:
        """The browser process' exit code once it has exited, else None."""
        return self._proc.returncode if self._proc is not None else None

    @property
    def alive(self) -> bool:
        return (
            self._proc is not None
            and self._proc.returncode is None
            and self._ws is not None
            and not self._ws.closed
        )

    async def start(self, timeout: float = 30.0) -> None:
        port = free_port()
        self._profile_dir = tempfile.mkdtemp(prefix="tdm-chrome-")
        # Chromium's own temp files (component-updater downloads, unpackers)
        # follow TMPDIR, so they go away with the profile instead of piling
        # up in a RAM-backed /tmp.
        chrome_tmp = os.path.join(self._profile_dir, "tmp")
        os.makedirs(chrome_tmp, exist_ok=True)
        env = {**clean_browser_env(self._env), "TMPDIR": chrome_tmp}
        args = [
            self._binary,
            *(["--no-sandbox"] if self._no_sandbox else []),
            f"--user-data-dir={self._profile_dir}",
            f"--remote-debugging-port={port}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-component-update",
            *(
                ["--disable-background-networking", "--disable-sync", "--no-pings"]
                if self._quiet
                else []
            ),
            *(self._HIDDEN_ARGS if self._hidden else []),
            self._url,
        ]
        spawn_kwargs: dict = {}
        if self._hidden and os.name == "nt":
            # Start minimized without taking focus (SW_SHOWMINNOACTIVE) so a background task
            # never flashes a window over what the user is doing.
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 7
            spawn_kwargs["startupinfo"] = startup
        try:
            self._proc = None
            if self._hidden and os.name == "nt":
                # Own invisible desktop: Chromium pulls off-screen windows back into view.
                try:
                    from src.auth.win_hidden_desktop import spawn_on_hidden_desktop

                    self._proc = spawn_on_hidden_desktop(args, env)
                except OSError as exc:
                    logger.warning(f"Hidden desktop unavailable ({exc}); the browser window may show briefly")
            if self._proc is None:
                self._proc = await asyncio.create_subprocess_exec(
                    *args, env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    **spawn_kwargs,
                )
            self._started_at = time.monotonic()
            if self._hidden and sys.platform == "darwin" and self._proc.pid:
                self._hider = asyncio.ensure_future(self._keep_hidden_mac(self._proc))
            self._http = aiohttp.ClientSession()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while True:
                try:
                    async with self._http.get(f"http://127.0.0.1:{port}/json/version") as resp:
                        ws_url = (await resp.json())["webSocketDebuggerUrl"]
                        break
                except aiohttp.ClientError:
                    if self._proc.returncode is not None or loop.time() >= deadline:
                        raise RuntimeError("Chrome debug endpoint never came up") from None
                    await asyncio.sleep(0.5)
            self._ws = await self._http.ws_connect(ws_url, max_msg_size=0)
        except BaseException:
            # Missing binary, a Chrome that died, a bad debug answer: never
            # leave the profile dir, the HTTP session or the process behind.
            await self.close()
            raise

    async def call(
        self, method: str, params: dict | None = None, session_id: str | None = None, timeout: float = 30.0
    ) -> dict:
        assert self._ws is not None, "start() first"
        self._next_id += 1
        msg: dict = {"id": self._next_id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        await self._ws.send_json(msg)
        while True:
            data = await self._ws.receive_json(timeout=timeout)
            if data.get("id") == msg["id"]:
                return data
            self._events.append(data)

    async def next_event(self, timeout: float) -> dict:
        if self._events:
            return self._events.pop(0)
        assert self._ws is not None, "start() first"
        return await self._ws.receive_json(timeout=timeout)

    async def cookies(self, domain_suffix: str) -> dict[str, str]:
        reply = await self.call("Storage.getCookies")
        return {
            c["name"]: c["value"]
            for c in reply.get("result", {}).get("cookies", [])
            if c["domain"].endswith(domain_suffix)
        }

    async def close(self) -> None:
        ws, http, proc, profile_dir = self._ws, self._http, self._proc, self._profile_dir
        self._ws = self._http = self._profile_dir = None
        if self._hider is not None:
            self._hider.cancel()
            self._hider = None
        try:
            if ws is not None and not ws.closed:
                try:
                    self._next_id += 1
                    await ws.send_json({"id": self._next_id, "method": "Browser.close", "params": {}})
                except Exception:
                    pass
                await ws.close()
            if http is not None:
                await http.close()
            if proc is not None and proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
        finally:
            if profile_dir is not None:
                shutil.rmtree(profile_dir, ignore_errors=True)
