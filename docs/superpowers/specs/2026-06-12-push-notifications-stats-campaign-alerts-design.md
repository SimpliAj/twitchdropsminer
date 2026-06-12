# Design: Push Notifications, Drop Stats, Campaign End Alerts

**Date:** 2026-06-12  
**Project:** TwitchDropsMiner (SimpliAj fork)

---

## 1. Browser Push Notifications

### Goal
Show a native browser notification when a drop is claimed, with toggles for on/off and sound on/off.

### Architecture
- Uses existing `new Notification()` API (permission request already in `app.js`)
- No Service Worker — regular foreground notifications suffice
- Settings stored in `web_config.json`: `push_notifications_enabled` (bool), `push_sound_enabled` (bool)
- New backend fields in `WebConfigUpdate` model and `web_config.json` defaults

### Flow
1. Drop claimed → backend fires `claimed_drop` socket event (already exists)
2. `app.js` `socket.on('notification', ...)` handler checks `push_notifications_enabled`
3. If enabled: `new Notification(dropName, { body: gameName, icon: imageUrl })`
4. If `push_sound_enabled`: play existing `notification.mp3`

### Settings UI
- New "Notifications" section in Settings tab
- Toggle: Push Notifications (on/off)
- Toggle: Sound (on/off)

### i18n
New keys in `gui.settings` section of `English.json`:
- `notifications_header`, `push_enabled`, `push_sound`

---

## 2. Drop Stats Dashboard (New Tab)

### Goal
Visual overview of all claimed drops from `drops_history.json`.

### Architecture
- New "Stats" tab (8th tab, between Help and System or after System)
- New endpoint: `GET /api/stats` — reads `drops_history.json`, returns aggregated data
- Frontend renders with vanilla JS canvas charts

### API Response Shape
```json
{
  "total_claims": 42,
  "by_game": [{"game": "R6", "count": 10}, ...],
  "by_day": [{"date": "2026-06-10", "count": 3}, ...],
  "recent": [{ "timestamp", "game", "drop", "reward", "image_url" }]
}
```

### UI Sections
1. Summary row: Total Claims, Games Played, Last Claim date
2. Bar chart: Claims per Game (top 10)
3. Line/bar chart: Claims per Day (last 30 days)
4. Recent claims list (last 10, with image thumbnail if available)

### i18n
New `stats` key in `gui.tabs` + `gui.stats` section with labels.

---

## 3. Campaign End Alerts

### Goal
Alert user (Discord + browser push) when a campaign ends within 24h and drops are not yet fully claimed.

### Architecture
- Hook into `fetch_inventory()` completion in `client.py`
- New `CampaignAlertService` (or inline in existing notify flow)
- Persists notified campaign IDs in `data/alerted_campaigns.json` to avoid duplicate alerts
- Sends via existing Discord webhook service + triggers browser push

### Logic
```
for campaign in inventory:
    if campaign.id in alerted_campaigns: skip
    if campaign.ends_at - now < 24h AND campaign has unclaimed drops:
        send_discord_alert(campaign)
        send_push_notification(campaign)
        alerted_campaigns.add(campaign.id)
        save alerted_campaigns.json
```

### Discord Message
Embed with: Campaign name, Game, ends_at timestamp, unclaimed drop count, link to twitch.tv/drops/campaigns.

### Settings UI
- Toggle in Settings → "Notifications": Campaign End Alerts (on/off)
- `campaign_end_alerts_enabled` in `web_config.json`

### i18n
New keys: `campaign_end_alert_header`, `campaign_end_alert_enabled`

---

## 4. i18n Wiring

- All new string keys added to `English.json` only
- TypedDicts in `translator.py` updated to include new fields
- Other language files untouched — contributors can add translations
- `applyTranslations()` in `app.js` extended for new sections

---

## Files Changed

| File | Change |
|------|--------|
| `lang/English.json` | New keys for all 3 features |
| `src/i18n/translator.py` | New TypedDict fields |
| `src/config/settings.py` | New web_config fields |
| `src/web/app.py` | `/api/stats` endpoint, web_config fields |
| `src/services/campaign_alert_service.py` | New service (Campaign End Alerts) |
| `src/core/client.py` | Hook campaign alert check after fetch_inventory |
| `web/index.html` | Stats tab, Notifications settings section |
| `web/static/app.js` | Push notification logic, stats rendering, translations |
