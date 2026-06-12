# Push Notifications, Drop Stats, Campaign End Alerts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add browser push notifications for claimed drops, a stats dashboard tab, and campaign end alerts via Discord + push.

**Architecture:** All settings stored in `web_config.json` (existing pattern). New `/api/stats` endpoint reads `drops_history.json`. Campaign alert service checks after every inventory fetch. Push notification via existing `Notification` browser API (already has permission request). i18n keys added to all language files (English values as fallback for untranslated languages).

**Tech Stack:** Python/FastAPI backend, vanilla JS + Socket.IO frontend, existing aiohttp for Discord webhooks.

---

## File Map

| File | Action | Purpose |
|------|--------|---------|
| `lang/English.json` | Modify | New i18n keys for all 3 features |
| `lang/*.json` (18 other files) | Modify | Add same keys with English fallback values |
| `src/i18n/translator.py` | Modify | Extend `GUISettings` + `GUITabs` TypedDicts |
| `src/web/app.py` | Modify | `/api/stats` endpoint, web_config getters/setters for push/alert settings |
| `src/services/campaign_alert_service.py` | Create | Check campaigns ending <24h, send Discord + push alert |
| `src/core/client.py` | Modify | Call campaign alert service after `fetch_inventory()` |
| `web/index.html` | Modify | Stats tab button + content, Notifications section in Settings |
| `web/static/app.js` | Modify | Push notification logic, stats rendering, applyTranslations extension |
| `tests/test_stats_api.py` | Create | Unit tests for /api/stats aggregation |
| `tests/test_campaign_alert.py` | Create | Unit tests for campaign alert logic |

---

## Task 1: Extend i18n TypedDicts and Language Files

**Files:**
- Modify: `src/i18n/translator.py`
- Modify: `lang/English.json`
- Modify: all other `lang/*.json` files (18 files)

- [ ] **Step 1: Extend `GUISettings` TypedDict in `translator.py`**

In `src/i18n/translator.py`, after `minimum_refresh: str` in `GUISettings`, add:

```python
    select_linked: str
    password_header: str
    password_current_label: str
    password_current_placeholder: str
    password_new_label: str
    password_new_placeholder: str
    password_confirm_label: str
    password_confirm_placeholder: str
    password_save: str
    password_disable: str
    password_saved: str
    password_disabled_msg: str
    password_mismatch: str
    password_status_active: str
    password_status_inactive: str
    discord_bot: dict
    notifications_header: str
    push_enabled: str
    push_sound: str
    campaign_end_alerts_enabled: str
```

Also extend `GUITabs` (currently only has `main`, `inventory`, `settings`, `help`, `system`):

```python
class GUITabs(TypedDict):
    main: str
    inventory: str
    settings: str
    help: str
    system: str
    stats: str
```

- [ ] **Step 2: Add new keys to `lang/English.json`**

In `lang/English.json`, under `gui.settings`, add after the last existing key (`discord_bot`):

```json
"notifications_header": "Notifications",
"push_enabled": "Browser Push Notifications",
"push_sound": "Notification Sound",
"campaign_end_alerts_enabled": "Campaign End Alerts (24h warning)"
```

In `lang/English.json`, under `gui.tabs`, add:

```json
"stats": "Stats"
```

- [ ] **Step 3: Add same keys to all other language files**

Run this Python script to add English fallback values to all other lang files:

```python
import json
import pathlib

lang_dir = pathlib.Path("lang")
english = json.loads((lang_dir / "English.json").read_text(encoding="utf-8"))

new_settings_keys = {
    "notifications_header": "Notifications",
    "push_enabled": "Browser Push Notifications",
    "push_sound": "Notification Sound",
    "campaign_end_alerts_enabled": "Campaign End Alerts (24h warning)"
}

for filepath in sorted(lang_dir.glob("*.json")):
    if filepath.name == "English.json":
        continue
    data = json.loads(filepath.read_text(encoding="utf-8"))
    # Add new settings keys if missing
    for key, value in new_settings_keys.items():
        if key not in data["gui"]["settings"]:
            data["gui"]["settings"][key] = value
    # Add stats tab if missing
    if "stats" not in data["gui"]["tabs"]:
        data["gui"]["tabs"]["stats"] = "Stats"
    filepath.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Updated {filepath.name}")

print("Done")
```

