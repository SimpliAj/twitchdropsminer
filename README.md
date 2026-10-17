# 🌟 Twitch Drops Miner — SimpliAj Edition

<p align="center">
  <a href="https://github.com/SimpliAj/twitchdropsminer/stargazers"><img src="https://img.shields.io/github/stars/SimpliAj/twitchdropsminer?style=for-the-badge&color=yellow" alt="Stars"></a>
  <a href="https://github.com/SimpliAj/twitchdropsminer/releases"><img src="https://img.shields.io/github/v/release/SimpliAj/twitchdropsminer?style=for-the-badge&color=brightgreen" alt="Release"></a>
  <a href="https://github.com/SimpliAj/twitchdropsminer/blob/main/LICENSE"><img src="https://img.shields.io/github/license/SimpliAj/twitchdropsminer?style=for-the-badge&color=orange" alt="License"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.12+-blue?style=for-the-badge&logo=python" alt="Python"></a>
</p>

**Twitch Drops Miner watches drop campaigns for you** — no video stream, no bandwidth. It sends the same watch heartbeats Twitch's own player sends, finds active campaigns, claims every drop the moment it's earned, and runs headlessly behind a web dashboard you can check from any device.

<p align="center"><img src="docs/images/hero.png" alt="Dashboard on desktop and phone" width="100%"></p>

An independently maintained fork of [rangermix/TwitchDropsMiner](https://github.com/rangermix/TwitchDropsMiner) (itself based on [DevilXD/TwitchDropsMiner](https://github.com/DevilXD/TwitchDropsMiner)) with its own dashboard, multi-account fleet, auto-betting, Discord bot and many reliability fixes.

---

## ✨ Features

- 🚀 **Streamless mining** — earns drops without streaming video
- 🔍 **Automatic campaigns** — finds new campaigns, picks the best live channel, claims drops instantly; optionally falls back to any other game with drops when nothing on your priority list is live
- 🕹️ **Admin-style dashboard** — sidebar navigation on desktop, bottom bar on phones, light & dark mode, 19 languages
- 🔑 **Simple, safe login** — a normal browser window on your own PC; nothing remote-controlled for Twitch to reject
- 👥 **Multi-account** — unlimited isolated accounts, a fleet view with bulk actions and settings
- 💰 **Channel points** — auto-claims bonus chests, idle-watches channels when no drops are running
- 🎯 **Auto-betting** — optional prediction bets with four strategies and per-channel overrides
- 🚫 **Fine-grained filters** — blacklist games, drop names or drop IDs, ignore single campaigns
- 🤖 **Discord** — a bot with live embeds and slash commands, or plain webhooks
- 🐳 **Docker-ready** — multi-arch images (`amd64`, `arm64`)

---

## 🧰 Quick Start

### 🐳 Docker (recommended)

```yaml
# docker-compose.yml
services:
  twitch-drops-miner:
    image: gitsimpliaj/twitch-drops-miner:latest
    container_name: twitch-drops-miner
    init: true
    stop_grace_period: 30s
    shm_size: 256m
    ports:
      - "8080:8080"
    volumes:
      - ./data:/app/data
    environment:
      - TZ=Europe/Vienna           # your real timezone
      - WEB_PASSWORD=yourpassword  # optional, recommended when reachable from outside
    restart: always
```

```bash
docker compose up -d
```

Open **<http://localhost:8080>**. Also on GHCR: `ghcr.io/simpliaj/twitchdropsminer:latest`.

### 🧑‍💻 From source

Python 3.12+:

```bash
git clone https://github.com/SimpliAj/twitchdropsminer.git
cd twitchdropsminer
pip install -e .
playwright install chromium
python main.py
```

On a headless Linux server also install `xvfb` and `procps` (`sudo apt-get install -y xvfb procps`).

---

## 🔑 Logging in

- **TDM runs on your PC** — a normal browser window opens on the Twitch login page by itself. Log in there.
- **TDM runs on a server (VPS, NAS, Docker)** — the Login panel shows a ready-made command with your dashboard's address. Run it on your own PC (needs Python 3 and Chrome or Edge):

  ```bash
  python -c "import urllib.request as u;exec(u.urlopen('https://your-tdm.example/login-helper.py').read())" https://your-tdm.example
  ```

  A browser window opens, you log in, and the session is sent to TDM. If the dashboard has a password, the command asks for it.

The session is saved, so you only log in again when Twitch ends it.

---

## 📸 Screenshots

<p align="center"><img src="docs/images/inventory.png" alt="Inventory" width="100%"></p>

| Analytics | Settings |
|---|---|
| <img src="docs/images/analytics.png" alt="Analytics"> | <img src="docs/images/settings.png" alt="Settings"> |

<p align="center"><img src="docs/images/mobile.png" alt="Phone layout" width="70%"></p>

---

## 👥 Multi-Account

Each account has its own data folder (`data/accounts/<name>/`). Add, switch and rename accounts in **Settings → System**; **Manage Accounts** in the sidebar shows the whole fleet with bulk actions and shared settings. For fully separate processes per account (one port each, e.g. `/acc2/` behind Nginx), see the [Multi-Account wiki page](../../wiki/Multi-Account).

## 🤖 Discord

[➕ Add the bot](https://discord.com/oauth2/authorize?client_id=1513555081218359506&permissions=84992&integration_type=0&scope=bot), then **Settings → Discord Bot → Generate code** and run `/link <dashboard-url> <code>` in Discord. `/setchannel drops`, `/setchannel points` and `/dashboard` set up notifications and a live embed. Prefer no bot? Add webhook URLs in Settings instead. Details: [Discord Bot](../../wiki/Discord-Bot), [Discord Webhook](../../wiki/Discord-Webhook).

## 🔒 Remote access

Set `WEB_PASSWORD` (or a password in **Settings → Login & Password**) before the dashboard is reachable from outside, and prefer https. Sessions last 30 days. See [Remote Access](../../wiki/Remote-Access).

## 🌍 Languages

Arabic · Chinese (Simplified / Traditional) · Czech · Danish · Dutch · English · French · German · Indonesian · Italian · Japanese · Polish · Portuguese · Romanian · Russian · Spanish · Turkish · Ukrainian

> ⚠️ Don't watch Twitch on the same account while the miner runs — it can desync drop progress.

📖 **Everything else** (priority queue, idle watch, auto-betting, FAQ) is in the [Wiki](../../wiki).

---

## 💬 Contributing

⭐ Star the repo if it's useful. [Open an issue](../../issues) or [a PR](../../pulls) for bugs and improvements.

## 🙏 Acknowledgments

- [@DevilXD](https://github.com/DevilXD) — original creator of [TwitchDropsMiner](https://github.com/DevilXD/TwitchDropsMiner)
- [@rangermix](https://github.com/rangermix) — upstream fork this project branched from
- [@PatrikDrex](https://github.com/PatrikDrex) — Czech translation overhaul and the translatable-UI groundwork ([#24](../../issues/24))

## 🧾 Disclaimer

> Use at your own risk. Back up your `data/` directory before updating.

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
