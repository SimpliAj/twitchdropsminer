# 🌟 Twitch Drops Miner — SimpliAj Edition

> 🎮 **Automate Twitch Drop Farming — Effortlessly, Headlessly, and Bandwidth-Free**

<p align="center">
  <a href="https://github.com/SimpliAj/twitchdropsminer/stargazers"><img src="https://img.shields.io/github/stars/SimpliAj/twitchdropsminer?style=for-the-badge&color=yellow" alt="Stars"></a>
  <a href="https://github.com/SimpliAj/twitchdropsminer/releases"><img src="https://img.shields.io/github/v/release/SimpliAj/twitchdropsminer?style=for-the-badge&color=brightgreen" alt="Release"></a>
  <a href="https://github.com/SimpliAj/twitchdropsminer/blob/main/LICENSE"><img src="https://img.shields.io/github/license/SimpliAj/twitchdropsminer?style=for-the-badge&color=orange" alt="License"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.12+-blue?style=for-the-badge&logo=python" alt="Python"></a>
</p>

**TwitchDropsMiner watches Twitch drop campaigns for you** — no video stream, no browser tab, no bandwidth. It sends the same "watch" heartbeats Twitch's own player sends, discovers active campaigns automatically, claims every drop the moment it's earned, and runs headlessly behind a clean web dashboard you can check from your phone.

