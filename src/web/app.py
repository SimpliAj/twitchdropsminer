from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import json
import os
import secrets

import socketio
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

_DATA_DIR = Path(__file__).parent.parent.parent / "data"
_WEB_CONFIG_FILE = _DATA_DIR / "web_config.json"
_BOT_TOKEN_FILE = _DATA_DIR / "discord_bot_token.json"
_PAIRINGS_FILE = Path(__file__).parent.parent.parent / "discord_bot" / "pairings.json"
_pair_codes: dict[str, dict] = {}  # code -> {token, expires}

def _get_account_data_dir() -> Path:
    """Account-aware data dir — same logic as src.config.paths but importable here."""
    try:
        cfg = json.loads(_WEB_CONFIG_FILE.read_text()) if _WEB_CONFIG_FILE.exists() else {}
        account = cfg.get("active_account")
        if account:
            d = _DATA_DIR / "accounts" / account
            d.mkdir(parents=True, exist_ok=True)
            return d
    except Exception:
        pass
    return _DATA_DIR

_SHARED_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0e0e10;color:#efeff1;font-family:system-ui,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:16px}
.card{background:#18181b;border:1px solid #2d2d35;border-radius:12px;padding:32px;width:100%;max-width:380px;text-align:center}
.logo{width:90px;height:90px;margin:0 auto 16px;display:block}
h1{font-size:1.2rem;margin-bottom:6px;color:#efeff1}
.subtitle{font-size:.85rem;color:#adadb8;margin-bottom:24px}
input{width:100%;background:#0e0e10;border:1px solid #3d3d4a;border-radius:8px;padding:10px 14px;color:#efeff1;font-size:.95rem;margin-bottom:12px;outline:none;text-align:left}
input:focus{border-color:#9147ff}
.btn{width:100%;border:none;border-radius:8px;padding:11px;font-size:.95rem;font-weight:600;cursor:pointer;margin-bottom:8px}
.btn-primary{background:#9147ff;color:#fff}
.btn-primary:hover{background:#7d3ce0}
.btn-ghost{background:transparent;color:#adadb8;border:1px solid #3d3d4a}
.btn-ghost:hover{background:#2d2d35}
.err{color:#eb4a4a;font-size:.85rem;margin-bottom:12px;text-align:left}
.info{color:#adadb8;font-size:.82rem;margin-bottom:12px;text-align:left;line-height:1.5}
hr{border:none;border-top:1px solid #2d2d35;margin:16px 0}
"""


def _load_web_config() -> dict:
    if _WEB_CONFIG_FILE.exists():
        try:
            return json.loads(_WEB_CONFIG_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_web_config(config: dict) -> None:
    _DATA_DIR.mkdir(exist_ok=True)
    _WEB_CONFIG_FILE.write_text(json.dumps(config, indent=2))


def _get_password() -> str:
    cfg = _load_web_config()
    # Config file takes priority over env var once setup is done
    if cfg.get("setup_done"):
        return cfg.get("password", "")
    return os.environ.get("WEB_PASSWORD", "")


def _get_bot_token() -> str:
    try:
        if _BOT_TOKEN_FILE.exists():
            return json.loads(_BOT_TOKEN_FILE.read_text()).get("token", "")
    except Exception:
        pass
    return ""


def _save_bot_token(token: str) -> None:
    _DATA_DIR.mkdir(exist_ok=True)
    _BOT_TOKEN_FILE.write_text(json.dumps({"token": token}))


def _is_setup_done() -> bool:
    return _load_web_config().get("setup_done", False)


def _vienna_today() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Europe/Vienna")).strftime("%Y-%m-%d")


def _load_daily_points() -> dict:
    try:
        p = _get_account_data_dir() / "daily_points.json"
        if p.exists():
            d = json.loads(p.read_text())
            if d.get("date") == _vienna_today():
                return d
    except Exception:
        pass
    return {"date": _vienna_today(), "total": 0}


def _save_daily_points(total: int) -> None:
    p = _get_account_data_dir() / "daily_points.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"date": _vienna_today(), "total": total}))


def _load_channel_points_history() -> dict:
    try:
        p = _get_account_data_dir() / "channel_points.json"
        if p.exists():
            return json.loads(p.read_text())
    except Exception:
        pass
    return {}


_UNPROTECTED_PATHS = {
    "/__auth_login", "/__auth_login_page",
    "/__setup", "/__setup_post",
    "/favicon.ico", "/logo.png", "/manifest.json",
    "/api/pair/claim",  # Discord bot pairing — no auth needed to exchange code
}
_UNPROTECTED_PREFIXES = ("/static/",)

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8"><title>TwitchDropsMiner</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<style>{css}</style></head>
<body><div class="card">
<img class="logo" src="/logo.png" alt="TwitchDropsMiner">
<h1>TwitchDropsMiner</h1>
<p class="subtitle">Web Interface</p>
{{error}}
<form method="POST" action="/__auth_login">
<input type="password" name="password" placeholder="Passwort" autofocus autocomplete="current-password">
<button class="btn btn-primary" type="submit">Einloggen</button>
</form></div></body></html>""".format(css=_SHARED_CSS)

_SETUP_HTML = """<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8"><title>TwitchDropsMiner – Setup</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<style>{css}</style></head>
<body><div class="card">
<img class="logo" src="/logo.png" alt="TwitchDropsMiner">
<h1>Willkommen!</h1>
<p class="subtitle">Erstmalige Einrichtung</p>
{{error}}
<p class="info">Möchtest du den Zugang mit einem Passwort schützen?<br>
Du kannst dies später in den Einstellungen ändern.</p>
<form method="POST" action="/__setup_post">
<input type="password" name="password" placeholder="Passwort festlegen (optional)" autocomplete="new-password">
<input type="password" name="password2" placeholder="Passwort wiederholen" autocomplete="new-password">
<button class="btn btn-primary" type="submit" name="action" value="set">Mit Passwort starten</button>
<hr>
<button class="btn btn-ghost" type="submit" name="action" value="skip">Ohne Passwort starten</button>
</form></div></body></html>""".format(css=_SHARED_CSS)


def _render_template(template: str, error: str = "") -> str:
    return template.replace("{error}", error)


class PasswordAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # First-time setup: redirect everything to setup page
        if not _is_setup_done() and path not in _UNPROTECTED_PATHS:
            return RedirectResponse("/__setup", status_code=302)
        # Public paths always pass through
        if path in _UNPROTECTED_PATHS or any(path.startswith(p) for p in _UNPROTECTED_PREFIXES):
            response = await call_next(request)
            if any(path.startswith(p) for p in _UNPROTECTED_PREFIXES):
                response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            return response
        # Bot token auth — allows Discord bot to use API
        if path.startswith("/api/"):
            bot_token_header = request.headers.get("X-Bot-Token", "")
            saved_bot_token = _get_bot_token()
            if saved_bot_token and secrets.compare_digest(bot_token_header, saved_bot_token):
                return await call_next(request)
        pw = _get_password()
        if not pw:
            return await call_next(request)
        session = request.cookies.get("__tdm_session", "")
        if secrets.compare_digest(session, pw):
            return await call_next(request)
        return HTMLResponse(_render_template(_LOGIN_HTML), status_code=401)


if TYPE_CHECKING:
    import uvicorn

    from src.core.client import Twitch
    from src.web.gui_manager import WebGUIManager


logger = logging.getLogger("TwitchDrops")

# Create FastAPI app
app = FastAPI(title="Twitch Drops Miner Web", version="1.0.0")

# Add auth middleware (if WEB_PASSWORD env var is set)
app.add_middleware(PasswordAuthMiddleware)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Create Socket.IO server
sio = socketio.AsyncServer(
    async_mode="asgi", cors_allowed_origins="*", logger=False, engineio_logger=False
)

# Wrap with ASGI app
socket_app = socketio.ASGIApp(sio, app)

# Global references (set by main.py)
gui_manager: WebGUIManager | None = None
twitch_client: Twitch | None = None
_server_instance: uvicorn.Server | None = None


def set_managers(gui: WebGUIManager, twitch: Twitch):
    """Called by main.py to set up references"""
    global gui_manager, twitch_client
    gui_manager = gui
    twitch_client = twitch
    gui.set_socketio(sio)


# Pydantic models for API
class LoginRequest(BaseModel):
    username: str
    password: str
    token: str = ""


class ChannelSelectRequest(BaseModel):
    channel_id: int


class SettingsUpdate(BaseModel):
    games_to_watch: list[str] | None = None
    dark_mode: bool | None = None
    language: str | None = None
    proxy: str | None = None
    connection_quality: int | None = None
    minimum_refresh_interval_minutes: int | None = None
    inventory_filters: dict | None = None
    mining_benefits: dict[str, bool] | None = None
    claim_channel_points: bool | None = None
    idle_channels: list[str] | None = None
    idle_use_followed: bool | None = None
    scheduler_enabled: bool | None = None
    scheduler_start: str | None = None
    scheduler_stop: str | None = None
    discord_webhook_drops: str | None = None
    discord_webhook_points: str | None = None
    drop_name_blacklist: list[str] | None = None


class ProxyVerifyRequest(BaseModel):
    proxy: str


class PairClaimRequest(BaseModel):
    code: str


# ==================== Auth Endpoints ====================


@app.get("/__setup", response_class=HTMLResponse)
async def setup_page():
    if _is_setup_done():
        return RedirectResponse("/", status_code=302)
    return HTMLResponse(_render_template(_SETUP_HTML))


@app.post("/__setup_post")
async def setup_post(request: Request):
    if _is_setup_done():
        return RedirectResponse("/", status_code=302)
    form = await request.form()
    action = form.get("action", "skip")
    if action == "set":
        pw = form.get("password", "")
        pw2 = form.get("password2", "")
        if not pw:
            return HTMLResponse(_render_template(_SETUP_HTML, '<p class="err">Bitte ein Passwort eingeben.</p>'))
        if pw != pw2:
            return HTMLResponse(_render_template(_SETUP_HTML, '<p class="err">Passwörter stimmen nicht überein.</p>'))
        _save_web_config({"setup_done": True, "password": pw})
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("__tdm_session", pw, httponly=True, samesite="lax", max_age=60*60*24*30)
        return resp
    else:
        _save_web_config({"setup_done": True, "password": ""})
        return RedirectResponse("/", status_code=303)


@app.post("/__auth_login")
async def auth_login_post(request: Request):
    form = await request.form()
    pw = form.get("password", "")
    current_pw = _get_password()
    if current_pw and secrets.compare_digest(pw, current_pw):
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("__tdm_session", current_pw, httponly=True, samesite="lax", max_age=60*60*24*30)
        return resp
    return HTMLResponse(_render_template(_LOGIN_HTML, '<p class="err">Falsches Passwort</p>'), status_code=401)


@app.get("/__auth_logout")
async def auth_logout():
    resp = RedirectResponse("/__auth_login_page", status_code=303)
    resp.delete_cookie("__tdm_session")
    return resp


@app.get("/__auth_login_page", response_class=HTMLResponse)
async def auth_login_page():
    return HTMLResponse(_render_template(_LOGIN_HTML))


# ==================== Static Assets ====================

_web_dir = Path(__file__).parent.parent.parent / "web"


@app.get("/favicon.ico")
async def favicon():
    return FileResponse(_web_dir / "favicon.ico", media_type="image/x-icon")


@app.get("/logo.png")
async def logo():
    return FileResponse(_web_dir / "logo.png", media_type="image/png")


@app.get("/manifest.json")
async def manifest():
    return FileResponse(_web_dir / "manifest.json", media_type="application/manifest+json")


# ==================== REST API Endpoints ====================


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    """Serve the main web interface"""
    # Web files are in project_root/web/, we're in project_root/src/web/
    web_dir = Path(__file__).parent.parent.parent / "web"
    index_file = web_dir / "index.html"
    logger.debug(
        f"Looking for web files: __file__={__file__}, web_dir={web_dir}, index_file={index_file}, exists={index_file.exists()}"
    )
    if index_file.exists():
        return FileResponse(index_file, headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        })
    return HTMLResponse(
        content=f"<h1>Twitch Drops Miner</h1><p>Web interface files not found. Please check installation.</p><p>Debug: Looking for {index_file}</p>",
        status_code=500,
    )


@app.get("/api/status")
async def get_status():
    """Get current application status"""
    if not gui_manager or not twitch_client:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    return {
        "status": gui_manager.status.get(),
        "login": gui_manager.login.get_status(),
        "manual_mode": twitch_client.get_manual_mode_info(),
        "paused": twitch_client.is_paused() if twitch_client else False,
    }


@app.post("/api/pause")
async def pause_miner():
    """Pause the miner."""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Not ready")
    twitch_client.pause(source="user")
    return {"success": True, "paused": True}


@app.post("/api/resume")
async def resume_miner():
    """Resume the miner."""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Not ready")
    twitch_client.resume(user_override=True)
    return {"success": True, "paused": False}


@app.get("/api/channels")
async def get_channels():
    """Get list of tracked channels"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    return {"channels": gui_manager.channels.get_channels()}


@app.post("/api/channels/select")
async def select_channel(request: ChannelSelectRequest):
    """Select a channel to watch"""
    if not gui_manager or not twitch_client:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    # Validate channel exists
    channel = twitch_client.channels.get(request.channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    # Validate channel has a game
    if not channel.game:
        raise HTTPException(status_code=400, detail="Channel is not playing any game")

    # Warn if channel has no drops (shouldn't happen if GUI is filtering correctly)
    if not any(campaign.can_earn(channel) for campaign in twitch_client.inventory):
        logger.warning(f"User selected channel {channel.name} but it has no available drops")

    gui_manager.select_channel(request.channel_id)

    # Trigger channel switch to apply the selection
    from src.config import State

    twitch_client.change_state(State.CHANNEL_SWITCH)

    return {"success": True}


@app.get("/api/campaigns")
async def get_campaigns():
    """Get campaign inventory"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    return {"campaigns": gui_manager.inv.get_campaigns()}


@app.get("/api/console")
async def get_console_history():
    """Get console output history"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    return {"lines": gui_manager.output.get_history()}


@app.get("/api/channel-points/{channel_login}")
async def get_channel_points(channel_login: str):
    """Fetch current channel points balance for a channel."""
    if not gui_manager or not gui_manager._twitch:
        raise HTTPException(status_code=503, detail="Not ready")
    try:
        from src.config import GQL_OPERATIONS
        resp = await gui_manager._twitch.gql_request(
            GQL_OPERATIONS["ChannelPointsContext"].with_variables({"channelLogin": channel_login})
        )
        data = resp.get("data") or {}
        try:
            points: int = data["community"]["channel"]["self"]["communityPoints"]["balance"]
        except (KeyError, TypeError):
            points = 0
        # Persist
        if points:
            from src.utils import json_load, json_save
            _cp_file = _get_account_data_dir() / "channel_points.json"
            history = json_load(_cp_file, {}, merge=False)
            history[channel_login] = points
            json_save(_cp_file, history)
        # Include last chest bonus info for Discord bot split notification
        last_chest = {}
        try:
            _chest_file = _get_account_data_dir() / "last_chest.json"
            if _chest_file.exists():
                _chest_data = json_load(_chest_file, {}, merge=False)
                last_chest = _chest_data.get(channel_login, {})
        except Exception:
            pass
        return {"channel": channel_login, "balance": points, "last_chest": last_chest}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



@app.get("/api/drops-history")
async def get_drops_history():
    """Return all claimed drops history."""
    hist_file = _get_account_data_dir() / "drops_history.json"
    try:
        if hist_file.exists():
            return json.loads(hist_file.read_text())
    except Exception:
        pass
    return []

@app.get("/api/idle-watch/status")
async def idle_watch_status():
    """Check if the current idle channel is online."""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Not ready")
    ch = twitch_client.watching_channel.get_with_default(None)
    if ch is None:
        return {"watching": None, "online": False}
    # Re-fetch stream info to get current status
    try:
        from src.config import GQL_OPERATIONS
        resp = await twitch_client.gql_request(
            GQL_OPERATIONS["GetStreamInfo"].with_variables({"channel": ch._login})
        )
        user_data = resp["data"]["user"]
        online = bool(user_data and user_data.get("stream"))
        return {"watching": ch._login, "online": online, "display_name": ch.name}
    except Exception:
        return {"watching": ch._login, "online": ch.online}


@app.post("/api/idle-watch/switch")
async def idle_watch_switch():
    """Switch to the next idle channel (manual list or followed)."""
    if not gui_manager or not twitch_client:
        raise HTTPException(status_code=503, detail="Not ready")

    # Build candidate list: manual channels first, then followed live channels
    candidates: list[str] = list(twitch_client.settings.idle_channels)
    if twitch_client.settings.idle_use_followed:
        followed = await twitch_client._fetch_followed_live_logins()
        seen = set(candidates)
        for login in followed:
            if login not in seen:
                candidates.append(login)
                seen.add(login)

    if not candidates:
        raise HTTPException(status_code=400, detail="No idle channels configured")

    current = twitch_client.watching_channel.get_with_default(None)
    current_login = current._login if current else None
    idx = candidates.index(current_login) if current_login in candidates else -1

    # Try channels starting after the current one, wrapping around
    for offset in range(1, len(candidates) + 1):
        next_login = candidates[(idx + offset) % len(candidates)]
        channel = await twitch_client._fetch_idle_channel_by_login(next_login)
        if channel is not None:
            twitch_client.gui.clear_drop()
            twitch_client.watch(channel, update_status=False)
            twitch_client.gui.status.update(f"💤 Idle watching: {channel.name}")
            return {"switched_to": next_login}

    raise HTTPException(status_code=404, detail="No other idle channels are online")


@app.get("/api/settings")
async def get_settings():
    """Get current settings"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")
    result = gui_manager.settings.get_settings()
    result["bot_paired"] = bool(_get_bot_token())
    return result


@app.get("/api/languages")
async def get_languages():
    """Get available languages"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    return gui_manager.settings.get_languages()


@app.get("/api/translations")
async def get_translations():
    """Get translations for current language"""
    from src.i18n.translator import _

    # Return the full Translation object
    return _.t


@app.post("/api/settings")
async def update_settings(settings: SettingsUpdate):
    """Update application settings"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    settings_dict = settings.dict(exclude_unset=True)
    gui_manager.settings.update_settings(settings_dict)
    return {"success": True, "settings": gui_manager.settings.get_settings()}


@app.post("/api/settings/verify-proxy")
async def verify_proxy(request: ProxyVerifyRequest):
    """Verify proxy connectivity"""
    import time

    import aiohttp

    proxy_url = request.proxy.strip()
    if not proxy_url:
        return {"success": False, "message": "Proxy URL is empty"}

    try:
        start_time = time.time()
        # Test connection to Twitch
        async with (
            aiohttp.ClientSession() as session,
            session.get("https://www.twitch.tv", proxy=proxy_url, timeout=10) as response,
        ):
            # Just checking if we can connect and get a response
            if response.status < 500:
                latency = round((time.time() - start_time) * 1000)
                return {
                    "success": True,
                    "message": f"Connected! ({latency}ms)",
                    "latency": latency,
                }
            else:
                return {
                    "success": False,
                    "message": f"Proxy reachable but returned {response.status}",
                }
    except Exception as e:
        return {"success": False, "message": f"Connection failed: {str(e)}"}


@app.post("/api/settings/test-webhook")
async def test_webhook(request: Request):
    """Send a test message to a Discord webhook URL"""
    import aiohttp
    body = await request.json()
    url = body.get("url", "").strip()
    if not url:
        return {"success": False, "message": "No URL provided"}
    try:
        async with aiohttp.ClientSession() as session:
            resp = await session.post(url, json={
                "embeds": [{
                    "title": "✅ Webhook Test",
                    "description": "TwitchDropsMiner webhook is working!",
                    "color": 0x9147ff,
                }]
            }, timeout=aiohttp.ClientTimeout(total=10))
            if resp.status in (200, 204):
                return {"success": True, "message": "Sent!"}
            return {"success": False, "message": f"HTTP {resp.status}"}
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.get("/api/version")
async def get_version():
    """Get current application version and check for updates"""
    import aiohttp

    from src.version import __version__

    current_version = __version__
    latest_version = None
    update_available = False
    download_url = None

    try:
        # Check GitHub API for latest release
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                "https://api.github.com/repos/SimpliAj/twitchdropsminer/releases/latest", timeout=5
            ) as response,
        ):
            if response.status == 200:
                data = await response.json()
                latest_version = data.get("tag_name", "").lstrip("v")
                download_url = data.get("html_url")

                # Compare versions (simple string comparison works for semantic versioning)
                if latest_version and latest_version > current_version:
                    update_available = True
    except Exception as e:
        logger.warning(f"Failed to check for updates: {str(e)}")

    return {
        "current_version": current_version,
        "latest_version": latest_version,
        "update_available": update_available,
        "download_url": download_url or "https://github.com/SimpliAj/twitchdropsminer/releases",
    }


@app.get("/api/wanted-items")
async def get_wanted_items_http():
    if not gui_manager:
        raise HTTPException(status_code=503, detail="Not initialized")
    return {"wanted_items": gui_manager.get_wanted_game_tree()}


@app.post("/api/login")
async def submit_login(login_data: LoginRequest):
    """Submit login credentials"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    gui_manager.login.submit_login(login_data.username, login_data.password, login_data.token)
    return {"success": True}


@app.post("/api/oauth/confirm")
async def confirm_oauth():
    """Confirm OAuth code has been entered by user"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    # Just set the event to signal the user has acknowledged the code
    gui_manager.login._login_event.set()
    return {"success": True}


@app.post("/api/reload")
async def trigger_reload():
    """Trigger application reload"""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Twitch client not initialized")

    from src.config import State

    twitch_client.change_state(State.INVENTORY_FETCH)
    return {"success": True}


@app.post("/api/close")
async def trigger_close():
    """Trigger application shutdown"""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Twitch client not initialized")

    twitch_client.close()
    return {"success": True}


@app.post("/api/restart")
async def trigger_restart():
    """Restart via PM2 (stop + auto-restart by PM2)"""
    import subprocess

    async def _restart():
        await asyncio.sleep(1)
        subprocess.Popen(["pm2", "restart", "twitchdrops"])

    asyncio.create_task(_restart())
    return {"success": True}


@app.post("/api/skip-game")
async def skip_game():
    """Skip current game and switch to a different game"""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Twitch client not initialized")

    twitch_client.skip_current_game()
    return {"success": True}


@app.post("/api/mode/exit-manual")
async def exit_manual_mode():
    """Exit manual mode and return to automatic channel selection"""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Twitch client not initialized")

    if not twitch_client.is_manual_mode():
        return {"success": False, "message": "Not in manual mode"}

    twitch_client.exit_manual_mode("User requested")
    return {"success": True}


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


class PasswordDisableRequest(BaseModel):
    current_password: str


@app.post("/api/auth/change-password")
async def change_password(data: PasswordChangeRequest, request: Request):
    current_pw = _get_password()
    if current_pw and not secrets.compare_digest(data.current_password, current_pw):
        raise HTTPException(status_code=403, detail="Aktuelles Passwort falsch")
    cfg = _load_web_config()
    cfg["password"] = data.new_password
    cfg["setup_done"] = True
    _save_web_config(cfg)
    resp_data = {"success": True}
    # Update session cookie to new password
    from fastapi.responses import JSONResponse
    response = JSONResponse(resp_data)
    if data.new_password:
        response.set_cookie("__tdm_session", data.new_password, httponly=True, samesite="lax", max_age=60*60*24*30)
    else:
        response.delete_cookie("__tdm_session")
    return response


@app.post("/api/auth/disable-password")
async def disable_password(data: PasswordDisableRequest):
    current_pw = _get_password()
    if current_pw and not secrets.compare_digest(data.current_password, current_pw):
        raise HTTPException(status_code=403, detail="Aktuelles Passwort falsch")
    cfg = _load_web_config()
    cfg["password"] = ""
    cfg["setup_done"] = True
    _save_web_config(cfg)
    from fastapi.responses import JSONResponse
    response = JSONResponse({"success": True})
    response.delete_cookie("__tdm_session")
    return response


@app.get("/api/auth/status")
async def auth_status():
    return {"password_set": bool(_get_password())}


# ==================== Discord Bot Pairing ====================


@app.post("/api/pair/generate")
async def pair_generate():
    """Generate a one-time pairing code for Discord bot."""
    import time
    code = "DROPS-" + secrets.token_hex(4).upper()
    token = secrets.token_hex(32)
    _pair_codes[code] = {"token": token, "expires": time.time() + 600}
    # Clean expired codes
    now = time.time()
    expired = [k for k, v in _pair_codes.items() if v["expires"] < now]
    for k in expired:
        del _pair_codes[k]
    return {"code": code, "expires_in": 600}


@app.post("/api/pair/claim")
async def pair_claim(req: PairClaimRequest):
    """Exchange pairing code for permanent bot token. No auth required."""
    import time
    entry = _pair_codes.get(req.code)
    if not entry or entry["expires"] < time.time():
        raise HTTPException(status_code=404, detail="Invalid or expired code")
    token = entry["token"]
    _save_bot_token(token)
    del _pair_codes[req.code]
    return {"token": token}


@app.get("/api/pair/status")
async def pair_status():
    """Check if a bot token is configured."""
    return {"paired": bool(_get_bot_token())}


@app.delete("/api/pair/revoke")
async def pair_revoke():
    """Revoke bot token."""
    if _BOT_TOKEN_FILE.exists():
        _BOT_TOKEN_FILE.unlink()
    return {"success": True}


def _get_bot_pairing() -> dict | None:
    """Find the pairings.json entry whose token matches the stored bot token."""
    token = _get_bot_token()
    if not token or not _PAIRINGS_FILE.exists():
        return None
    try:
        data = json.loads(_PAIRINGS_FILE.read_text())
        for uid, entry in data.get("users", {}).items():
            if entry.get("token") == token:
                return {"discord_user_id": uid, **entry}
    except Exception:
        pass
    return None


def _save_pairing_channels(discord_user_id: str, channels: dict) -> None:
    if not _PAIRINGS_FILE.exists():
        return
    data = json.loads(_PAIRINGS_FILE.read_text())
    if discord_user_id in data.get("users", {}):
        data["users"][discord_user_id]["channels"] = channels
        _PAIRINGS_FILE.write_text(json.dumps(data, indent=2))


def _save_pairing_field(discord_user_id: str, field: str, value) -> None:
    if not _PAIRINGS_FILE.exists():
        return
    data = json.loads(_PAIRINGS_FILE.read_text())
    if discord_user_id in data.get("users", {}):
        if value is None:
            data["users"][discord_user_id].pop(field, None)
        else:
            data["users"][discord_user_id][field] = value
        _PAIRINGS_FILE.write_text(json.dumps(data, indent=2))


def _normalize_channel_entries(val) -> list[dict]:
    """Normalize a channel field (int, dict, or list of either) to list of {id,name,guild}."""
    if val is None:
        return []
    if not isinstance(val, list):
        val = [val]
    result = []
    for v in val:
        if v is None:
            continue
        if isinstance(v, dict):
            result.append({"id": str(v["id"]), "name": v.get("name", ""), "guild": v.get("guild", "")})
        else:
            result.append({"id": str(v), "name": "", "guild": ""})
    return result


@app.get("/api/discord-bot/config")
async def discord_bot_config():
    """Return channel configuration for the paired Discord bot user."""
    pairing = _get_bot_pairing()
    if not pairing:
        return {"paired": False, "channels": {}}
    channels = pairing.get("channels", {})
    return {
        "paired": True,
        "discord_user_id": pairing["discord_user_id"],
        "profile_name": pairing.get("profile_name", ""),
        "channels": {
            "drops": _normalize_channel_entries(channels.get("drops")),
            "points": _normalize_channel_entries(channels.get("points")),
        },
    }


@app.post("/api/daily-points")
async def update_daily_points(request: Request):
    body = await request.json()
    total = int(body.get("total", 0))
    _save_daily_points(max(0, total))
    return {"success": True}


@app.post("/api/discord-bot/profile-name")
async def discord_bot_set_profile_name(request: Request):
    body = await request.json()
    name = (body.get("name") or "").strip()[:50]
    pairing = _get_bot_pairing()
    if not pairing:
        raise HTTPException(status_code=404, detail="No bot paired")
    _save_pairing_field(pairing["discord_user_id"], "profile_name", name or None)
    return {"success": True, "profile_name": name}


@app.delete("/api/discord-bot/config/channel/{channel_type}")
async def discord_bot_clear_channel(channel_type: str, channel_id: str | None = None):
    """Remove a specific channel (by channel_id) or all channels of a type."""
    if channel_type not in ("drops", "points"):
        raise HTTPException(status_code=400, detail="Invalid channel type")
    pairing = _get_bot_pairing()
    if not pairing:
        raise HTTPException(status_code=404, detail="No bot paired")
    channels = dict(pairing.get("channels", {}))
    if channel_id:
        current = _normalize_channel_entries(channels.get(channel_type))
        channels[channel_type] = [e for e in current if e["id"] != channel_id]
    else:
        channels[channel_type] = []
    _save_pairing_channels(pairing["discord_user_id"], channels)
    return {"success": True}


# ==================== Account Management ====================

@app.get("/api/accounts")
async def list_accounts():
    """List all saved accounts and the currently active one"""
    cfg = _load_web_config()
    active = cfg.get("active_account", "")
    accounts_dir = _DATA_DIR / "accounts"
    accounts = []
    if accounts_dir.exists():
        for d in sorted(accounts_dir.iterdir()):
            if d.is_dir():
                has_cookies = (d / "cookies.jar").exists()
                accounts.append({"label": d.name, "active": d.name == active, "has_cookies": has_cookies})
    return {"accounts": accounts, "active": active}


class AccountSwitchRequest(BaseModel):
    label: str


@app.post("/api/accounts/switch")
async def switch_account(data: AccountSwitchRequest):
    """Switch active account and restart"""
    cfg = _load_web_config()
    account_dir = _DATA_DIR / "accounts" / data.label
    if not account_dir.exists():
        raise HTTPException(status_code=404, detail="Account not found")
    cfg["active_account"] = data.label
    _save_web_config(cfg)
    pairing = _get_bot_pairing()
    if pairing:
        _save_pairing_field(pairing["discord_user_id"], "profile_name", data.label)
    async def _restart():
        await asyncio.sleep(1)
        import subprocess
        subprocess.Popen(["pm2", "restart", "twitchdrops"])
    asyncio.create_task(_restart())
    return {"success": True}


class AccountAddRequest(BaseModel):
    label: str


@app.post("/api/accounts/add")
async def add_account(data: AccountAddRequest):
    """Create a new account slot, switch to it, and restart for fresh login"""
    label = data.label.strip()
    if not label or any(c in label for c in "/\\."):
        raise HTTPException(status_code=400, detail="Invalid account label")
    account_dir = _DATA_DIR / "accounts" / label
    if account_dir.exists():
        raise HTTPException(status_code=409, detail="Account label already exists")
    account_dir.mkdir(parents=True, exist_ok=True)
    cfg = _load_web_config()
    cfg["active_account"] = label
    _save_web_config(cfg)
    pairing = _get_bot_pairing()
    if pairing:
        _save_pairing_field(pairing["discord_user_id"], "profile_name", label)
    async def _restart():
        await asyncio.sleep(1)
        import subprocess
        subprocess.Popen(["pm2", "restart", "twitchdrops"])
    asyncio.create_task(_restart())
    return {"success": True}


@app.delete("/api/accounts/{label}")
async def remove_account(label: str):
    """Delete an account (cannot delete the active account)"""
    import shutil
    cfg = _load_web_config()
    if cfg.get("active_account") == label:
        raise HTTPException(status_code=400, detail="Cannot delete the active account. Switch first.")
    account_dir = _DATA_DIR / "accounts" / label
    if not account_dir.exists():
        raise HTTPException(status_code=404, detail="Account not found")
    shutil.rmtree(account_dir)
    return {"success": True}


@app.patch("/api/accounts/{label}")
async def rename_account(label: str, request: Request):
    """Rename an account label (renames the folder, updates active_account and bot profile_name)"""
    import shutil
    body = await request.json()
    new_label = (body.get("new_label") or "").strip()
    if not new_label or any(c in new_label for c in "/\\."):
        raise HTTPException(status_code=400, detail="Invalid account label")
    account_dir = _DATA_DIR / "accounts" / label
    if not account_dir.exists():
        raise HTTPException(status_code=404, detail="Account not found")
    new_dir = _DATA_DIR / "accounts" / new_label
    if new_dir.exists():
        raise HTTPException(status_code=409, detail="Account label already exists")
    shutil.move(str(account_dir), str(new_dir))
    cfg = _load_web_config()
    was_active = cfg.get("active_account") == label
    if was_active:
        cfg["active_account"] = new_label
        _save_web_config(cfg)
        pairing = _get_bot_pairing()
        if pairing:
            _save_pairing_field(pairing["discord_user_id"], "profile_name", new_label)
    return {"success": True, "new_label": new_label}


@app.get("/api/accounts/migration-hint")
async def migration_hint():
    """Returns hint if legacy cookies exist but no accounts are configured"""
    legacy_cookies = (_DATA_DIR / "cookies.jar").exists()
    cfg = _load_web_config()
    has_active = bool(cfg.get("active_account"))
    return {"has_legacy": legacy_cookies and not has_active, "migrated": has_active}


# ==================== Socket.IO Events ====================


@sio.event
async def connect(sid, environ):
    """Client connected"""
    logger.info(f"Web client connected: {sid}")

    # Send initial state to new client
    if gui_manager and twitch_client:
        await sio.emit(
            "initial_state",
            {
                "status": gui_manager.status.get(),
                "channels": gui_manager.channels.get_channels(),
                "campaigns": gui_manager.inv.get_campaigns(),
                "console": gui_manager.output.get_history(),
                "settings": gui_manager.settings.get_settings(),
                "login": gui_manager.login.get_status(),
                "manual_mode": twitch_client.get_manual_mode_info(),
                "current_drop": gui_manager.progress.get_current_drop(),
                "wanted_items": gui_manager.get_wanted_game_tree(),
                "watching_channel": (
                    {
                        "id": ch.id,
                        "login": ch._login,
                    }
                    if (ch := twitch_client.watching_channel.get_with_default(None)) is not None
                    else None
                ),
                "channel_points_history": _load_channel_points_history(),
                "daily_points": _load_daily_points(),
            },
            room=sid,
        )


@sio.event
async def disconnect(sid):
    """Client disconnected"""
    logger.info(f"Web client disconnected: {sid}")


@sio.event
async def request_login(sid):
    """Client requested login form submission"""
    logger.info(f"Login request from client: {sid}")
    # The actual login data comes via REST API


@sio.event
async def request_reload(sid):
    """Client requested application reload"""
    if twitch_client:
        from src.config import State

        twitch_client.change_state(State.INVENTORY_FETCH)


@sio.event
async def get_wanted_items(sid):
    """Client requested wanted items list"""
    if gui_manager:
        await sio.emit("wanted_items_update", gui_manager.get_wanted_game_tree(), to=sid)


# Serve app.js with no-cache so PWA always gets the latest version
@app.get("/static/app.js")
async def serve_app_js():
    web_dir_path = Path(__file__).parent.parent.parent / "web"
    js_file = web_dir_path / "static" / "app.js"
    return FileResponse(js_file, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Content-Type": "application/javascript",
    })


# Mount static files (CSS, JS, images)
# Web files are in project_root/web/, we're in project_root/src/web/
web_dir = Path(__file__).parent.parent.parent / "web"
if web_dir.exists():
    static_dir = web_dir / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")


# Development server runner
async def run_server(host: str = "0.0.0.0", port: int = 8080):
    """Run the web server (used for development/testing)"""
    global _server_instance
    import uvicorn

    config = uvicorn.Config(socket_app, host=host, port=port, log_level="info", access_log=False)
    server = uvicorn.Server(config)
    _server_instance = server
    try:
        await server.serve()
    finally:
        _server_instance = None


async def shutdown_server():
    """Gracefully shutdown the web server"""
    if _server_instance:
        logger.info("Setting server.should_exit = True")
        _server_instance.should_exit = True
        # Give the server a moment to process the shutdown signal
        # The uvicorn server checks should_exit periodically
        await asyncio.sleep(0.1)


if __name__ == "__main__":
    # For standalone testing
    asyncio.run(run_server())