Save as `scripts/add_i18n_keys.py` and run: `python3 scripts/add_i18n_keys.py`

- [ ] **Step 4: Run translation test to verify**

```bash
cd /home/claude/twitchdrops && python3 -m pytest tests/test_translations.py -v
```

Expected: All tests pass (all language files have all GUISettings keys).

- [ ] **Step 5: Commit**

```bash
git add lang/ src/i18n/translator.py scripts/
git commit -m "feat: add i18n keys for notifications, stats tab, campaign alerts"
```

---

## Task 2: Web Config API — Push & Alert Settings

**Files:**
- Modify: `src/web/app.py`

- [ ] **Step 1: Add helper functions for push/alert config**

In `src/web/app.py`, after the existing `_load_web_config` / `_save_web_config` helpers (around line 75), add:

```python
def _get_push_config() -> dict:
    cfg = _load_web_config()
    return {
        "push_notifications_enabled": cfg.get("push_notifications_enabled", False),
        "push_sound_enabled": cfg.get("push_sound_enabled", True),
        "campaign_end_alerts_enabled": cfg.get("campaign_end_alerts_enabled", True),
    }
```

- [ ] **Step 2: Add GET endpoint for push config**

In `src/web/app.py`, after the existing `/api/web-config` endpoint, add:

```python
@app.get("/api/push-config")
async def get_push_config(request: Request):
    _require_auth(request)
    return _get_push_config()


@app.post("/api/push-config")
async def set_push_config(request: Request):
    _require_auth(request)
    body = await request.json()
    cfg = _load_web_config()
    for key in ("push_notifications_enabled", "push_sound_enabled", "campaign_end_alerts_enabled"):
        if key in body:
            cfg[key] = bool(body[key])
    _save_web_config(cfg)
    return {"ok": True}
```

- [ ] **Step 3: Write unit test**

Create `tests/test_push_config_api.py`:

```python
import json
import tempfile
import pathlib
import pytest

def test_get_push_config_defaults(monkeypatch, tmp_path):
    import src.web.app as app_module
    monkeypatch.setattr(app_module, "_WEB_CONFIG_FILE", tmp_path / "web_config.json")
    result = app_module._get_push_config()
    assert result == {
        "push_notifications_enabled": False,
        "push_sound_enabled": True,
        "campaign_end_alerts_enabled": True,
    }

def test_get_push_config_persisted(monkeypatch, tmp_path):
    import src.web.app as app_module
    cfg_file = tmp_path / "web_config.json"
    cfg_file.write_text(json.dumps({"push_notifications_enabled": True, "push_sound_enabled": False}))
    monkeypatch.setattr(app_module, "_WEB_CONFIG_FILE", cfg_file)
    result = app_module._get_push_config()
    assert result["push_notifications_enabled"] is True
    assert result["push_sound_enabled"] is False
```

- [ ] **Step 4: Run test**

```bash
cd /home/claude/twitchdrops && python3 -m pytest tests/test_push_config_api.py -v
```

Expected: 2 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/web/app.py tests/test_push_config_api.py
git commit -m "feat: add push/alert config API endpoints"
```

---

## Task 3: Drop Stats API Endpoint

**Files:**
- Modify: `src/web/app.py`
- Create: `tests/test_stats_api.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_stats_api.py`:

```python
import json
import pathlib
import pytest