This is an **independently developed and maintained** fork originally based on [rangermix/TwitchDropsMiner](https://github.com/rangermix/TwitchDropsMiner), which itself descends from [DevilXD/TwitchDropsMiner](https://github.com/DevilXD/TwitchDropsMiner). It has diverged significantly and is now its own standalone project — a full dashboard redesign, multi-account fleet management, an auto-betting engine, a Discord bot, and a large list of reliability fixes on top of the original streamless-mining core.

Upstream changes that make sense will continue to be merged when applicable, but this project follows its own roadmap.

---

## ✨ Features

- 🚀 **Streamless Mining** — Earn drops without streaming video, by sending Twitch watch events directly
- 🔍 **Automatic Campaign Discovery** — Detects new drop campaigns and switches to them automatically
- ⚙️ **Auto Channel Switching** — Always mines the best available stream for the highest-priority game with progress remaining
- 💾 **Persistent Login** — OAuth device-flow login, saved via cookies, survives restarts
- 🕹️ **Extraction Console Dashboard** — A from-scratch web UI redesign: a dark, instrument-panel-style control room with a 7-tab layout (Main, Inventory, Channel Points, History, Analytics, Settings, System) plus a Help wiki tab
- 👥 **Multi-Account Fleet Management** — Run unlimited isolated accounts and manage all of them from one "Manage Accounts" view with live fleet status and bulk actions/settings
- 💰 **Channel Points Auto-Claimer** — Bonus chests claimed automatically via WebSocket (PubSub) with a 60s polling fallback
- 💤 **Idle Watch** — Farms channel points on chosen (or auto-followed) channels whenever no drop campaign is active
- 🎯 **Auto-Betting on Predictions** — Optional, off by default: automatically places Twitch Prediction bets using one of four strategies, with per-channel overrides
- 🚫 **Fine-Grained Blacklisting** — Blacklist by game, by drop name (keyword), by exact drop ID, or ignore a single campaign without touching the rest of that game
- 🤖 **Discord Bot** — Slash-command pairing, live-updating dashboard embeds, and drop/points notifications across multiple servers
- 🔔 **Discord Webhooks** — Separate webhook URLs for drops and channel points, with a one-click test button
- 🌍 **19-Language UI** — Actively audited for translation-key coverage, not just "has a language file"
- 🛡️ **Safe Frontend Rendering** — Dynamic UI content is built with DOM APIs, not innerHTML, to avoid HTML injection
- 🔒 **Password-Protected Dashboard** — Optional `WEB_PASSWORD` lock with a 30-day session cookie, for safely exposing the dashboard remotely
- 🧩 **Docker-Ready** — Pre-built multi-arch images, or build from source; one command to deploy anywhere

---

## 🧰 Quick Start

### 🐳 Pre-built Image (Recommended)

No need to clone or build — pull the image directly from Docker Hub:

```yaml
# docker-compose.yml
services:
  twitch-drops-miner:
    image: gitsimpliaj/twitch-drops-miner:latest
    container_name: twitch-drops-miner
    ports:
      - "8080:8080"
    volumes:
      - ./data:/app/data
    environment:
      - TZ=Europe/Vienna           # Set your timezone
      - WEB_PASSWORD=yourpassword  # Optional: lock the dashboard
    restart: always
```

```bash
docker compose up -d
```

Visit 👉 **<http://localhost:8080>**

Images are built automatically for `linux/amd64` and `linux/arm64` on every release.
Also available on GHCR: `ghcr.io/simpliaj/twitchdropsminer:latest`

### 🔨 Build from Source with Docker

```bash
git clone https://github.com/SimpliAj/twitchdropsminer.git
cd twitchdropsminer
docker compose up -d
```

### 🧑‍💻 From Source (without Docker)

Requires Python 3.12+.

```bash
pip install -e .
python main.py
```

Visit 👉 **<http://localhost:8080>**

---

## 👥 Multi-Account: Two Ways to Run Several Accounts

There are two different features for running more than one Twitch account — pick based on whether you want them sharing one process or fully separate ones.

### Fleet Management (single instance, many accounts)

Each Twitch account lives in its own isolated `data/accounts/<name>/` directory (cookies, settings, drop history, channel points). Add, switch, rename, and delete accounts from **System → Accounts** in the dashboard — no config files needed.

Click **⚙ Manage Accounts** in the header to open the fleet manager:

- **Fleet Status table** — one row per registered account showing live status, what it's currently watching, drops claimed today, and last-active time
- **Bulk Actions** — apply an operation to every selected account at once:
  - *Start Idle-Watch (Followed)*
  - *Start Drop Mining (Selected)*
  - *Pause / Stop All Selected*
- **Bulk Settings** — push a shared **Games to Watch** priority list or **Blacklisted Games** change across every selected account in one step, instead of editing each account's settings individually

### Multi-Account Parallel Mode (separate processes)

Run unlimited fully independent miner processes at once — each with its own port, its own data directory, and its own login session. Useful when you want hard process isolation (e.g. separate proxies per account) rather than one dashboard managing several logins.

- **Dynamic management** — add/remove instances from **System → Instances**; no manual config editing
- Instance 1 runs on port **8080** with data in `data/` (always present, cannot be removed)
- Additional instances use ports **8082, 8084, ...** and data dirs `data2/`, `data3/`, ...
- Nginx config regenerates automatically when instances are added or removed
- A proxy warning is shown in the dashboard when running 3+ instances on one IP (Twitch may flag the accounts)
- Configured via `TDM_PORT` (listening port) and `TDM_DATA_DIR` (data directory) environment variables

**Docker Compose (two instances):**

```yaml
services:
  tdm-account1:
    build: .
    ports:
      - "8080:8080"
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
    environment:
      - TZ=Europe/Vienna
      - WEB_PASSWORD=yourpassword
      - TDM_PORT=8080
      - TDM_DATA_DIR=data
    restart: unless-stopped

  tdm-account2:
    build: .
    ports:
      - "8082:8082"
    volumes:
      - ./data2:/app/data
      - ./logs2:/app/logs
    environment:
      - TZ=Europe/Vienna
      - WEB_PASSWORD=yourpassword
      - TDM_PORT=8082
      - TDM_DATA_DIR=data
    restart: unless-stopped
```

**From source — PM2 (two named processes):**

```bash
TDM_PORT=8080 TDM_DATA_DIR=data      pm2 start main.py --name twitchdrops  --interpreter python3
TDM_PORT=8082 TDM_DATA_DIR=data2     pm2 start main.py --name twitchdrops2 --interpreter python3
pm2 save
```

**From source — two terminals:**

```bash
# Terminal 1
TDM_PORT=8080 TDM_DATA_DIR=data   python main.py

# Terminal 2
TDM_PORT=8082 TDM_DATA_DIR=data2  python main.py
```

**Nginx reverse proxy (single domain, two accounts):**

```nginx
server {
    listen 443 ssl;
    server_name tdm.example.xyz;

    # Account 1 — root path
    location / {
        proxy_pass         http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade $http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host $host;
    }

    # Account 2 — /acc2/ path
    location /acc2/ {
        proxy_pass         http://127.0.0.1:8082/;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade $http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host $host;
    }
}
```

**Accessing both dashboards:**
- Account 1: `https://tdm.example.xyz/` (or `http://localhost:8080`)
- Account 2: `https://tdm.example.xyz/acc2/` (or `http://localhost:8082`)
- The header shows account switcher buttons labeled with each account's actual Twitch username once logged in. Click to jump between dashboards, or append `?acc=2` to the URL to go directly to account 2.

---

## 🌈 Using the Web App

1. Open `http://localhost:8080`
2. Log in with your Twitch account (OAuth device flow)
3. The miner auto-fetches available campaigns
4. Go to **Settings → Games to Watch** and select games:
   - **Select Linked** — auto-selects games where your account is linked
   - **Add Game** — add any custom game by name
   - **Drag to reorder** — top = highest priority
   - **Select All / Deselect All** for quick changes
5. Click **Reload** to apply changes
6. TDM starts mining drops automatically 🎉

📝 **Tip:** Make sure your Twitch account is linked to your game accounts →
👉 [https://www.twitch.tv/drops/campaigns](https://www.twitch.tv/drops/campaigns)

### Channel Points

- Enable **Auto-claim bonus channel points** in Settings to claim chests automatically
- Add channels to **Idle Watch** to keep earning points when no drops are active
- Live balance is shown in the **Main** tab; full earn/spend history lives in the **Channel Points** tab
- Click the **Drops Today** stat on the Main tab to open a popup listing exactly what's been claimed so far today, most recent first

### Blacklisting & Ignoring

TDM has four separate ways to exclude content, from broadest to narrowest:

| Scope | Where | Effect |
|-------|-------|--------|
| **Blacklisted Games** | Settings → Games to Watch | The game is never auto-added or mined at all |
| **Ignore Campaign** | Inventory card → 🚫 Ignore | One specific campaign for a game is skipped; the game's other campaigns keep mining normally |
| **Drop Name Blacklist** | Settings → Blacklist | Any drop whose name contains a listed keyword is skipped |
| **Blacklisted Drop IDs** | Settings, or the 🚫 button next to the active drop | One exact drop (e.g. a stuck/broken quest) is permanently excluded; everything else for that game keeps mining |

### Auto-Betting (Predictions)

Configure under **Settings → Predictions → Auto-Bet**. Disabled by default — nothing is wagered unless you turn it on.

| Strategy | How it picks an outcome |
|----------|--------------------------|
| **SMART** | Compares vote share between the top two outcomes; only bets if the gap is at least the configured **Bet Gap %**, otherwise skips as too close to call |
| **PERCENTAGE** | Always bets on the outcome with the most points already wagered (the crowd favorite by points) |
| **HIGH_ODDS** | Always bets on the underdog — fewest points wagered, highest payout if it wins |
| **MOST_VOTED** | Always bets on whichever outcome the most individual users picked |

Other controls: bet size as a percentage of balance, a hard max-points-per-bet cap, a minimum balance floor below which the miner sits out, a configurable delay before betting (auto-shortened if the prediction's own lock window is shorter), a channel whitelist, and per-channel overrides for strategy/percentages/delay. Every bet is logged to **Analytics → Predictions History**, and a win/loss Discord embed is posted automatically if a channel-points webhook is configured.

---

## 🤖 Discord Bot Integration

A dedicated Discord bot that pairs with your miner instance and sends rich, live-updating notifications — no webhook URLs needed.

**Slash Commands**
| Command | Description |
|---------|-------------|
| `/link <url> <code>` | Pair the bot with your miner dashboard |
| `/unlink` | Remove the pairing |
| `/setchannel drops` | Add a channel for drop notifications (multi-server) |
| `/setchannel points` | Add a channel for channel points notifications (multi-server) |
| `/dashboard` | Post a live-updating embed with control buttons |
| `/devpanel` | *(Dev-restricted)* Global stats + "Post Live Stats" embed |

**Key features:**
- **Multi-server support** — run `/setchannel` in multiple Discord servers; all configured channels receive notifications simultaneously
- **Drop notifications** — one embed per drop, with reward thumbnail image, game, drop name, reward name, and account name in the footer
- **Channel points notifications** — fires on gains ≥ 25 pts, showing channel, amount, balance, and account name
- **Live dashboard embed** — auto-updates every 30s on state change; shows status, watching channel, points balance, and drop count; owner-only control buttons (Pause/Resume, Switch Mode, View Campaigns, Last Drops, Refresh)
- **Live global stats embed** — `/devpanel → Post Live Stats Here` posts a public embed showing total drops, drops today, channel points, and paired accounts; auto-updates every 30 min and survives bot restarts
- **Web UI channel config** — Settings → Discord Bot shows all configured notification channels with name, server, and individual Remove buttons per channel

**Invite the bot:**
[➕ Add TwitchDropsMiner Bot to your server](https://discord.com/oauth2/authorize?client_id=1513555081218359506&permissions=84992&integration_type=0&scope=bot)

**Setup:**
1. Invite the bot to your server using the link above
2. In the web dashboard, go to **Settings → Discord Bot → Generate code**
3. In Discord, run: `/link https://your-server:8081 DROPS-XXXXXXXX`
4. Run `/setchannel drops` and `/setchannel points` in the channels where you want notifications
5. Run `/dashboard` to post a live-updating stats embed with control buttons

### Discord Webhooks (alternative to the bot)

If you'd rather not add a bot, two separate webhook URLs (drops / channel points) can be configured directly in Settings:
- Drop claimed → embed with game, drop name, reward, item thumbnail image, and account name
- Channel points bonus chest → embed with channel, bonus amount, balance, and account name
- A test button is included to verify webhooks without waiting for a real event
- Account name in the footer makes it easy to distinguish multiple accounts using the same webhook

---

## 🔒 Dashboard Password (Remote Access)

To protect the web UI when exposing it publicly (e.g. running on a VPS), set the `WEB_PASSWORD` environment variable:

```bash
# From source
WEB_PASSWORD=yourpassword uv run main.py
```

```yaml
# Docker Compose
environment:
  - WEB_PASSWORD=yourpassword
```

The dashboard will show a password prompt on first visit. Auth is stored as a 30-day session cookie. Change or remove the password from the **Settings** tab in the web UI at any time.

> **No `WEB_PASSWORD` set?** The dashboard is open to anyone who can reach the port — fine for localhost, not for public servers.

---

## 🌍 Languages

The dashboard is fully translated into 19 languages (selectable from the header), with translation-key coverage actively audited against every string the frontend and backend actually reference — not just "a file exists for it":

Arabic · Chinese (Simplified) · Chinese (Traditional) · Czech · Danish · Dutch · English · French · German · Indonesian · Italian · Japanese · Polish · Portuguese · Romanian · Russian · Spanish · Turkish · Ukrainian

See the [Original Project Credits](#original-project-credits) section for translation credits.

---

## ⚠️ Notes & Warnings

> ⚠️ **Avoid Watching on the Same Account**
> Watching Twitch manually while the miner runs can cause progress desync.
> Use a different account if you want to watch live streams while mining.

> 💡 **Requirements**
> Python 3.12+
> Docker optional but recommended
> Persistent data stored in `/data`

---

## 💬 Contributing

⭐ **Star this repo** if it's useful!
💬 [Open an issue](../../issues) or [submit a PR](../../pulls) for bugs and improvements.

---

## 🎯 Project Goals

| Goal | Description |
|------|--------------|
| 🎯 **Focus** | Twitch Drops automation |
| 🧩 **Ease of Use** | Simple web UI |
| 🛡️ **Reliability** | Designed for continuous operation |
| ⚙️ **Efficiency** | Minimal API calls, Twitch-friendly |
| 🐳 **Deployment** | Docker-first, headless operation |

---

## 🙏 Acknowledgments

This project builds on the work of:
- [@DevilXD](https://github.com/DevilXD) — original creator of [TwitchDropsMiner](https://github.com/DevilXD/TwitchDropsMiner)
- [@rangermix](https://github.com/rangermix) — upstream fork this project branched from

For translation credits, see the [Original Project Credits](#original-project-credits) section below.

---

## 🧾 Disclaimer

> This project is actively developed. It is stable and runs continuously in production,
> but use it at your own risk. Always back up your `data/` directory before updating.

---

## 🧑‍💻 Original Project Credits

<!---
Note: The translations credits are sorted alphabetically, based on their English language name.
When adding a new entry, please ensure to insert it in the correct place in the second section.
Non-translations related credits should be added to the first section instead.

Note: When adding a new credits line below, please add two trailing spaces at the end
of the previous line, if they aren't already there. Doing so ensures proper markdown
rendering on Github. In short: Each credits line should end with two trailing spaces,
placed past the period character at the end.

• Last line can have the two trailing spaces omitted.
• Please ensure your editor won't trim the trailing spaces upon saving the file.
• Please ensure to leave a single empty new line at the end of the file.
-->

@guihkx - For the CI script, CI maintenance, and everything related to Linux builds.  
@kWAYTV - For the implementation of the dark mode theme.  

@Bamboozul - For the entirety of the Arabic (العربية) translation.  
@Suz1e - For the entirety of the Chinese (简体中文) translation and revisions.  
@wwj010 - For the Chinese (简体中文) translation corrections and revisions.  
@zhangminghao1989 - For the Chinese (简体中文) translation corrections and revisions.  
@Ricky103403 - For the entirety of the Traditional Chinese (繁體中文) translation.  
@LusTerCsI - For the Traditional Chinese (繁體中文) translation corrections and revisions.  
@nwvh - For the entirety of the Czech (Čeština) translation.  
@Kjerne - For the entirety of the Danish (Dansk) translation.  
@roobini-gamer - For the entirety of the French (Français) translation.  
@Calvineries - For the French (Français) translation revisions.  
@ThisIsCyreX - For the entirety of the German (Deutsch) translation.  
@Eriza-Z - For the entirety of the Indonesian translation.  
@casungo - For the entirety of the Italian (Italiano) translation.  
@ShimadaNanaki - For the entirety of the Japanese (日本語) translation.  
@Patriot99 - For the Polish (Polski) translation and revisions (co-authored with @DevilXD).  
@zarigata - For the entirety of the Portuguese (Português) translation.  
@Sergo1217 - For the entirety of the Russian (Русский) translation.  
@kilroy98 - For the Russian (Русский) translation corrections and revisions.  
@Shofuu - For the entirety of the Spanish (Español) translation and revisions.  
@alikdb - For the entirety of the Turkish (Türkçe) translation.  
@Nollasko - For the entirety of the Ukrainian (Українська) translation and revisions.  
@kilroy98 - For the Ukrainian (Українська) translation corrections and revisions.  
