from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import json
import os
import secrets
import shutil
import subprocess
import sys

import socketio
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

import os as _os
_DATA_DIR = Path(_os.environ.get("TDM_DATA_DIR", str(Path(__file__).parent.parent.parent / "data")))
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
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@600;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
body{background:radial-gradient(900px 420px at 15% -10%,rgba(145,70,255,.10),transparent 60%),radial-gradient(700px 360px at 100% 0%,rgba(18,128,95,.08),transparent 55%),#0B0D12;color:#ECEEF3;font-family:'IBM Plex Sans',system-ui,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:16px}
.card{background:#12151D;border:1px solid #262B38;border-radius:16px;padding:32px;width:100%;max-width:380px;text-align:center;box-shadow:0 20px 48px -12px rgba(0,0,0,.65)}
.logo{width:84px;height:84px;margin:0 auto 16px;display:block;border-radius:12px;box-shadow:0 0 0 1px #262B38}
h1{font-family:'Space Grotesk',system-ui,sans-serif;font-size:1.2rem;font-weight:700;margin-bottom:6px;color:#ECEEF3}
.subtitle{font-size:.85rem;color:#8991A6;margin-bottom:24px}
input{width:100%;background:#0B0D12;border:1px solid #333A4A;border-radius:8px;padding:10px 14px;color:#ECEEF3;font-family:'IBM Plex Sans',system-ui,sans-serif;font-size:.95rem;margin-bottom:12px;outline:none;text-align:left;transition:border-color .15s}
input:focus{border-color:#9146FF}
.btn{width:100%;border:none;border-radius:8px;padding:11px;font-family:'Space Grotesk',system-ui,sans-serif;font-size:.95rem;font-weight:600;cursor:pointer;margin-bottom:8px;transition:filter .15s,background .15s}
.btn-primary{background:#9146FF;color:#fff}
.btn-primary:hover{filter:brightness(1.1)}
.btn-ghost{background:transparent;color:#8991A6;border:1px solid #333A4A}
.btn-ghost:hover{background:#1B2029}
.err{color:#FF6B81;font-size:.85rem;margin-bottom:12px;text-align:left}
.info{color:#8991A6;font-size:.82rem;margin-bottom:12px;text-align:left;line-height:1.5}
hr{border:none;border-top:1px solid #262B38;margin:16px 0}
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


def _get_push_config() -> dict:
    cfg = _load_web_config()
    return {
        "push_notifications_enabled": cfg.get("push_notifications_enabled", False),
        "push_sound_enabled": cfg.get("push_sound_enabled", True),
        "campaign_end_alerts_enabled": cfg.get("campaign_end_alerts_enabled", True),
    }


def _aggregate_stats() -> dict:
    from collections import defaultdict
    hist_file = _get_account_data_dir() / "drops_history.json"
    if not hist_file.exists():
        return {"total_claims": 0, "by_game": [], "by_day": [], "recent": []}
    try:
        history: list[dict] = json.loads(hist_file.read_text())
    except Exception:
        return {"total_claims": 0, "by_game": [], "by_day": [], "recent": []}

    by_game: dict[str, int] = defaultdict(int)
    by_day: dict[str, int] = defaultdict(int)

    for entry in history:
        by_game[entry.get("game", "Unknown")] += 1
        ts = entry.get("timestamp", "")
        if ts:
            day = ts[:10]  # "YYYY-MM-DD"
            by_day[day] += 1

    sorted_games = sorted(by_game.items(), key=lambda x: x[1], reverse=True)
    sorted_days = sorted(by_day.items())

    recent = [e for e in history if e.get("image_url")][:10]
    if len(recent) < 10:
        seen = {e.get("reward") for e in recent}
        for e in history:
            if e.get("reward") not in seen and len(recent) < 10:
                recent.append(e)
                seen.add(e.get("reward"))

    from src.services.drop_history import get_total_claims

    return {
        "total_claims": get_total_claims(_get_account_data_dir()),
        "by_game": [{"game": g, "count": c} for g, c in sorted_games[:10]],
        "by_day": [{"date": d, "count": c} for d, c in sorted_days[-365:]],
        "recent": recent[:10],
    }


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
            from src.utils import merge_case_variant_keys
            return merge_case_variant_keys(json.loads(p.read_text()))
    except Exception:
        pass
    return {}


_UNPROTECTED_PATHS = {
    "/__auth_login", "/__auth_login_page",
    "/__setup", "/__setup_post",
    "/favicon.ico", "/logo.png", "/manifest.json",
    "/api/pair/claim",  # Discord bot pairing — no auth needed to exchange code
    "/api/instance",   # Instance info for switcher — public
    "/api/instances",  # Instances registry — public
    "/healthz",  # Docker/orchestrator liveness probe — must pass with no session
}
_UNPROTECTED_PREFIXES = ("/static/",)

# Password-gate strings, keyed by the same language_name used by src/i18n's lang/*.json
# (self-contained instead of another lang/*.json section — this handful of short strings
# doesn't need the full translator machinery, and avoids repeating the missing-key crash
# class that hit lang/Indonesian.json). Falls back to English for any language not listed.
_WEB_AUTH_STRINGS: dict[str, dict[str, str]] = {
    "English": dict(html_lang="en", subtitle="Web Interface", password="Password", login_button="Login",
        wrong_password="Wrong password", welcome="Welcome!", setup_subtitle="First-time setup",
        setup_info="Do you want to protect access with a password?<br>You can change this later in settings.",
        password_optional="Set password (optional)", password_repeat="Repeat password",
        start_with_password="Start with password", start_without_password="Start without password",
        error_password_required="Please enter a password.", error_password_mismatch="Passwords do not match."),
    "Deutsch": dict(html_lang="de", subtitle="Web Interface", password="Passwort", login_button="Einloggen",
        wrong_password="Falsches Passwort", welcome="Willkommen!", setup_subtitle="Erstmalige Einrichtung",
        setup_info="Möchtest du den Zugang mit einem Passwort schützen?<br>Du kannst dies später in den Einstellungen ändern.",
        password_optional="Passwort festlegen (optional)", password_repeat="Passwort wiederholen",
        start_with_password="Mit Passwort starten", start_without_password="Ohne Passwort starten",
        error_password_required="Bitte ein Passwort eingeben.", error_password_mismatch="Passwörter stimmen nicht überein."),
    "Español": dict(html_lang="es", subtitle="Interfaz Web", password="Contraseña", login_button="Iniciar sesión",
        wrong_password="Contraseña incorrecta", welcome="¡Bienvenido!", setup_subtitle="Configuración inicial",
        setup_info="¿Quieres proteger el acceso con una contraseña?<br>Puedes cambiarlo más tarde en ajustes.",
        password_optional="Establecer contraseña (opcional)", password_repeat="Repetir contraseña",
        start_with_password="Iniciar con contraseña", start_without_password="Iniciar sin contraseña",
        error_password_required="Introduce una contraseña.", error_password_mismatch="Las contraseñas no coinciden."),
    "Français": dict(html_lang="fr", subtitle="Interface Web", password="Mot de passe", login_button="Connexion",
        wrong_password="Mot de passe incorrect", welcome="Bienvenue !", setup_subtitle="Configuration initiale",
        setup_info="Voulez-vous protéger l'accès par un mot de passe ?<br>Vous pourrez le modifier plus tard dans les paramètres.",
        password_optional="Définir un mot de passe (optionnel)", password_repeat="Répéter le mot de passe",
        start_with_password="Démarrer avec mot de passe", start_without_password="Démarrer sans mot de passe",
        error_password_required="Veuillez saisir un mot de passe.", error_password_mismatch="Les mots de passe ne correspondent pas."),
    "Italiano": dict(html_lang="it", subtitle="Interfaccia Web", password="Password", login_button="Accedi",
        wrong_password="Password errata", welcome="Benvenuto!", setup_subtitle="Configurazione iniziale",
        setup_info="Vuoi proteggere l'accesso con una password?<br>Puoi cambiarlo più tardi nelle impostazioni.",
        password_optional="Imposta password (opzionale)", password_repeat="Ripeti password",
        start_with_password="Avvia con password", start_without_password="Avvia senza password",
        error_password_required="Inserisci una password.", error_password_mismatch="Le password non coincidono."),
    "Português": dict(html_lang="pt", subtitle="Interface Web", password="Senha", login_button="Entrar",
        wrong_password="Senha incorreta", welcome="Bem-vindo!", setup_subtitle="Configuração inicial",
        setup_info="Deseja proteger o acesso com uma senha?<br>Você pode alterar isso depois nas configurações.",
        password_optional="Definir senha (opcional)", password_repeat="Repetir senha",
        start_with_password="Iniciar com senha", start_without_password="Iniciar sem senha",
        error_password_required="Digite uma senha.", error_password_mismatch="As senhas não coincidem."),
    "Polski": dict(html_lang="pl", subtitle="Interfejs WWW", password="Hasło", login_button="Zaloguj",
        wrong_password="Nieprawidłowe hasło", welcome="Witaj!", setup_subtitle="Pierwsza konfiguracja",
        setup_info="Czy chcesz zabezpieczyć dostęp hasłem?<br>Możesz to zmienić później w ustawieniach.",
        password_optional="Ustaw hasło (opcjonalnie)", password_repeat="Powtórz hasło",
        start_with_password="Uruchom z hasłem", start_without_password="Uruchom bez hasła",
        error_password_required="Podaj hasło.", error_password_mismatch="Hasła nie są zgodne."),
    "Nederlandse": dict(html_lang="nl", subtitle="Webinterface", password="Wachtwoord", login_button="Inloggen",
        wrong_password="Onjuist wachtwoord", welcome="Welkom!", setup_subtitle="Eerste installatie",
        setup_info="Wil je de toegang met een wachtwoord beveiligen?<br>Je kunt dit later wijzigen in de instellingen.",
        password_optional="Wachtwoord instellen (optioneel)", password_repeat="Wachtwoord herhalen",
        start_with_password="Starten met wachtwoord", start_without_password="Starten zonder wachtwoord",
        error_password_required="Voer een wachtwoord in.", error_password_mismatch="Wachtwoorden komen niet overeen."),
    "Dansk": dict(html_lang="da", subtitle="Webgrænseflade", password="Adgangskode", login_button="Log ind",
        wrong_password="Forkert adgangskode", welcome="Velkommen!", setup_subtitle="Førstegangsopsætning",
        setup_info="Vil du beskytte adgangen med en adgangskode?<br>Du kan ændre det senere i indstillingerne.",
        password_optional="Angiv adgangskode (valgfrit)", password_repeat="Gentag adgangskode",
        start_with_password="Start med adgangskode", start_without_password="Start uden adgangskode",
        error_password_required="Indtast en adgangskode.", error_password_mismatch="Adgangskoderne stemmer ikke overens."),
    "Čeština": dict(html_lang="cs", subtitle="Webové rozhraní", password="Heslo", login_button="Přihlásit",
        wrong_password="Nesprávné heslo", welcome="Vítejte!", setup_subtitle="První nastavení",
        setup_info="Chcete přístup chránit heslem?<br>Toto můžete později změnit v nastavení.",
        password_optional="Nastavit heslo (volitelné)", password_repeat="Zopakovat heslo",
        start_with_password="Spustit s heslem", start_without_password="Spustit bez hesla",
        error_password_required="Zadejte heslo.", error_password_mismatch="Hesla se neshodují."),
    "Türkçe": dict(html_lang="tr", subtitle="Web Arayüzü", password="Şifre", login_button="Giriş Yap",
        wrong_password="Yanlış şifre", welcome="Hoş geldiniz!", setup_subtitle="İlk kurulum",
        setup_info="Erişimi bir şifreyle korumak ister misiniz?<br>Bunu daha sonra ayarlardan değiştirebilirsiniz.",
        password_optional="Şifre belirle (isteğe bağlı)", password_repeat="Şifreyi tekrarla",
        start_with_password="Şifre ile başlat", start_without_password="Şifresiz başlat",
        error_password_required="Lütfen bir şifre girin.", error_password_mismatch="Şifreler eşleşmiyor."),
    "Română": dict(html_lang="ro", subtitle="Interfață Web", password="Parolă", login_button="Autentificare",
        wrong_password="Parolă greșită", welcome="Bine ai venit!", setup_subtitle="Configurare inițială",
        setup_info="Dorești să protejezi accesul cu o parolă?<br>Poți schimba asta mai târziu din setări.",
        password_optional="Setează parolă (opțional)", password_repeat="Repetă parola",
        start_with_password="Pornește cu parolă", start_without_password="Pornește fără parolă",
        error_password_required="Introdu o parolă.", error_password_mismatch="Parolele nu coincid."),
    "Русский": dict(html_lang="ru", subtitle="Веб-интерфейс", password="Пароль", login_button="Войти",
        wrong_password="Неверный пароль", welcome="Добро пожаловать!", setup_subtitle="Первоначальная настройка",
        setup_info="Хотите защитить доступ паролем?<br>Это можно изменить позже в настройках.",
        password_optional="Задать пароль (необязательно)", password_repeat="Повторите пароль",
        start_with_password="Начать с паролем", start_without_password="Начать без пароля",
        error_password_required="Введите пароль.", error_password_mismatch="Пароли не совпадают."),
    "Українська": dict(html_lang="uk", subtitle="Веб-інтерфейс", password="Пароль", login_button="Увійти",
        wrong_password="Невірний пароль", welcome="Ласкаво просимо!", setup_subtitle="Початкове налаштування",
        setup_info="Бажаєте захистити доступ паролем?<br>Це можна змінити пізніше в налаштуваннях.",
        password_optional="Встановити пароль (необов'язково)", password_repeat="Повторіть пароль",
        start_with_password="Почати з паролем", start_without_password="Почати без пароля",
        error_password_required="Введіть пароль.", error_password_mismatch="Паролі не збігаються."),
    "العربية": dict(html_lang="ar", subtitle="واجهة الويب", password="كلمة المرور", login_button="تسجيل الدخول",
        wrong_password="كلمة مرور خاطئة", welcome="مرحبًا!", setup_subtitle="الإعداد الأول",
        setup_info="هل تريد حماية الوصول بكلمة مرور؟<br>يمكنك تغيير ذلك لاحقًا في الإعدادات.",
        password_optional="تعيين كلمة مرور (اختياري)", password_repeat="تكرار كلمة المرور",
        start_with_password="البدء بكلمة مرور", start_without_password="البدء بدون كلمة مرور",
        error_password_required="الرجاء إدخال كلمة مرور.", error_password_mismatch="كلمتا المرور غير متطابقتين."),
    "Indonesian": dict(html_lang="id", subtitle="Antarmuka Web", password="Kata sandi", login_button="Masuk",
        wrong_password="Kata sandi salah", welcome="Selamat datang!", setup_subtitle="Pengaturan awal",
        setup_info="Ingin melindungi akses dengan kata sandi?<br>Anda dapat mengubahnya nanti di pengaturan.",
        password_optional="Atur kata sandi (opsional)", password_repeat="Ulangi kata sandi",
        start_with_password="Mulai dengan kata sandi", start_without_password="Mulai tanpa kata sandi",
        error_password_required="Silakan masukkan kata sandi.", error_password_mismatch="Kata sandi tidak cocok."),
    "日本語": dict(html_lang="ja", subtitle="ウェブインターフェース", password="パスワード", login_button="ログイン",
        wrong_password="パスワードが違います", welcome="ようこそ!", setup_subtitle="初回セットアップ",
        setup_info="パスワードでアクセスを保護しますか？<br>これは後で設定から変更できます。",
        password_optional="パスワードを設定（任意）", password_repeat="パスワードを再入力",
        start_with_password="パスワード付きで開始", start_without_password="パスワードなしで開始",
        error_password_required="パスワードを入力してください。", error_password_mismatch="パスワードが一致しません。"),
    "简体中文": dict(html_lang="zh-Hans", subtitle="网页界面", password="密码", login_button="登录",
        wrong_password="密码错误", welcome="欢迎！", setup_subtitle="首次设置",
        setup_info="是否要使用密码保护访问？<br>您可以稍后在设置中更改此项。",
        password_optional="设置密码（可选）", password_repeat="重复密码",
        start_with_password="使用密码启动", start_without_password="不使用密码启动",
        error_password_required="请输入密码。", error_password_mismatch="两次输入的密码不一致。"),
    "繁體中文": dict(html_lang="zh-Hant", subtitle="網頁介面", password="密碼", login_button="登入",
        wrong_password="密碼錯誤", welcome="歡迎！", setup_subtitle="首次設定",
        setup_info="是否要使用密碼保護存取？<br>您可以稍後在設定中變更此項。",
        password_optional="設定密碼（選填）", password_repeat="重複密碼",
        start_with_password="使用密碼啟動", start_without_password="不使用密碼啟動",
        error_password_required="請輸入密碼。", error_password_mismatch="兩次輸入的密碼不一致。"),
}


def _web_auth_strings() -> dict[str, str]:
    """Current account's language, mirroring the global i18n Translator (src/i18n/translator.py::_)
    which src/__main__.py:61 and settings.py::_set_language keep in sync with settings.language —
    same source of truth the rest of the app already uses, just applied to this pre-auth page too."""
    from src.i18n.translator import _ as translator
    return _WEB_AUTH_STRINGS.get(translator.current_language, _WEB_AUTH_STRINGS["English"])


def _login_html(error: str = "") -> str:
    s = _web_auth_strings()
    return """<!DOCTYPE html>
<html lang="{html_lang}"><head><meta charset="utf-8"><title>TwitchDropsMiner</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<style>{css}</style></head>
<body><div class="card">
<img class="logo" src="/logo.png" alt="TwitchDropsMiner">
<h1>TwitchDropsMiner</h1>
<p class="subtitle">{subtitle}</p>
{error}
<form method="POST" action="/__auth_login">
<input type="password" name="password" placeholder="{password}" autofocus autocomplete="current-password">
<button class="btn btn-primary" type="submit">{login_button}</button>
</form></div></body></html>""".format(css=_SHARED_CSS, error=error, **s)


def _setup_html(error: str = "") -> str:
    s = _web_auth_strings()
    return """<!DOCTYPE html>
<html lang="{html_lang}"><head><meta charset="utf-8"><title>TwitchDropsMiner – Setup</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<style>{css}</style></head>
<body><div class="card">
<img class="logo" src="/logo.png" alt="TwitchDropsMiner">
<h1>{welcome}</h1>
<p class="subtitle">{setup_subtitle}</p>
{error}
<p class="info">{setup_info}</p>
<form method="POST" action="/__setup_post">
<input type="password" name="password" placeholder="{password_optional}" autocomplete="new-password">
<input type="password" name="password2" placeholder="{password_repeat}" autocomplete="new-password">
<button class="btn btn-primary" type="submit" name="action" value="set">{start_with_password}</button>
<hr>
<button class="btn btn-ghost" type="submit" name="action" value="skip">{start_without_password}</button>
</form></div></body></html>""".format(css=_SHARED_CSS, error=error, **s)


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
        return HTMLResponse(_login_html(), status_code=401)


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
    _patch_status_for_persistence(gui)
    asyncio.create_task(_auto_resume_mode())


def _patch_status_for_persistence(gui: WebGUIManager) -> None:
    """Wrap StatusManager.update to auto-save last_mode when idle."""
    import re as _re
    _orig = gui.status.update

    def _patched(status: str) -> None:
        if "💤" in status or "idle watching" in status.lower():
            cfg = _load_web_config()
            cfg["last_mode"] = "idle_watch"
            m = _re.search(r"idle watching:\s*(\S+)", status, _re.IGNORECASE)
            if m:
                cfg["last_idle_channel"] = m.group(1)
            _save_web_config(cfg)
        _orig(status)

    gui.status.update = _patched


async def _auto_resume_mode() -> None:
    """After startup, resume idle_watch if that was the last mode."""
    await asyncio.sleep(20)  # Wait for miner to log in and initialize
    if not twitch_client or not gui_manager:
        return
    cfg = _load_web_config()
    if cfg.get("last_mode") != "idle_watch":
        return
    # Only resume if the miner isn't already doing something active
    current_status = gui_manager.status.get().lower()
    if "💤" in current_status or "idle" in current_status:
        return  # Already idle-watching
    last_channel = cfg.get("last_idle_channel")
    if not last_channel:
        return
    try:
        channel = await twitch_client._fetch_idle_channel_by_login(last_channel)
        if channel is not None:
            twitch_client.gui.clear_drop()
            twitch_client.watch(channel, update_status=False)
            twitch_client.gui.status.update(f"💤 Idle watching: {channel.name}")
    except Exception:
        pass


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
    inventory_list_view: bool | None = None
    mining_benefits: dict[str, bool] | None = None
    claim_channel_points: bool | None = None
    idle_channels: list[str] | None = None
    idle_use_followed: bool | None = None
    idle_parallel: bool | None = None
    preferred_games: list[str] | None = None
    scheduler_enabled: bool | None = None
    scheduler_start: str | None = None
    scheduler_stop: str | None = None
    discord_webhook_drops: str | None = None
    discord_webhook_points: str | None = None
    discord_webhook_mentions: str | None = None
    drop_name_blacklist: list[str] | None = None
    blacklisted_drop_ids: list[str] | None = None
    ignored_campaign_ids: list[str] | None = None
    auto_prioritize: bool | None = None
    auto_add_linked: bool | None = None
    auto_add_excluded_games: list[str] | None = None
    tab_counter_enabled: bool | None = None
    make_predictions: bool | None = None
    bet_strategy: str | None = None
    bet_percentage: int | None = None
    bet_max_points: int | None = None
    bet_minimum_points: int | None = None
    bet_percentage_gap: int | None = None
    bet_delay_seconds: int | None = None
    prediction_channels: list[str] | None = None
    channel_strategies: dict[str, str] | None = None
    claim_moments: bool | None = None
    irc_chat_presence: bool | None = None
    irc_mention_notify: bool | None = None


class ProxyVerifyRequest(BaseModel):
    proxy: str


class PairClaimRequest(BaseModel):
    code: str


# ==================== Auth Endpoints ====================


@app.get("/__setup", response_class=HTMLResponse)
async def setup_page():
    if _is_setup_done():
        return RedirectResponse("/", status_code=302)
    return HTMLResponse(_setup_html())


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
            return HTMLResponse(_setup_html(f'<p class="err">{_web_auth_strings()["error_password_required"]}</p>'))
        if pw != pw2:
            return HTMLResponse(_setup_html(f'<p class="err">{_web_auth_strings()["error_password_mismatch"]}</p>'))
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
    return HTMLResponse(_login_html(f'<p class="err">{_web_auth_strings()["wrong_password"]}</p>'), status_code=401)


@app.get("/__auth_logout")
async def auth_logout():
    resp = RedirectResponse("/__auth_login_page", status_code=303)
    resp.delete_cookie("__tdm_session")
    return resp


@app.get("/__auth_login_page", response_class=HTMLResponse)
async def auth_login_page():
    return HTMLResponse(_login_html())


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


@app.get("/healthz")
async def healthz():
    """Unauthenticated liveness probe for Docker/orchestrators — no session state exposed."""
    return {"status": "ok"}


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
        cp_obj = None
        try:
            cp_obj = data["community"]["channel"]["self"]["communityPoints"]
        except (KeyError, TypeError):
            pass
        cp_enabled = cp_obj is not None
        points: int = int(cp_obj.get("balance", 0)) if cp_enabled else 0
        # Twitch logins are case-insensitive; always key persisted history by the
        # lowercase login so this channel doesn't split across case-variant entries.
        login_key = channel_login.lower()
        # Persist
        if points:
            from src.utils import json_load, json_save, merge_case_variant_keys
            _cp_file = _get_account_data_dir() / "channel_points.json"
            history = merge_case_variant_keys(json_load(_cp_file, {}, merge=False))
            history[login_key] = points
            json_save(_cp_file, history)
        # Include last chest bonus info for Discord bot split notification
        last_chest = {}
        try:
            from src.utils import json_load, merge_case_variant_keys
            _chest_file = _get_account_data_dir() / "last_chest.json"
            if _chest_file.exists():
                _chest_data = merge_case_variant_keys(json_load(_chest_file, {}, merge=False))
                last_chest = _chest_data.get(login_key, {})
        except Exception:
            pass
        return {"channel": channel_login, "balance": points, "cp_enabled": cp_enabled, "last_chest": last_chest}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



_SUBSCRIBED_CHANNELS_TTL = 6 * 3600  # subscription status changes rarely — avoid re-checking every tab open


@app.get("/api/analytics/subscribed-channels")
async def get_subscribed_channels(refresh: bool = False):
    """Return which channels (from channel-points history) the account currently
    holds an active Twitch subscription to. Cached with a TTL since it costs one
    Helix call per tracked channel; falls back to stale cache if the live check
    fails so the UI doesn't hard-error on a transient Twitch/Helix hiccup.
    """
    if not gui_manager or not twitch_client:
        raise HTTPException(status_code=503, detail="Not ready")
    import time
    cache_file = _get_account_data_dir() / "subscribed_channels.json"
    now = time.time()

    def _read_cache() -> dict | None:
        try:
            if cache_file.exists():
                return json.loads(cache_file.read_text())
        except Exception:
            pass
        return None

    if not refresh:
        cached = _read_cache()
        if cached and (now - cached.get("ts", 0)) < _SUBSCRIBED_CHANNELS_TTL:
            return {"channels": cached.get("channels", []), "cached": True, "ts": cached.get("ts")}

    history_logins = list(_load_channel_points_history().keys())
    try:
        subscribed = await twitch_client._fetch_subscribed_channels(history_logins)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"ts": now, "channels": subscribed}))
        return {"channels": subscribed, "cached": False, "ts": now}
    except Exception as e:
        cached = _read_cache()
        if cached is not None:
            return {"channels": cached.get("channels", []), "cached": True, "stale": True, "ts": cached.get("ts")}
        raise HTTPException(status_code=500, detail=str(e))


_FOLLOWED_CHANNELS_TTL = 6 * 3600  # matches the subscribed-channels TTL — same cost/rationale


@app.get("/api/analytics/followed-channels")
async def get_followed_channels(refresh: bool = False):
    """Return which channels the account currently follows on Twitch, for the
    Channel Points list / Points Over Time "Followed" filter mode. Following is
    a much lower bar than subscribing (free, one click) so unlike the
    subscribed-channels filter this is expected to actually have overlap with
    most accounts' channel-points history. Reuses the same GQL-backed
    _fetch_followed_channels() idle-watch's "Auto: use followed channels"
    setting already relies on. Cached with the same TTL/stale-fallback shape
    as get_subscribed_channels() above.
    """
    if not gui_manager or not twitch_client:
        raise HTTPException(status_code=503, detail="Not ready")
    import time
    cache_file = _get_account_data_dir() / "followed_channels.json"
    now = time.time()

    def _read_cache() -> dict | None:
        try:
            if cache_file.exists():
                return json.loads(cache_file.read_text())
        except Exception:
            pass
        return None

    if not refresh:
        cached = _read_cache()
        if cached and (now - cached.get("ts", 0)) < _FOLLOWED_CHANNELS_TTL:
            return {"channels": cached.get("channels", []), "cached": True, "ts": cached.get("ts")}

    try:
        followed = await twitch_client._fetch_followed_channels()
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"ts": now, "channels": followed}))
        return {"channels": followed, "cached": False, "ts": now}
    except Exception as e:
        cached = _read_cache()
        if cached is not None:
            return {"channels": cached.get("channels", []), "cached": True, "stale": True, "ts": cached.get("ts")}
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/analytics/points")
async def get_analytics_points(channel: str = "", days: int = 7):
    """Return timestamped channel points history for analytics chart."""
    days = max(1, min(days, 365))
    from datetime import datetime, timezone, timedelta
    ts_file = _get_account_data_dir() / "channel_points_ts.json"
    try:
        data = json.loads(ts_file.read_text()) if ts_file.exists() else {}
    except Exception:
        data = {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    if channel:
        raw = data.get(channel.lower(), [])
        filtered = [p for p in raw if p.get("ts", "") >= cutoff]
        return {"channels": {channel.lower(): filtered}}
    result = {}
    for login, snapshots in data.items():
        result[login] = [p for p in snapshots if p.get("ts", "") >= cutoff]
    return {"channels": result}


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

@app.get("/api/stats")
async def get_stats(request: Request):
    return _aggregate_stats()

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
    """Switch to the next idle channel (manual list or followed). Saves last_mode."""
    cfg = _load_web_config()
    cfg["last_mode"] = "idle_watch"
    _save_web_config(cfg)
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
            cfg2 = _load_web_config()
            cfg2["last_idle_channel"] = next_login
            _save_web_config(cfg2)
            return {"switched_to": next_login}

    raise HTTPException(status_code=404, detail="No other idle channels are online")


@app.post("/api/idle-watch/resume")
async def idle_watch_resume():
    """Resume watching the last known idle channel, falling back to switch."""
    if not gui_manager or not twitch_client:
        raise HTTPException(status_code=503, detail="Not ready")
    cfg = _load_web_config()
    last_channel = cfg.get("last_idle_channel")
    if last_channel:
        channel = await twitch_client._fetch_idle_channel_by_login(last_channel)
        if channel is not None:
            twitch_client.gui.clear_drop()
            twitch_client.watch(channel, update_status=False)
            twitch_client.gui.status.update(f"💤 Idle watching: {channel.name}")
            return {"switched_to": last_channel, "resumed": True}
    return await idle_watch_switch()


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


@app.get("/api/instance")
async def get_instance():
    import os as _eos
    port = int(_eos.environ.get("TDM_PORT", 8080))
    label = _eos.environ.get("TDM_LABEL", f"Instance {port}")
    login = None
    try:
        if twitch_client and hasattr(twitch_client._auth_state, "user_login"):
            login = twitch_client._auth_state.user_login
    except Exception:
        pass
    return {"port": port, "label": label, "login": login}


@app.get("/api/version")
async def get_version():
    """Get current application version and check for updates"""
    import aiohttp

    from src.version import __version__

    current_version = __version__
    latest_version = None
    update_available = False
    download_url = None
    release_notes = None

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
                release_notes = data.get("body", "")

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
        "release_notes": release_notes,
    }


_SECRET_LINE_RE = re.compile(
    r"(oauth:[a-zA-Z0-9]+|(?:access_token|refresh_token|client_secret|password)[\"']?\s*[:=]\s*[\"']?[^\s\"',}]+)",
    re.IGNORECASE,
)


@app.get("/api/logs/download")
async def download_logs():
    """Bundle recent log output into one downloadable file users can attach to a bug report."""
    logs_dir = Path(__file__).parent.parent.parent / "logs"
    log_file = logs_dir / "TDM.log"
    if not log_file.exists():
        raise HTTPException(status_code=404, detail="No log file found yet")

    MAX_LINES = 5000
    try:
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not read log file: {e}")
    if len(lines) > MAX_LINES:
        lines = [f"... ({len(lines) - MAX_LINES} earlier lines omitted) ..."] + lines[-MAX_LINES:]
    content = "\n".join(_SECRET_LINE_RE.sub("[REDACTED]", line) for line in lines)

    from src.version import __version__ as _tdm_version
    filename = f"TDM-logs-v{_tdm_version}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.txt"
    return Response(
        content=content,
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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

    cfg = _load_web_config()
    cfg["last_mode"] = "drop_mining"
    _save_web_config(cfg)
    twitch_client.clear_skipped_games()
    twitch_client.change_state(State.INVENTORY_FETCH)
    return {"success": True}


@app.post("/api/close")
async def trigger_close():
    """Trigger application shutdown"""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Twitch client not initialized")

    twitch_client.close()
    return {"success": True}


def _is_docker() -> bool:
    return Path("/.dockerenv").exists() or _os.environ.get("DOCKER_CONTAINER") == "1"


def _restart_self() -> None:
    """Restart the current process. Uses PM2 if present (maintainer's own VPS
    layout); otherwise re-execs the process in place, which works fine under
    Docker (container keeps running, PID 1 re-execs) or bare systemd/manual runs."""
    if shutil.which("pm2"):
        subprocess.Popen(["pm2", "restart", "twitchdrops"])
    else:
        logging.getLogger(__name__).info("pm2 not found, re-exec'ing process in place to restart")
        os.execv(sys.executable, [sys.executable] + sys.argv)


@app.post("/api/restart")
async def trigger_restart():
    """Restart via PM2 if available, otherwise re-exec in place."""

    async def _restart():
        await asyncio.sleep(1)
        _restart_self()

    asyncio.create_task(_restart())
    return {"success": True}


@app.post("/api/self-update")
async def self_update():
    """Pull latest code from GitHub and restart — detects Docker vs PM2"""
    import subprocess

    if _is_docker():
        return {
            "success": False,
            "docker": True,
            "log": (
                "Running inside Docker.\n\n"
                "To update, run on your host:\n\n"
                "  docker compose pull\n"
                "  docker compose up -d\n\n"
                "Or with docker run:\n\n"
                "  docker pull gitsimpliaj/twitch-drops-miner:latest\n"
                "  docker stop <container> && docker run ..."
            ),
        }

    repo_dir = Path(__file__).parent.parent.parent
    logs = []

    try:
        result = subprocess.run(
            ["git", "pull", "simpliaj", "main"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=60,
        )
        logs.append(result.stdout.strip() or "(no output)")
        if result.stderr.strip():
            logs.append(result.stderr.strip())
        success = result.returncode == 0
    except Exception as e:
        logs.append(f"Error: {e}")
        success = False

    async def _restart():
        await asyncio.sleep(2)
        if shutil.which("pm2"):
            subprocess.Popen(["pm2", "restart", "twitchdrops", "twitchdrops2"])
        else:
            logging.getLogger(__name__).warning(
                "pm2 not found, cannot restart multi-instance setup automatically — restart manually"
            )

    asyncio.create_task(_restart())
    return {"success": success, "log": "\n".join(logs)}


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
            # New nested format: {uid: {name: pairing}}
            if isinstance(entry, dict) and "url" not in entry:
                for name, pairing in entry.items():
                    if isinstance(pairing, dict) and pairing.get("token") == token:
                        return {"discord_user_id": uid, "_pairing_name": name, **pairing}
            else:
                # Old flat format
                if entry.get("token") == token:
                    return {"discord_user_id": uid, "_pairing_name": "default", **entry}
    except Exception:
        pass
    return None


def _save_pairing_channels(discord_user_id: str, channels: dict) -> None:
    if not _PAIRINGS_FILE.exists():
        return
    data = json.loads(_PAIRINGS_FILE.read_text())
    users = data.get("users", {})
    if discord_user_id not in users:
        return
    entry = users[discord_user_id]
    if "url" not in entry:
        # New nested format — find which named pairing belongs to this instance
        token = _get_bot_token()
        for name, pairing in entry.items():
            if isinstance(pairing, dict) and pairing.get("token") == token:
                data["users"][discord_user_id][name]["channels"] = channels
                break
    else:
        data["users"][discord_user_id]["channels"] = channels
    _PAIRINGS_FILE.write_text(json.dumps(data, indent=2))


def _save_pairing_field(discord_user_id: str, field: str, value) -> None:
    if not _PAIRINGS_FILE.exists():
        return
    data = json.loads(_PAIRINGS_FILE.read_text())
    users = data.get("users", {})
    if discord_user_id not in users:
        return
    entry = users[discord_user_id]
    if "url" not in entry:
        token = _get_bot_token()
        for name, pairing in entry.items():
            if isinstance(pairing, dict) and pairing.get("token") == token:
                if value is None:
                    data["users"][discord_user_id][name].pop(field, None)
                else:
                    data["users"][discord_user_id][name][field] = value
                break
    else:
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
        _restart_self()
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
        _restart_self()
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


@app.get("/api/push-config")
async def get_push_config(request: Request):
    return _get_push_config()


@app.post("/api/push-config")
async def set_push_config(request: Request):
    body = await request.json()
    cfg = _load_web_config()
    for key in ("push_notifications_enabled", "push_sound_enabled", "campaign_end_alerts_enabled"):
        if key in body:
            cfg[key] = bool(body[key])
    _save_web_config(cfg)
    return {"ok": True}


# ==================== Instance Management ====================

_INSTANCES_FILE = _DATA_DIR / "instances.json"
_LEGACY_INSTANCES_FILE = Path(__file__).parent.parent.parent / "instances.json"


def _migrate_legacy_instances_file() -> None:
    # instances.json used to live at the repo root, which is neither a
    # git-ignored path (update.sh's "stash custom mods, pull, pop" dance can
    # conflict on it) nor a Docker volume (./data is the only one mounted) —
    # so every update or container recreation silently reset manually
    # registered instances back to the two defaults. Move it under data/,
    # which is already both git-ignored and volume-mounted.
    if _LEGACY_INSTANCES_FILE.exists() and not _INSTANCES_FILE.exists():
        _DATA_DIR.mkdir(exist_ok=True)
        _INSTANCES_FILE.write_text(_LEGACY_INSTANCES_FILE.read_text())


_migrate_legacy_instances_file()


def _autoprovision_enabled() -> bool:
    # pm2+nginx auto-provisioning (scripts/manage_instance.sh) is hardcoded to
    # the maintainer's own VPS layout (absolute path, sudo nginx reload, a
    # fixed domain) and isn't shipped in the Docker image. It's opt-in via env
    # var so it only ever shows up on that one deployment — every other
    # self-hoster only ever sees the host/port "register existing instance"
    # path, which works anywhere.
    return os.environ.get("ENABLE_AUTOPROVISION", "").lower() in ("1", "true", "yes")


def _load_instances_registry() -> dict:
    if _INSTANCES_FILE.exists():
        return json.loads(_INSTANCES_FILE.read_text())
    return {"instances": [
        {"n": 1, "port": 8080, "data_dir": "data", "pm2_name": "twitchdrops", "label": "Account 1"},
        {"n": 2, "port": 8082, "data_dir": "data2", "pm2_name": "twitchdrops2", "label": "Account 2"},
    ]}


@app.get("/api/instances")
async def get_instances():
    registry = _load_instances_registry()
    if len(registry.get("instances", [])) >= 3:
        registry["proxy_warning"] = True
    registry["autoprovision_enabled"] = _autoprovision_enabled()
    return registry


@app.post("/api/instances")
async def create_instance():
    if not _autoprovision_enabled():
        raise HTTPException(
            status_code=403,
            detail=(
                "Auto-provisioning isn't available on this deployment — it only works on "
                "the maintainer's own VPS layout (pm2+nginx, fixed paths). Use "
                "'Register existing instance' instead to add one running on any host/port."
            ),
        )
    import subprocess
    script = str(Path(__file__).parent.parent.parent / "scripts" / "manage_instance.sh")
    if not Path(script).exists():
        raise HTTPException(
            status_code=500,
            detail="manage_instance.sh not found even though auto-provisioning is enabled.",
        )
    result = subprocess.run(["bash", script, "create"], capture_output=True, text=True)
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=result.stderr)
    return {"success": True, "instances": _load_instances_registry()["instances"]}


class RegisterInstanceRequest(BaseModel):
    host: str
    port: int
    label: str | None = None


@app.post("/api/instances/register")
async def register_instance(req: RegisterInstanceRequest):
    """Register an already-running instance (any host/port) for tab-switching only.

    Unlike POST /api/instances, this never starts a process or touches nginx —
    it just remembers where to send you. Switching to it navigates the browser
    to that instance's own origin instead of relying on a reverse-proxy path,
    so it works regardless of how or where that instance is actually hosted.
    """
    if not (1 <= req.port <= 65535):
        raise HTTPException(status_code=400, detail="Port must be between 1 and 65535")
    host = req.host.strip() or "localhost"
    registry = _load_instances_registry()
    instances = registry["instances"]
    next_n = max((i["n"] for i in instances), default=0) + 1
    instances.append({
        "n": next_n,
        "base_url": f"http://{host}:{req.port}",
        "label": (req.label or "").strip() or f"Account {next_n}",
    })
    _INSTANCES_FILE.write_text(json.dumps(registry, indent=2) + "\n")
    return {"success": True, "instances": instances}


@app.delete("/api/instances/{n}")
async def remove_instance(n: int):
    if n == 1:
        raise HTTPException(status_code=400, detail="Cannot remove main instance")
    registry = _load_instances_registry()
    target = next((i for i in registry["instances"] if i["n"] == n), None)
    if target is None:
        raise HTTPException(status_code=404, detail="Instance not found")
    import subprocess
    script = str(Path(__file__).parent.parent.parent / "scripts" / "manage_instance.sh")
    if "pm2_name" not in target or not _autoprovision_enabled() or not Path(script).exists():
        # Manually registered, or auto-provisioning isn't available on this
        # deployment (see _autoprovision_enabled) — there's no pm2/nginx setup
        # to tear down here, so just forget about it instead of shelling out
        # to a script that's hardcoded to the maintainer's own VPS layout.
        registry["instances"] = [i for i in registry["instances"] if i["n"] != n]
        _INSTANCES_FILE.write_text(json.dumps(registry, indent=2) + "\n")
        return {"success": True, "instances": registry["instances"]}
    result = subprocess.run(["bash", script, "remove", str(n)], capture_output=True, text=True)
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=result.stderr)
    return {"success": True, "instances": _load_instances_registry()["instances"]}


# ==================== Accounts Fleet View ====================
# Multi-account overview + bulk settings. This deliberately doesn't invent a
# new cross-instance data layer: it fans out plain HTTP calls to each
# instance's own already-existing single-account API (the same endpoints its
# own dashboard uses) and aggregates the responses. Bulk-apply works the same
# way — it just calls each target instance's own POST /api/settings once per
# account, reusing that endpoint's existing partial-update semantics.

_FLEET_HTTP_TIMEOUT = 4.0
# The only settings exposed for bulk-apply — the ones genuinely shared across
# a fleet of accounts mining the same set of games. Not every SettingsUpdate
# field belongs here (things like proxy/webhooks are inherently per-account).
_BULK_EDITABLE_FIELDS = {
    "games_to_watch": "Games to Watch (priority list)",
    "auto_add_excluded_games": "Blacklisted Games",
}


def _instance_base_url(inst: dict) -> str:
    if inst.get("base_url"):
        return inst["base_url"].rstrip("/")
    return f"http://127.0.0.1:{inst.get('port', 8080)}"


def _fleet_auth_cookies() -> dict:
    # All instances in a single-user deployment share the same web password
    # (that's how the account-switcher pills already work today — they just
    # navigate the browser to another port and the same session cookie is
    # valid there too, since cookies aren't port-scoped). Reuse that here:
    # send this instance's own password as the session cookie on outbound
    # calls. If a target instance was set up with a different password, that
    # one call 401s and is reported per-account instead of failing the batch.
    pw = _get_password()
    return {"__tdm_session": pw} if pw else {}


def _compute_bulk_list(current: list[str], values: list[str], mode: str) -> list[str]:
    """Pure merge logic for bulk-editing a settings list field, split out for testability."""
    cleaned = [v.strip() for v in values if v.strip()]
    if mode == "replace":
        return list(dict.fromkeys(cleaned))
    if mode == "remove":
        remove_set = {v.lower() for v in cleaned}
        return [v for v in current if v.strip().lower() not in remove_set]
    # add (default)
    result = list(current)
    existing_lower = {v.strip().lower() for v in result}
    for v in cleaned:
        if v.lower() not in existing_lower:
            result.append(v)
            existing_lower.add(v.lower())
    return result


async def _fetch_instance_overview(session, inst: dict) -> dict:
    n = inst["n"]
    label = inst.get("label") or f"Account {n}"
    base = _instance_base_url(inst)
    cookies = _fleet_auth_cookies()
    result: dict = {
        "n": n,
        "label": label,
        "base_url": base,
        "reachable": False,
        "login": None,
        "status_text": None,
        "paused": None,
        "watching": None,
        "drops_today": None,
        "last_active": None,
        "error": None,
    }
    try:
        async with session.get(f"{base}/api/instance", cookies=cookies, timeout=_FLEET_HTTP_TIMEOUT) as r:
            if r.status == 200:
                d = await r.json()
                result["login"] = d.get("login")
            elif r.status == 401:
                result["error"] = "Auth failed (password mismatch between instances)"
                return result
    except Exception as e:
        result["error"] = f"Unreachable: {e}"
        return result

    try:
        async with session.get(f"{base}/api/status", cookies=cookies, timeout=_FLEET_HTTP_TIMEOUT) as r:
            if r.status == 200:
                d = await r.json()
                result["status_text"] = d.get("status")
                result["paused"] = d.get("paused")
                result["reachable"] = True
            elif r.status == 503:
                result["reachable"] = True
                result["status_text"] = "Starting…"
    except Exception as e:
        result["error"] = result["error"] or str(e)

    try:
        async with session.get(f"{base}/api/channels", cookies=cookies, timeout=_FLEET_HTTP_TIMEOUT) as r:
            if r.status == 200:
                d = await r.json()
                watching = next((c for c in d.get("channels", []) if c.get("watching")), None)
                if watching:
                    result["watching"] = {"channel": watching.get("name"), "game": watching.get("game")}
    except Exception:
        pass

    try:
        async with session.get(f"{base}/api/stats", cookies=cookies, timeout=_FLEET_HTTP_TIMEOUT) as r:
            if r.status == 200:
                d = await r.json()
                today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                by_day = {e.get("date"): e.get("count", 0) for e in d.get("by_day", [])}
                result["drops_today"] = by_day.get(today_key, 0)
    except Exception:
        pass

    try:
        async with session.get(f"{base}/api/drops-history", cookies=cookies, timeout=_FLEET_HTTP_TIMEOUT) as r:
            if r.status == 200:
                d = await r.json()
                if isinstance(d, list) and d:
                    result["last_active"] = d[0].get("timestamp")
    except Exception:
        pass

    return result


@app.get("/api/accounts/overview")
async def get_accounts_overview():
    """Fleet-wide status snapshot: one row per registered instance."""
    registry = _load_instances_registry()
    instances = registry.get("instances", [])
    import aiohttp
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*[_fetch_instance_overview(session, inst) for inst in instances])
    return {"accounts": results, "bulk_editable_fields": _BULK_EDITABLE_FIELDS}


class BulkSettingsRequest(BaseModel):
    targets: list[int]
    field: str
    values: list[str]
    mode: str = "add"  # "add" | "remove" | "replace"


@app.post("/api/accounts/bulk-settings")
async def bulk_apply_settings(req: BulkSettingsRequest):
    """Apply a shared-settings change across multiple accounts at once.

    Fans out to each target instance's own GET/POST /api/settings — the same
    endpoint that instance's own Settings tab uses — instead of writing to
    any settings file directly, so per-instance validation/side effects
    (debounced reload, broadcast to that instance's own connected clients)
    still happen exactly as they do for a normal single-account save.
    """
    if req.field not in _BULK_EDITABLE_FIELDS:
        raise HTTPException(
            status_code=400,
            detail=f"'{req.field}' isn't bulk-editable. Choose one of: {', '.join(_BULK_EDITABLE_FIELDS)}",
        )
    if req.mode not in ("add", "remove", "replace"):
        raise HTTPException(status_code=400, detail="mode must be 'add', 'remove', or 'replace'")
    if not req.targets:
        raise HTTPException(status_code=400, detail="No target accounts selected")

    registry = _load_instances_registry()
    by_n = {i["n"]: i for i in registry.get("instances", [])}
    cookies = _fleet_auth_cookies()
    results = []
    import aiohttp
    async with aiohttp.ClientSession() as session:
        for n in req.targets:
            inst = by_n.get(n)
            if inst is None:
                results.append({"n": n, "success": False, "error": "Unknown instance"})
                continue
            base = _instance_base_url(inst)
            try:
                current: list[str] = []
                if req.mode != "replace":
                    async with session.get(
                        f"{base}/api/settings", cookies=cookies, timeout=_FLEET_HTTP_TIMEOUT
                    ) as r:
                        if r.status == 200:
                            d = await r.json()
                            current = list(d.get(req.field) or [])
                        elif r.status == 401:
                            results.append({"n": n, "success": False, "error": "Auth failed"})
                            continue
                new_list = _compute_bulk_list(current, req.values, req.mode)
                async with session.post(
                    f"{base}/api/settings", cookies=cookies, json={req.field: new_list}, timeout=_FLEET_HTTP_TIMEOUT
                ) as r:
                    if r.status == 200:
                        results.append({"n": n, "success": True, "count": len(new_list)})
                    else:
                        text = await r.text()
                        results.append({"n": n, "success": False, "error": f"HTTP {r.status}: {text[:200]}"})
            except Exception as e:
                results.append({"n": n, "success": False, "error": str(e)})
    return {"results": results}


class BulkActionRequest(BaseModel):
    targets: list[int]
    action: str  # "start" | "pause" | "drop_mining"


@app.post("/api/accounts/bulk-action")
async def bulk_account_action(req: BulkActionRequest):
    """Fan out a real operational action (not just a settings change) across accounts.

    Same fan-out shape as /api/accounts/bulk-settings: for each target this
    calls that instance's own already-existing single-account endpoints —
    never reimplements mining-control logic here.

    - "pause": POST {instance}/api/pause — identical to that instance's own
      "Pause" quick-control button; stops the mining loop entirely.
    - "start": ensures the account is actively watching. If it's currently
      paused, POST {instance}/api/resume first (mirrors that instance's own
      "Resume" button) so the account isn't stuck in the PAUSED loop, which
      would otherwise silently discard any channel switch. Then POST
      {instance}/api/idle-watch/switch — identical to that instance's own
      "Start Idle Watch" / "Switch Channel" quick-control button, which picks
      an idle channel from that instance's own configured idle_channels list
      and/or its followed-live channels (whichever idle_use_followed setting
      that instance already has configured).
    - "drop_mining": POST {instance}/api/reload — identical to that instance's
      own "Start Drop Mining" quick-control button; stops idle-watching and
      makes that instance search for active drop campaigns right away.
    """
    if req.action not in ("start", "pause", "drop_mining"):
        raise HTTPException(status_code=400, detail="action must be 'start', 'pause', or 'drop_mining'")
    if not req.targets:
        raise HTTPException(status_code=400, detail="No target accounts selected")

    registry = _load_instances_registry()
    by_n = {i["n"]: i for i in registry.get("instances", [])}
    cookies = _fleet_auth_cookies()
    results = []
    import aiohttp
    async with aiohttp.ClientSession() as session:
        for n in req.targets:
            inst = by_n.get(n)
            if inst is None:
                results.append({"n": n, "success": False, "error": "Unknown instance"})
                continue
            base = _instance_base_url(inst)
            try:
                if req.action == "pause":
                    async with session.post(
                        f"{base}/api/pause", cookies=cookies, timeout=_FLEET_HTTP_TIMEOUT
                    ) as r:
                        if r.status == 200:
                            results.append({"n": n, "success": True, "paused": True})
                        elif r.status == 401:
                            results.append({"n": n, "success": False, "error": "Auth failed"})
                        else:
                            text = await r.text()
                            results.append({"n": n, "success": False, "error": f"HTTP {r.status}: {text[:200]}"})
                    continue

                if req.action == "drop_mining":
                    async with session.post(
                        f"{base}/api/reload", cookies=cookies, timeout=_FLEET_HTTP_TIMEOUT
                    ) as r:
                        if r.status == 200:
                            results.append({"n": n, "success": True})
                        elif r.status == 401:
                            results.append({"n": n, "success": False, "error": "Auth failed"})
                        else:
                            text = await r.text()
                            results.append({"n": n, "success": False, "error": f"HTTP {r.status}: {text[:200]}"})
                    continue

                # action == "start"
                was_paused = False
                try:
                    async with session.get(
                        f"{base}/api/status", cookies=cookies, timeout=_FLEET_HTTP_TIMEOUT
                    ) as r:
                        if r.status == 200:
                            d = await r.json()
                            was_paused = bool(d.get("paused"))
                        elif r.status == 401:
                            results.append({"n": n, "success": False, "error": "Auth failed"})
                            continue
                except Exception:
                    pass  # fall through and try to switch anyway

                if was_paused:
                    async with session.post(
                        f"{base}/api/resume", cookies=cookies, timeout=_FLEET_HTTP_TIMEOUT
                    ) as r:
                        if r.status not in (200,):
                            text = await r.text()
                            results.append({"n": n, "success": False, "error": f"Resume failed — HTTP {r.status}: {text[:200]}"})
                            continue

                async with session.post(
                    f"{base}/api/idle-watch/switch", cookies=cookies, timeout=_FLEET_HTTP_TIMEOUT
                ) as r:
                    if r.status == 200:
                        d = await r.json()
                        results.append({"n": n, "success": True, "channel": d.get("switched_to")})
                    elif r.status == 401:
                        results.append({"n": n, "success": False, "error": "Auth failed"})
                    else:
                        text = await r.text()
                        results.append({"n": n, "success": False, "error": f"HTTP {r.status}: {text[:200]}"})
            except Exception as e:
                results.append({"n": n, "success": False, "error": str(e)})
    return {"results": results}


@app.get("/api/predictions")
async def get_predictions():
    """Return predictions history."""
    from src.services.prediction_service import MAX_HISTORY, _get_predictions_file, sweep_stale_pending_by_age
    import json as _j
    p = _get_predictions_file()
    try:
        hist = _j.loads(p.read_text()) if p.exists() else []
    except Exception:
        hist = []
    if sweep_stale_pending_by_age(hist):
        try:
            p.write_text(_j.dumps(hist, indent=2))
        except Exception:
            pass
    return {"predictions": list(reversed(hist[-MAX_HISTORY:]))}


@app.get("/api/streamer-overrides")
async def get_streamer_overrides():
    from src.services.prediction_service import _load_overrides
    return {"overrides": _load_overrides()}


class StreamerOverrideRequest(BaseModel):
    channel: str
    overrides: dict


@app.post("/api/streamer-overrides")
async def set_streamer_override(req: StreamerOverrideRequest):
    from src.services.prediction_service import _get_overrides_file
    import json as _j
    p = _get_overrides_file()
    try:
        data = _j.loads(p.read_text()) if p.exists() else {}
    except Exception:
        data = {}
    if req.overrides:
        data[req.channel.lower()] = req.overrides
    else:
        data.pop(req.channel.lower(), None)
    p.write_text(_j.dumps(data, indent=2))
    return {"ok": True}


@app.post("/api/session-report")
async def send_session_report():
    """Build and send session report to Discord."""
    import json as _j
    from datetime import datetime, timezone
    from src.services.prediction_service import _get_predictions_file
    webhook = twitch_client.settings.discord_webhook_points if twitch_client else ""
    if not webhook:
        raise HTTPException(status_code=400, detail="No webhook configured")
    # Points delta
    cp_file = _get_account_data_dir() / "channel_points.json"
    try:
        cp = _j.loads(cp_file.read_text()) if cp_file.exists() else {}
    except Exception:
        cp = {}
    # Drops
    hist_file = _get_account_data_dir() / "drops_history.json"
    try:
        drops = _j.loads(hist_file.read_text()) if hist_file.exists() else []
    except Exception:
        drops = []
    # Predictions
    ph = _get_predictions_file()
    try:
        preds = _j.loads(ph.read_text()) if ph.exists() else []
    except Exception:
        preds = []
    wins = sum(1 for p in preds if p.get("result") == "WIN")
    losses = sum(1 for p in preds if p.get("result") == "LOSE")
    net = sum(p.get("points_won", 0) - p.get("points_bet", 0) for p in preds if p.get("result") in ("WIN", "LOSE"))
    fields = [
        {"name": "📊 Channel Points", "value": "\n".join(f"{ch}: {bal:,}" for ch, bal in list(cp.items())[:10]) or "—", "inline": False},
        {"name": "🎁 Drops Claimed", "value": str(len(drops)), "inline": True},
        {"name": "🎯 Predictions", "value": f"W:{wins} L:{losses} Net:{net:+,}", "inline": True},
    ]
    embed = {"title": "📋 Session Report", "color": 0x9147FF, "fields": fields, "timestamp": datetime.now(timezone.utc).isoformat()}
    import aiohttp
    async with aiohttp.ClientSession() as session:
        await session.post(webhook, json={"embeds": [embed]}, timeout=aiohttp.ClientTimeout(total=10))
    return {"ok": True}


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
                        "game": ch.game.name if ch.game is not None else "",
                    }
                    if (ch := twitch_client.watching_channel.get_with_default(None)) is not None
                    else None
                ),
                "channel_points_history": _load_channel_points_history(),
                "daily_points": _load_daily_points(),
                "last_mode": _load_web_config().get("last_mode", "drop_mining"),
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

    # uvicorn's legacy `websockets` protocol implementation logs a benign
    # ConnectionClosedError from a shielded background task whenever a browser
    # tab closes/reloads mid-connection. It's not an app error — silence it.
    logging.getLogger("websockets").setLevel(logging.CRITICAL)

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