def _make_history(tmp_path):
    data = [
        {"timestamp": "2026-06-10T10:00:00+00:00", "game": "R6", "drop": "Pack", "reward": "Pack 1", "image_url": "https://example.com/img.jpg"},
        {"timestamp": "2026-06-10T12:00:00+00:00", "game": "R6", "drop": "Pack", "reward": "Pack 2"},
        {"timestamp": "2026-06-11T08:00:00+00:00", "game": "Rust", "drop": "Skin", "reward": "Skin 1"},
        {"timestamp": "2026-06-11T09:00:00+00:00", "game": "R6", "drop": "Pack", "reward": "Pack 3"},
    ]
    f = tmp_path / "drops_history.json"
    f.write_text(json.dumps(data))
    return f


def test_aggregate_stats(tmp_path, monkeypatch):
    hist_file = _make_history(tmp_path)
    import src.web.app as app_module
    monkeypatch.setattr(app_module, "_DATA_DIR", tmp_path)

    result = app_module._aggregate_stats()
    assert result["total_claims"] == 4
    assert result["by_game"][0]["game"] == "R6"
    assert result["by_game"][0]["count"] == 3
    assert result["by_game"][1]["game"] == "Rust"
    assert result["by_game"][1]["count"] == 1
    assert len(result["recent"]) <= 10
    assert result["recent"][0]["image_url"] == "https://example.com/img.jpg"


def test_aggregate_stats_empty(tmp_path, monkeypatch):
    import src.web.app as app_module
    monkeypatch.setattr(app_module, "_DATA_DIR", tmp_path)
    result = app_module._aggregate_stats()
    assert result["total_claims"] == 0
    assert result["by_game"] == []
    assert result["by_day"] == []
    assert result["recent"] == []
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /home/claude/twitchdrops && python3 -m pytest tests/test_stats_api.py -v
```

Expected: FAIL with `AttributeError: module 'src.web.app' has no attribute '_aggregate_stats'`

- [ ] **Step 3: Implement `_aggregate_stats` and `/api/stats` endpoint**

In `src/web/app.py`, add the following function before the route definitions:

```python
def _aggregate_stats() -> dict:
    from collections import defaultdict
    hist_file = _DATA_DIR / "drops_history.json"
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

    return {
        "total_claims": len(history),
        "by_game": [{"game": g, "count": c} for g, c in sorted_games[:10]],
        "by_day": [{"date": d, "count": c} for d, c in sorted_days[-30:]],
        "recent": recent[:10],
    }
```

Then add the endpoint:

```python
@app.get("/api/stats")
async def get_stats(request: Request):
    _require_auth(request)
    return _aggregate_stats()
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /home/claude/twitchdrops && python3 -m pytest tests/test_stats_api.py -v
```

Expected: Both tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/web/app.py tests/test_stats_api.py
git commit -m "feat: add /api/stats endpoint for drop history aggregation"
```

---

## Task 4: Campaign Alert Service

**Files:**
- Create: `src/services/campaign_alert_service.py`
- Modify: `src/core/client.py`
- Create: `tests/test_campaign_alert.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_campaign_alert.py`:

```python
import asyncio
import json
import pathlib
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch


def _make_campaign(id, game_name, ends_in_hours, unclaimed_drops):
    c = MagicMock()
    c.id = id
    c.name = f"Campaign {id}"
    c.game = MagicMock()
    c.game.name = game_name
    c.ends_at = datetime.now(timezone.utc) + timedelta(hours=ends_in_hours)
    c.is_claimed = unclaimed_drops == 0
    c.unclaimed_drops = unclaimed_drops
    c.campaign_url = f"https://twitch.tv/drops/campaigns?dropID={id}"
    return c


def test_finds_expiring_campaigns():
    from src.services.campaign_alert_service import CampaignAlertService

    service = CampaignAlertService.__new__(CampaignAlertService)
    service._alerted: set = set()

    expiring = _make_campaign("c1", "R6", 10, 2)
    not_expiring = _make_campaign("c2", "Rust", 48, 1)
    already_claimed = _make_campaign("c3", "R6", 5, 0)
    already_alerted = _make_campaign("c4", "Rust", 3, 1)
    service._alerted.add("c4")

    campaigns = [expiring, not_expiring, already_claimed, already_alerted]
    result = service._get_campaigns_to_alert(campaigns)

    assert len(result) == 1
    assert result[0].id == "c1"


def test_already_alerted_not_repeated():
    from src.services.campaign_alert_service import CampaignAlertService

    service = CampaignAlertService.__new__(CampaignAlertService)
    service._alerted = {"c1"}
    campaign = _make_campaign("c1", "R6", 5, 2)
    result = service._get_campaigns_to_alert([campaign])
    assert result == []
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /home/claude/twitchdrops && python3 -m pytest tests/test_campaign_alert.py -v
```

