# Upstream Issue Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 4 upstream issues (#38 GQL crash, #25/#26 Wanted Queue/expired drops, #46 Drop Blacklist) and prepare PR text for each.

**Architecture:** All backend fixes in `src/`, blacklist setting added to `Settings` dataclass + defaults + UI. No new files needed.

**Tech Stack:** Python 3.12, FastAPI, existing Settings/models pattern

---

### Task 1: Fix #38 — GQL crash logs game name, doesn't silently swallow

**Files:**
- Modify: `src/core/client.py` (line ~427 — the `except Exception: pass`)

The crash in `__main__.py` was from an older code path. Current code already has `except Exception: pass` but silently swallows it. Improve: log the game name so users can see which game caused the skip.

- [ ] **Step 1: Improve exception logging**

In `src/core/client.py`, find:
```python
                    try:
                        new_channels.update(await self.get_live_streams(game, drops_enabled=True))
                    except Exception:
                        pass
```
Replace with:
```python
                    try:
                        new_channels.update(await self.get_live_streams(game, drops_enabled=True))
                    except Exception as exc:
                        logger.warning(f"Failed to fetch channels for {game.name}: {exc} — skipping game")
```

- [ ] **Step 2: Commit**
```bash
git add src/core/client.py
git commit -m "fix: log game name when channel fetch fails instead of silently skipping (#38)"
```

**PR text for #38:**
```
## Fix: GQL PersistedQueryNotFound no longer crashes the miner

The crash at `__main__.py` occurred because `GQLException` from a failed
`GameDirectory` GQL query propagated as fatal. The existing `except Exception`
block was already catching it but silently swallowing it.

This PR improves the handling by logging the game name and error so users
can identify which game causes the failure, while the miner continues mining
other games.

Tested with Path of Exile which triggers PersistedQueryNotFound — miner
now skips the game with a warning log and continues normally.
```

---

### Task 2: Fix #25/#26 — Finished/expired/sub drops in Wanted Queue

**Files:**
- Modify: `src/services/stream_selector.py` (inner loop ~line 38)

**Root cause:**
- `drop.is_claimed` is checked but NOT `drop.required_minutes <= 0` (sub/0-min drops pass filter)
- Expired individual drops within a valid campaign also appear (no `_base_can_earn()` check on inner loop)

- [ ] **Step 1: Fix inner drop loop in `_get_wanted_game_tree`**

In `src/services/stream_selector.py`, find:
```python
                wanted_drops = []
                for drop in campaign.drops:
                    if drop.is_claimed:
                        continue

                    filtered_benefits = drop.get_wanted_unclaimed_benefits(mining_benefits)
```
Replace with:
```python
                wanted_drops = []
                for drop in campaign.drops:
                    if drop.is_claimed or drop.required_minutes <= 0:
                        continue
                    if not drop._base_can_earn():
                        continue

                    filtered_benefits = drop.get_wanted_unclaimed_benefits(mining_benefits)
```

- [ ] **Step 2: Commit**
```bash
git add src/services/stream_selector.py
git commit -m "fix: exclude 0-min sub drops and expired drops from Wanted Queue (#25 #26)"
```

**PR text for #25/#26:**
```
## Fix: Finished campaigns and sub drops no longer appear in Wanted Drop Queue

Two related bugs caused the Wanted Queue to show items that can never be earned:

1. **Sub drops (0-minute required_minutes)**: Were not filtered — only `is_claimed`
   was checked. Added `or drop.required_minutes <= 0` to skip them.

2. **Expired drops**: Individual drops within a still-active campaign could be
   expired (past their `ends_at`). Added `drop._base_can_earn()` check to filter
   drops that are outside their valid time window.

Fixes #25, #26.
```

---

### Task 3: Feature #46 — Drop Name Blacklist

**Files:**
- Modify: `src/config/settings.py` — add `drop_name_blacklist: list[str]` field
- Modify: `src/services/stream_selector.py` — filter blacklisted drops
- Modify: `web/index.html` — add blacklist input UI in Settings tab
- Modify: `web/static/app.js` — load/save blacklist setting

**Flow:** User enters keywords in Settings → saved to settings.json → `stream_selector` skips drops whose name contains any keyword (case-insensitive) → both Wanted Queue and mining skip them.

- [ ] **Step 1: Add to Settings dataclass**

In `src/config/settings.py`:

Add to `default_settings` dict:
```python
    "drop_name_blacklist": [],
```

Add to `Settings` dataclass:
```python
    drop_name_blacklist: list[str]
```