Expected: FAIL with `ModuleNotFoundError` or `ImportError`

- [ ] **Step 3: Implement `CampaignAlertService`**

Create `src/services/campaign_alert_service.py`:

```python
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    from src.core.client import Twitch
    from src.models.campaign import DropsCampaign

logger = logging.getLogger("TwitchDrops")
_ALERT_THRESHOLD_HOURS = 24


class CampaignAlertService:
    def __init__(self, twitch: Twitch) -> None:
        self._twitch = twitch
        self._alerted: set[str] = set()
        from src.config import DATA_DIR
        self._alert_file = DATA_DIR / "alerted_campaigns.json"
        self._load_alerted()

    def _load_alerted(self) -> None:
        if self._alert_file.exists():
            try:
                self._alerted = set(json.loads(self._alert_file.read_text()))
            except Exception:
                self._alerted = set()

    def _save_alerted(self) -> None:
        self._alert_file.parent.mkdir(exist_ok=True)
        self._alert_file.write_text(json.dumps(list(self._alerted)))

    def _get_campaigns_to_alert(self, campaigns: list[DropsCampaign]) -> list[DropsCampaign]:
        now = datetime.now(timezone.utc)
        threshold = now + timedelta(hours=_ALERT_THRESHOLD_HOURS)
        result = []
        for c in campaigns:
            if c.id in self._alerted:
                continue
            if c.is_claimed:
                continue
            if c.ends_at <= threshold:
                result.append(c)
        return result

    async def check_and_alert(self, campaigns: list[DropsCampaign]) -> None:
        to_alert = self._get_campaigns_to_alert(campaigns)
        if not to_alert:
            return

        from src.web.app import _get_push_config
        push_cfg = _get_push_config()
        alerts_enabled = push_cfg.get("campaign_end_alerts_enabled", True)
        if not alerts_enabled:
            return

        webhook_url = getattr(self._twitch.settings, "discord_webhook_drops", "")

        for campaign in to_alert:
            hours_left = int((campaign.ends_at - datetime.now(timezone.utc)).total_seconds() / 3600)
            logger.info(f"Campaign ending alert: {campaign.name} ({hours_left}h left)")
            self._alerted.add(campaign.id)

            if webhook_url:
                embed = {
                    "title": "⏰ Campaign Ending Soon",
                    "description": f"**{campaign.name}** ends in ~{hours_left}h",
                    "color": 0xFF4500,
                    "fields": [
                        {"name": "Game", "value": campaign.game.name, "inline": True},
                        {"name": "Unclaimed Drops", "value": str(campaign.unclaimed_drops), "inline": True},
                    ],
                    "url": campaign.campaign_url,
                }
                asyncio.create_task(self._send_discord_webhook(webhook_url, {"embeds": [embed]}))

        self._save_alerted()

        # Notify frontend via socket for browser push
        from src.web.app import sio
        await sio.emit("campaign_end_alert", [
            {
                "id": c.id,
                "name": c.name,
                "game": c.game.name,
                "hours_left": int((c.ends_at - datetime.now(timezone.utc)).total_seconds() / 3600),
                "unclaimed_drops": c.unclaimed_drops,
            }
            for c in to_alert
        ])

    async def _send_discord_webhook(self, url: str, payload: dict) -> None:
        if not url:
            return
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10))
        except Exception as e:
            logger.debug(f"Campaign alert webhook failed: {e}")
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /home/claude/twitchdrops && python3 -m pytest tests/test_campaign_alert.py -v
```

Expected: Both tests pass.

- [ ] **Step 5: Wire campaign alert into `client.py`**

In `src/core/client.py`, find the `__init__` method and add the service initialization after the other services are created. Look for the block that creates `self._message_handler_service` or similar, then add:

```python
from src.services.campaign_alert_service import CampaignAlertService
# In __init__:
self._campaign_alert_service = CampaignAlertService(self)
```

Then find `fetch_inventory()` call in `_run()` (around line 315):

```python
await self.fetch_inventory()
```

After it, add:

```python
asyncio.create_task(
    self._campaign_alert_service.check_and_alert(list(self.inventory))
)
```

- [ ] **Step 6: Commit**

```bash
git add src/services/campaign_alert_service.py src/core/client.py tests/test_campaign_alert.py
git commit -m "feat: add campaign end alert service (Discord + push, 24h warning)"
```

---

## Task 5: Stats Tab UI

**Files:**
- Modify: `web/index.html`
- Modify: `web/static/app.js`

- [ ] **Step 1: Add Stats tab button in `web/index.html`**

In `web/index.html`, find the `<nav class="tabs">` section with the existing tab buttons:

```html
<button class="tab-button" data-tab="help">Help</button>
```

Add after it (before the system tab button):

```html
<button class="tab-button" data-tab="stats">Stats</button>
```

- [ ] **Step 2: Add Stats tab content in `web/index.html`**

After the closing `</div>` of `id="help-tab"`, add:

```html
<!-- Stats Tab -->
<div id="stats-tab" class="tab-content">
    <div style="padding: 16px; max-width: 900px; margin: 0 auto;">
        <h2 style="margin-bottom: 16px; color: var(--text);">Drop Statistics</h2>

        <!-- Summary row -->
        <div id="stats-summary" style="display:flex; gap:12px; margin-bottom:24px; flex-wrap:wrap;">
            <div class="stat-card">
                <div class="stat-value" id="stats-total">—</div>
                <div class="stat-label">Total Claims</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" id="stats-games">—</div>
                <div class="stat-label">Games</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" id="stats-last">—</div>
                <div class="stat-label">Last Claim</div>
            </div>
        </div>

        <!-- By Game bar chart -->
        <div class="stats-section">
            <h3>Claims by Game</h3>
            <div id="stats-by-game"></div>
        </div>

        <!-- By Day bar chart -->
        <div class="stats-section" style="margin-top:24px;">
            <h3>Claims per Day (last 30 days)</h3>
            <canvas id="stats-by-day-canvas" height="120" style="width:100%;"></canvas>
        </div>

        <!-- Recent claims -->
        <div class="stats-section" style="margin-top:24px;">
            <h3>Recent Claims</h3>
            <div id="stats-recent"></div>
        </div>
    </div>
</div>
```

- [ ] **Step 3: Add stats CSS in `web/index.html`**

In the `<style>` section, add:

```css
.stat-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px 24px;
    min-width: 120px;
    text-align: center;
}
.stat-value {
    font-size: 2em;
    font-weight: 700;
    color: var(--accent);
}
.stat-label {
    font-size: 0.85em;
    color: var(--text-muted);
    margin-top: 4px;
}
.stats-section h3 {
    color: var(--text);
    margin-bottom: 12px;
    font-size: 1em;
    font-weight: 600;
}
.stats-game-bar {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
}
.stats-game-bar-label {
    width: 160px;
    font-size: 0.85em;
    color: var(--text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.stats-game-bar-track {
    flex: 1;
    height: 18px;
    background: var(--border);
    border-radius: 4px;
    overflow: hidden;
}
.stats-game-bar-fill {
    height: 100%;
    background: var(--accent);
    border-radius: 4px;
    transition: width 0.4s;
}
.stats-game-bar-count {
    width: 32px;
    text-align: right;
    font-size: 0.85em;
    color: var(--text-muted);
}
.stats-recent-item {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 0;
    border-bottom: 1px solid var(--border);
}
.stats-recent-img {
    width: 40px;
    height: 40px;
    object-fit: cover;
    border-radius: 4px;
    background: var(--border);
}
.stats-recent-info { flex: 1; }
.stats-recent-reward { font-weight: 600; font-size: 0.9em; color: var(--text); }
.stats-recent-meta { font-size: 0.8em; color: var(--text-muted); }
```