- [ ] **Step 2: Add blacklist filter in stream_selector**

In `src/services/stream_selector.py`, after `mining_benefits = settings.mining_benefits`:
```python
        blacklist = [kw.lower() for kw in settings.drop_name_blacklist if kw.strip()]
```

Then in the inner drop loop, after the existing `continue` checks:
```python
                    if blacklist and any(kw in drop.name.lower() for kw in blacklist):
                        continue
```

- [ ] **Step 3: Add UI in Settings tab**

In `web/index.html`, find the Settings tab idle channels section and add after it:
```html
                    <div class="settings-group">
                        <label class="settings-label">Drop Name Blacklist</label>
                        <div style="font-size:0.8rem;color:#adadb8;margin-bottom:6px;">
                            Keywords to skip (comma-separated). Drops containing these words are ignored. Useful for regional/exclusive drops (e.g. "JP", "Korea").
                        </div>
                        <input type="text" id="drop-blacklist-input" class="settings-input"
                            placeholder="JP, Korea, (KR), exclusive..." style="width:100%;">
                    </div>
```

- [ ] **Step 4: Wire up JS — load and save**

In `web/static/app.js`, in `updateSettingsUI` function where settings are loaded into UI, add:
```javascript
    const blacklistEl = document.getElementById('drop-blacklist-input');
    if (blacklistEl) blacklistEl.value = (settings.drop_name_blacklist || []).join(', ');
```

In `saveSettings` function where settings are read from UI, add:
```javascript
        drop_name_blacklist: (document.getElementById('drop-blacklist-input')?.value || '')
            .split(',').map(s => s.trim()).filter(Boolean),
```

Add change listener in the init block:
```javascript
    document.getElementById('drop-blacklist-input')?.addEventListener('change', saveSettings);
```

- [ ] **Step 5: Commit**
```bash
git add src/config/settings.py src/services/stream_selector.py web/index.html web/static/app.js
git commit -m "feat: drop name blacklist — skip regional/exclusive drops by keyword (#46)"
```

**PR text for #46:**
```
## Feature: Drop Name Blacklist

Adds a configurable keyword blacklist to skip regional/exclusive drops
(e.g. Japan-only, Korea-only) that the miner would get stuck on.

**How it works:**
- New setting `drop_name_blacklist` (list of strings, case-insensitive)
- Configurable via Settings tab → "Drop Name Blacklist" input (comma-separated)
- Any drop whose name contains a blacklisted keyword is skipped in the
  Wanted Queue and not mined

**Example:** Adding "JP", "(KR)", "Japan" skips Black Desert JP-exclusive drops
while still mining global campaigns for the same game.

Implements the proof-of-concept from the issue description in a configurable,
UI-driven way.

Fixes #46.
```

---

### Task 4: Fix #56 — Link status tooltip + Check for Drops hint

**Files:**
- Modify: `web/static/app.js` — improve "not linked" display in inventory to hint at Check for Drops button

The link status (`campaign.linked`) comes from GQL data fetched at inventory time.
If a user just linked their account, it won't reflect until the next inventory refresh.
This is not a code bug — but the UX is confusing. Fix: add a note in the "Not Linked"
badge that clicking "Check for Drops" will refresh.

- [ ] **Step 1: Find not-linked rendering in inventory**

In `web/static/app.js`, find where `not_linked` badge is rendered for campaigns.

```bash
grep -n "not_linked\|not-linked\|notLinked" web/static/app.js | head -10
```

- [ ] **Step 2: Add hint text to not-linked tooltip**

Find the not-linked span/badge creation and add `title` attribute:
```javascript
notLinkedEl.title = "Your account is not linked to this game. Link at twitch.tv/drops/campaigns, then click 'Check for Drops' to refresh.";
```

- [ ] **Step 3: Commit**
```bash
git add web/static/app.js
git commit -m "fix: improve not-linked tooltip — hint to use Check for Drops after linking (#56)"
```

**PR text for #56:**
```
## Fix: Incorrect link status display — improved UX

The "not linked" status is fetched from Twitch GQL at inventory load time.
If a user links their game account on Twitch after the miner has already
loaded, the status appears as "not linked" until the next refresh cycle.

This PR improves the UX by:
- Adding a tooltip to the "Not Linked" badge explaining that the status
  is fetched at load time
- Hinting to click "Check for Drops" to trigger an immediate refresh

Note: The underlying data is correct — it reflects the state at time of fetch.
The "Check for Drops" button (or waiting for the auto-refresh interval) will
update the status after linking on Twitch.

Addresses #56.
```