- [ ] **Step 4: Add stats rendering function to `web/static/app.js`**

Find the tab click handling in `app.js` (around the `loadTab` or `data-tab` click handler). Add a call to load stats when the stats tab is clicked.

Then add this function:

```javascript
async function loadStats() {
    try {
        const resp = await fetch(API_BASE + '/api/stats');
        const data = await resp.json();

        // Summary
        document.getElementById('stats-total').textContent = data.total_claims;
        document.getElementById('stats-games').textContent = data.by_game.length;
        const lastClaim = data.recent[0]?.timestamp;
        document.getElementById('stats-last').textContent = lastClaim
            ? new Date(lastClaim).toLocaleDateString()
            : '—';

        // By game bars
        const maxCount = data.by_game[0]?.count || 1;
        const gameContainer = document.getElementById('stats-by-game');
        gameContainer.innerHTML = '';
        for (const { game, count } of data.by_game) {
            const pct = Math.round((count / maxCount) * 100);
            gameContainer.insertAdjacentHTML('beforeend', `
                <div class="stats-game-bar">
                    <span class="stats-game-bar-label" title="${game}">${game}</span>
                    <div class="stats-game-bar-track">
                        <div class="stats-game-bar-fill" style="width:${pct}%"></div>
                    </div>
                    <span class="stats-game-bar-count">${count}</span>
                </div>
            `);
        }

        // By day canvas chart
        const canvas = document.getElementById('stats-by-day-canvas');
        if (canvas && data.by_day.length > 0) {
            const ctx = canvas.getContext('2d');
            const W = canvas.offsetWidth || 600;
            canvas.width = W;
            canvas.height = 120;
            const maxDay = Math.max(...data.by_day.map(d => d.count), 1);
            const barW = Math.max(4, Math.floor(W / data.by_day.length) - 2);
            ctx.clearRect(0, 0, W, 120);
            data.by_day.forEach(({ date, count }, i) => {
                const h = Math.round((count / maxDay) * 100);
                const x = i * (barW + 2);
                ctx.fillStyle = 'var(--accent, #9147ff)';
                ctx.fillRect(x, 120 - h, barW, h);
            });
        }

        // Recent claims
        const recentEl = document.getElementById('stats-recent');
        recentEl.innerHTML = '';
        for (const drop of data.recent) {
            const date = drop.timestamp ? new Date(drop.timestamp).toLocaleString() : '';
            recentEl.insertAdjacentHTML('beforeend', `
                <div class="stats-recent-item">
                    ${drop.image_url
                        ? `<img class="stats-recent-img" src="${drop.image_url}" alt="">`
                        : `<div class="stats-recent-img"></div>`}
                    <div class="stats-recent-info">
                        <div class="stats-recent-reward">${drop.reward || drop.drop}</div>
                        <div class="stats-recent-meta">${drop.game} · ${date}</div>
                    </div>
                </div>
            `);
        }
    } catch (e) {
        console.error('Failed to load stats:', e);
    }
}
```

- [ ] **Step 5: Hook `loadStats()` into tab switching**

Find the tab click event listener in `app.js`. Add inside the handler, after the existing tab logic:

```javascript
if (tabName === 'stats') loadStats();
```

- [ ] **Step 6: Wire stats tab into `applyTranslations` in `app.js`**

Find the `applyTranslations` function. In the `tabButtons` object add:

```javascript
'stats': document.querySelector('[data-tab="stats"]'),
```

And in the block that sets tab text content, add:

```javascript
if (tabButtons.stats && t.gui?.tabs) tabButtons.stats.textContent = t.gui.tabs.stats;
```

- [ ] **Step 7: Commit**

```bash
git add web/index.html web/static/app.js
git commit -m "feat: add Stats tab with claims by game, by day chart, recent claims"
```

---

## Task 6: Notifications Settings UI

**Files:**
- Modify: `web/index.html`
- Modify: `web/static/app.js`

- [ ] **Step 1: Add Notifications section to Settings tab in `web/index.html`**

Find the Settings tab content in `web/index.html`. Locate the Discord Webhook section or scheduler section. Add a new section after the scheduler section:

```html
<!-- Notifications Section -->
<section class="panel" id="settings-notifications">
    <h2 id="settings-notifications-header">Notifications</h2>

    <div class="settings-row">
        <label class="toggle-label" for="push-enabled-toggle">
            <span id="settings-push-enabled-label">Browser Push Notifications</span>
            <input type="checkbox" id="push-enabled-toggle" class="toggle-input">
            <span class="toggle-slider"></span>
        </label>
    </div>

    <div class="settings-row">
        <label class="toggle-label" for="push-sound-toggle">
            <span id="settings-push-sound-label">Notification Sound</span>
            <input type="checkbox" id="push-sound-toggle" class="toggle-input">
            <span class="toggle-slider"></span>
        </label>
    </div>

    <div class="settings-row">
        <label class="toggle-label" for="campaign-alerts-toggle">
            <span id="settings-campaign-alerts-label">Campaign End Alerts (24h warning)</span>
            <input type="checkbox" id="campaign-alerts-toggle" class="toggle-input">
            <span class="toggle-slider"></span>
        </label>
    </div>
</section>
```

- [ ] **Step 2: Add push config load/save logic to `app.js`**

Add these functions:

```javascript
async function loadPushConfig() {
    try {
        const resp = await fetch(API_BASE + '/api/push-config');
        const cfg = await resp.json();
        const pushToggle = document.getElementById('push-enabled-toggle');
        const soundToggle = document.getElementById('push-sound-toggle');
        const alertsToggle = document.getElementById('campaign-alerts-toggle');
        if (pushToggle) pushToggle.checked = !!cfg.push_notifications_enabled;
        if (soundToggle) soundToggle.checked = cfg.push_sound_enabled !== false;
        if (alertsToggle) alertsToggle.checked = cfg.campaign_end_alerts_enabled !== false;
    } catch (e) {
        console.error('Failed to load push config:', e);
    }
}

async function savePushConfig() {
    const pushToggle = document.getElementById('push-enabled-toggle');
    const soundToggle = document.getElementById('push-sound-toggle');
    const alertsToggle = document.getElementById('campaign-alerts-toggle');
    const payload = {
        push_notifications_enabled: pushToggle?.checked || false,
        push_sound_enabled: soundToggle?.checked !== false,
        campaign_end_alerts_enabled: alertsToggle?.checked !== false,
    };
    try {
        await fetch(API_BASE + '/api/push-config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
    } catch (e) {
        console.error('Failed to save push config:', e);
    }
}
```

- [ ] **Step 3: Attach save handlers and load on Settings tab open**

In the tab click handler, add:

```javascript
if (tabName === 'settings') loadPushConfig();
```

Attach change listeners (place in the main init block):

```javascript
['push-enabled-toggle', 'push-sound-toggle', 'campaign-alerts-toggle'].forEach(id => {
    document.getElementById(id)?.addEventListener('change', savePushConfig);
});
```

For push notification permission: when `push-enabled-toggle` is checked, also request permission:

```javascript
document.getElementById('push-enabled-toggle')?.addEventListener('change', async function() {
    if (this.checked && 'Notification' in window && Notification.permission !== 'granted') {
        await Notification.requestPermission();
    }
    savePushConfig();
});
```

- [ ] **Step 4: Wire i18n keys in `applyTranslations`**

In the `applyTranslations` function in `app.js`, add handling for the new notifications settings labels:

```javascript
if (t.gui?.settings) {
    const s = t.gui.settings;
    const notifHeader = document.getElementById('settings-notifications-header');
    if (notifHeader && s.notifications_header) notifHeader.textContent = s.notifications_header;
    const pushLabel = document.getElementById('settings-push-enabled-label');
    if (pushLabel && s.push_enabled) pushLabel.textContent = s.push_enabled;
    const soundLabel = document.getElementById('settings-push-sound-label');
    if (soundLabel && s.push_sound) soundLabel.textContent = s.push_sound;
    const alertLabel = document.getElementById('settings-campaign-alerts-label');
    if (alertLabel && s.campaign_end_alerts_enabled) alertLabel.textContent = s.campaign_end_alerts_enabled;
}
```

- [ ] **Step 5: Commit**

```bash
git add web/index.html web/static/app.js
git commit -m "feat: add Notifications settings section with push/sound/alerts toggles"
```

---

## Task 7: Browser Push Notification Logic

**Files:**
- Modify: `web/static/app.js`

- [ ] **Step 1: Replace existing `socket.on('notification', ...)` handler with push-aware version**

Find the existing handler in `app.js`:

```javascript
socket.on('notification', (data) => {
```

The existing handler already plays sound and shows in-page notification. Extend it to also show a browser push notification when enabled. Add inside the handler, after the existing logic:

```javascript
socket.on('notification', async (data) => {
    // ... existing in-page notification code stays ...

    // Browser push notification
    const pushToggle = document.getElementById('push-enabled-toggle');
    const soundToggle = document.getElementById('push-sound-toggle');
    const pushEnabled = pushToggle?.checked;
    const soundEnabled = soundToggle?.checked !== false;

    if (pushEnabled && 'Notification' in window && Notification.permission === 'granted') {
        const title = data.drop_name || data.title || 'Drop Claimed!';
        const body = data.game || data.body || '';
        const icon = data.image_url || undefined;
        new Notification(title, { body, icon });
    }

    if (soundEnabled && data.type !== 'points') {
        try {
            const audio = new Audio(STATIC_BASE + '/notification.mp3');
            audio.play().catch(() => {});
        } catch (e) {}
    }
});
```

NOTE: Review the existing handler carefully before replacing — preserve all existing behavior and only ADD the push notification + move sound check to respect the sound toggle.

- [ ] **Step 2: Add campaign end alert socket handler**

Add a new socket handler for campaign end alerts:

```javascript
socket.on('campaign_end_alert', (campaigns) => {
    const pushToggle = document.getElementById('push-enabled-toggle');
    if (!pushToggle?.checked) return;
    if (!('Notification' in window) || Notification.permission !== 'granted') return;

    for (const c of campaigns) {
        new Notification(`⏰ Campaign ending in ~${c.hours_left}h`, {
            body: `${c.name} — ${c.game} (${c.unclaimed_drops} drops left)`,
        });
    }
});
```

- [ ] **Step 3: Commit**

```bash
git add web/static/app.js
git commit -m "feat: browser push notifications for drops and campaign end alerts"
```

---

## Task 8: Restart and Verify

- [ ] **Step 1: Run full test suite**

```bash
cd /home/claude/twitchdrops && python3 -m pytest tests/ -v 2>&1 | tail -20
```

Expected: All tests pass.

- [ ] **Step 2: Restart PM2 instances**

```bash
pm2 restart twitchdrops twitchdrops2
pm2 logs twitchdrops --lines 10 --nostream
```

Expected: Both instances online, no errors in logs.

- [ ] **Step 3: Push to GitHub**

```bash
cd /home/claude/twitchdrops && git push simpliaj main
```

- [ ] **Step 4: Notify Discord**

```bash
node /home/claude/.claude/discord/discord.js twitchdropsminer "✅ Deployed: Push Notifications, Stats Tab, Campaign End Alerts — all 3 features live on both instances"
```
