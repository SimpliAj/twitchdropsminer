import os
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("twitchdrops_bot")

BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN env var not set")

PAIRINGS_PATH = Path(__file__).parent / "pairings.json"
LIVE_STATS_PATH = Path(__file__).parent / "live_stats.json"

COLOR_TWITCH = 0x9147FF
COLOR_SUCCESS = 0x00B04F
COLOR_ERROR = 0xEB4A4A
COLOR_PAUSED = 0xFF9900


def _migrate_users_data(data: dict) -> dict:
    """Migrate old format {uid: pairing} to new format {uid: {name: pairing}}."""
    for uid, val in list(data.items()):
        if isinstance(val, dict) and "url" in val:
            data[uid] = {"default": val}
    return data


def load_pairings() -> dict:
    if PAIRINGS_PATH.exists():
        with open(PAIRINGS_PATH) as f:
            data = json.load(f)
        users = data.get("users", {})
        return _migrate_users_data(users)
    return {}


def save_pairings(users: dict) -> None:
    # Preserve fields written externally (e.g. profile_name set by web app)
    if PAIRINGS_PATH.exists():
        try:
            existing = json.loads(PAIRINGS_PATH.read_text()).get("users", {})
            # existing may be old or new format — migrate first
            existing = _migrate_users_data(existing)
            for uid, pairings_dict in users.items():
                if uid in existing:
                    for name, pairing in pairings_dict.items():
                        existing_pairing = existing[uid].get(name, {})
                        if existing_pairing.get("profile_name"):
                            pairing.setdefault("profile_name", existing_pairing["profile_name"])
        except Exception:
            pass
    with open(PAIRINGS_PATH, "w") as f:
        json.dump({"users": users}, f, indent=2)


def load_live_stats() -> dict[int, int]:
    if LIVE_STATS_PATH.exists():
        with open(LIVE_STATS_PATH) as f:
            data = json.load(f)
        return {int(k): int(v) for k, v in data.items()}
    return {}


def save_live_stats(refs: dict[int, int]) -> None:
    with open(LIVE_STATS_PATH, "w") as f:
        json.dump({str(k): v for k, v in refs.items()}, f, indent=2)


def get_user_pairing(users: dict, user_id: int, name: str = "default") -> dict | None:
    return users.get(str(user_id), {}).get(name)


def _channel_ids(pairing: dict, type: str) -> list[int]:
    """Return list of channel IDs (handles int, dict with 'id', or list of either)."""
    val = pairing.get("channels", {}).get(type)
    if val is None:
        return []
    if not isinstance(val, list):
        val = [val]
    ids = []
    for v in val:
        if v is None:
            continue
        if isinstance(v, dict):
            ids.append(int(v["id"]))
        else:
            ids.append(int(v))
    return ids


def _channel_entries(pairing: dict, type: str) -> list[dict]:
    """Return list of {id, name, guild} dicts for a channel type."""
    val = pairing.get("channels", {}).get(type)
    if val is None:
        return []
    if not isinstance(val, list):
        val = [val]
    entries = []
    for v in val:
        if v is None:
            continue
        if isinstance(v, dict):
            entries.append({"id": str(v["id"]), "name": v.get("name", ""), "guild": v.get("guild", "")})
        else:
            entries.append({"id": str(v), "name": "", "guild": ""})
    return entries


def get_footer_text(pairing: dict | None, suffix: str = "") -> str:
    name = (pairing or {}).get("profile_name") or "TwitchDropsMiner Bot"
    return f"{name}{suffix}"


def make_footer(embed: discord.Embed, pairing: dict | None = None, suffix: str = "") -> None:
    embed.set_footer(text=get_footer_text(pairing, suffix))
    embed.timestamp = datetime.now(timezone.utc)


def error_embed(description: str) -> discord.Embed:
    e = discord.Embed(description=description, color=COLOR_ERROR)
    make_footer(e)
    return e


def success_embed(description: str) -> discord.Embed:
    e = discord.Embed(description=description, color=COLOR_SUCCESS)
    make_footer(e)
    return e


async def api_get(session: aiohttp.ClientSession, url: str, token: str, path: str):
    async with session.get(
        f"{url.rstrip('/')}{path}",
        headers={"X-Bot-Token": token},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        resp.raise_for_status()
        return await resp.json()


async def api_post(session: aiohttp.ClientSession, url: str, token: str, path: str, body: dict | None = None):
    async with session.post(
        f"{url.rstrip('/')}{path}",
        headers={"X-Bot-Token": token},
        json=body or {},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        resp.raise_for_status()
        return await resp.json()


async def build_dashboard_embed(session: aiohttp.ClientSession, url: str, token: str, pairing: dict | None = None) -> discord.Embed:
    try:
        status_data = await api_get(session, url, token, "/api/status")
    except Exception:
        e = discord.Embed(title="TwitchDropsMiner — Dashboard", description="❌ Dashboard unreachable", color=COLOR_ERROR)
        make_footer(e, pairing, " • Updates every 30s")
        return e

    paused = status_data.get("paused", False)
    status_str = status_data.get("status", "")
    login_info = status_data.get("login", {})
    account = login_info.get("user_login") if isinstance(login_info, dict) else str(login_info)

    watching_login = None
    if "watching:" in status_str.lower():
        watching_login = status_str.split(":")[-1].strip()

    if paused:
        color, state_line = COLOR_PAUSED, "⏸️  **Paused**"
    elif "idle" in status_str.lower():
        color, state_line = 0x5865F2, "💤  **Idle Watching**"
    elif status_str:
        color, state_line = COLOR_SUCCESS, "🟢  **Mining Drops**"
    else:
        color, state_line = COLOR_ERROR, "🔴  **Unknown**"

    # Watching + channel points → second line of description
    watch_line = None
    try:
        idle_data = await api_get(session, url, token, "/api/idle-watch/status")
        w_login = idle_data.get("watching")
        w_name = idle_data.get("display_name") or w_login
        online = idle_data.get("online", False)
        if w_name:
            watching_login = w_login or watching_login
            watch_line = f"📺 **{w_name}**  {'🟢 Live' if online else '⚫ Offline'}"
            if watching_login:
                try:
                    cp_data = await api_get(session, url, token, f"/api/channel-points/{watching_login}")
                    balance = cp_data.get("balance")
                    if balance is not None:
                        watch_line += f"  ·  💰 **{balance:,}** pts"
                except Exception:
                    pass
    except Exception:
        if watching_login:
            watch_line = f"📺 **{watching_login}**"

    desc_parts = [f"{state_line}  ·  👤 `{account}`" if account else state_line]
    if watch_line:
        desc_parts.append(watch_line)

    embed = discord.Embed(
        title="TwitchDropsMiner",
        description="\n".join(desc_parts),
        color=color,
    )

    # Thumbnail: next drop item image (first unclaimed benefit image from active campaign)
    try:
        camp_data = await api_get(session, url, token, "/api/campaigns")
        campaigns = camp_data.get("campaigns", []) if isinstance(camp_data, dict) else camp_data
        next_thumb = None
        for c in campaigns:
            if c.get("expired"):
                continue
            drops = c.get("drops", [])
            # prefer in-progress drop, then upcoming
            for d in drops:
                if not d.get("is_claimed"):
                    benefits = d.get("benefits", [])
                    if benefits and benefits[0].get("image_url"):
                        next_thumb = benefits[0]["image_url"]
                        break
            if next_thumb:
                break
        if next_thumb:
            embed.set_thumbnail(url=next_thumb)
    except Exception:
        pass

    # Total drops + last drop
    try:
        history = await api_get(session, url, token, "/api/drops-history")
        if isinstance(history, list):
            embed.add_field(name="📈 Drops", value=f"**{len(history)}** total", inline=True)
            if history:
                last = history[0]  # newest-first
                reward = last.get("reward") or last.get("drop") or "?"
                game = last.get("game", "")
                ts = last.get("timestamp", "")[5:10] if last.get("timestamp") else ""  # MM-DD only
                embed.add_field(
                    name="🏆 Last Drop",
                    value=f"**{reward}**\n{game}{' · ' + ts if ts else ''}",
                    inline=True,
                )
    except Exception:
        pass

    # Wanted Queue — compact single line
    try:
        wanted_data = await api_get(session, url, token, "/api/wanted-items")
        wanted_games = wanted_data.get("wanted_items", []) if isinstance(wanted_data, dict) else []
        if wanted_games:
            names = [g.get("game_name", "?") for g in wanted_games[:6]]
            suffix = f" +{len(wanted_games) - 6}" if len(wanted_games) > 6 else ""
            embed.add_field(name="🎯 Wanted", value="  ·  ".join(names) + suffix, inline=False)
        else:
            embed.add_field(name="🎯 Wanted", value="Nothing configured", inline=False)
    except Exception:
        pass

    # Prediction stats
    try:
        from collections import Counter as _Counter
        pred_data = await api_get(session, url, token, "/api/predictions")
        preds = pred_data.get("predictions", []) if isinstance(pred_data, dict) else []
        resolved = [p for p in preds if p.get("result") in ("WIN", "LOSE")]
        if resolved:
            wins = [p for p in resolved if p["result"] == "WIN"]
            losses = [p for p in resolved if p["result"] == "LOSE"]
            total_bet = sum(p.get("points_bet", 0) for p in resolved)
            total_won = sum(p.get("points_won", 0) for p in wins)
            total_lost = sum(p.get("points_bet", 0) for p in losses)
            net = total_won - total_lost
            win_rate = int(len(wins) / len(resolved) * 100) if resolved else 0
            net_str = f"+{net:,}" if net >= 0 else f"{net:,}"
            strategy_counts = _Counter(p.get("strategy", "") for p in resolved if p.get("strategy"))
            top_strategy = strategy_counts.most_common(1)[0][0] if strategy_counts else None
            pred_val = f"✅ {len(wins)}W / ❌ {len(losses)}L  ·  {win_rate}%\nNet: **{net_str} pts**  ·  Bet: {total_bet:,}"
            if top_strategy:
                pred_val += f"\nStrategy: **{top_strategy}**"
            embed.add_field(name="🎲 Predictions", value=pred_val, inline=False)
    except Exception:
        pass

    # Notification channels
    if pairing:
        notif_lines = []
        for type_key, emoji, label in [("drops", "🎁", "Drops"), ("points", "💰", "Points"), ("bets", "🎲", "Bets"), ("campaigns", "⏰", "Campaigns")]:
            entries = _channel_entries(pairing, type_key)
            if entries:
                ch_strs = [f"<#{e['id']}>" + (f" *({e['guild']})*" if e.get("guild") else "") for e in entries]
                notif_lines.append(f"{emoji} {label}: {', '.join(ch_strs)}")
        if notif_lines:
            embed.add_field(name="📢 Notifications", value="\n".join(notif_lines), inline=False)

    make_footer(embed, pairing, " • Auto-updates on change")
    return embed


class DashboardView(discord.ui.View):
    def __init__(self, owner_id: int, bot: "TwitchDropsBot", pairing_name: str = "default"):
        super().__init__(timeout=None)
        self.owner_id = owner_id
        self.bot = bot
        self.pairing_name = pairing_name

    def _pairing(self) -> dict | None:
        return self.bot.users_data.get(str(self.owner_id), {}).get(self.pairing_name)

    async def _owner_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ Only the person who linked this dashboard can use these buttons.",
                ephemeral=True,
            )
            return False
        return True

    async def _update_embed(self, interaction: discord.Interaction):
        pairing = self._pairing()
        if not pairing:
            return
        async with aiohttp.ClientSession() as session:
            new_embed = await build_dashboard_embed(session, pairing["url"], pairing["token"], pairing)
        await interaction.message.edit(embed=new_embed, view=DashboardView(self.owner_id, self.bot, self.pairing_name))

    @discord.ui.button(label="⏸️ Pause/Resume", style=discord.ButtonStyle.primary, row=0)
    async def toggle_pause(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._owner_check(interaction):
            return
        pairing = self._pairing()
        if not pairing:
            await interaction.response.send_message("❌ No dashboard linked.", ephemeral=True)
            return
        await interaction.response.defer_update()
        try:
            async with aiohttp.ClientSession() as session:
                status = await api_get(session, pairing["url"], pairing["token"], "/api/status")
                paused = status.get("paused", False)
                await api_post(session, pairing["url"], pairing["token"], "/api/resume" if paused else "/api/pause")
                new_embed = await build_dashboard_embed(session, pairing["url"], pairing["token"])
            await interaction.message.edit(embed=new_embed, view=DashboardView(self.owner_id, self.bot, self.pairing_name))
        except Exception as e:
            log.debug("Pause toggle error: %s", e)

    @discord.ui.button(label="🎮 Switch Mode", style=discord.ButtonStyle.secondary, row=0)
    async def switch_mode(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._owner_check(interaction):
            return
        pairing = self._pairing()
        if not pairing:
            await interaction.response.send_message("❌ No dashboard linked.", ephemeral=True)
            return
        await interaction.response.defer_update()
        try:
            async with aiohttp.ClientSession() as session:
                await api_post(session, pairing["url"], pairing["token"], "/api/idle-watch/switch", {})
                new_embed = await build_dashboard_embed(session, pairing["url"], pairing["token"])
            await interaction.message.edit(embed=new_embed, view=DashboardView(self.owner_id, self.bot, self.pairing_name))
        except Exception as e:
            log.debug("Switch mode error: %s", e)

    @discord.ui.button(label="📋 Campaigns", style=discord.ButtonStyle.secondary, row=0)
    async def show_campaigns(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._owner_check(interaction):
            return
        pairing = self._pairing()
        if not pairing:
            await interaction.response.send_message("❌ No dashboard linked.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            async with aiohttp.ClientSession() as session:
                data = await api_get(session, pairing["url"], pairing["token"], "/api/campaigns")
            campaigns = data.get("campaigns", []) if isinstance(data, dict) else data
            embed = discord.Embed(title="🎮 Active Campaigns", color=COLOR_TWITCH)
            if campaigns:
                lines = []
                for camp in campaigns[:10]:
                    name = camp.get("name") or "Unknown"
                    drops = camp.get("drops", [])
                    claimed = sum(1 for d in drops if d.get("is_claimed"))
                    total = len(drops)
                    game = camp.get("game_name", "")
                    bar = "█" * int(claimed / total * 8) + "░" * (8 - int(claimed / total * 8)) if total else "░" * 8
                    lines.append(f"**{name}**\n{game + ' · ' if game else ''}`{bar}` {claimed}/{total} drops")
                embed.description = "\n\n".join(lines)
            else:
                embed.description = "No active campaigns."
            make_footer(embed)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(embed=error_embed(f"❌ Error: {e}"), ephemeral=True)

    @discord.ui.button(label="🏆 Last Drops", style=discord.ButtonStyle.secondary, row=1)
    async def show_drops(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._owner_check(interaction):
            return
        pairing = self._pairing()
        if not pairing:
            await interaction.response.send_message("❌ No dashboard linked.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            async with aiohttp.ClientSession() as session:
                history = await api_get(session, pairing["url"], pairing["token"], "/api/drops-history")
            embed = discord.Embed(title="🎁 Recent Drops", color=COLOR_TWITCH)
            if history:
                lines = []
                for drop in history[:10]:  # newest-first
                    game = drop.get("game", "?")
                    reward = drop.get("reward") or drop.get("drop") or "?"
                    ts = drop.get("timestamp", "")[:10] if drop.get("timestamp") else ""
                    lines.append(f"**{reward}**\n{game}{' · ' + ts if ts else ''}")
                embed.description = "\n\n".join(lines)
            else:
                embed.description = "No drops claimed yet."
            make_footer(embed)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(embed=error_embed(f"❌ Error: {e}"), ephemeral=True)

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary, row=1)
    async def do_refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._owner_check(interaction):
            return
        pairing = self._pairing()
        if not pairing:
            await interaction.response.send_message("❌ No dashboard linked.", ephemeral=True)
            return
        await interaction.response.defer_update()
        try:
            async with aiohttp.ClientSession() as session:
                new_embed = await build_dashboard_embed(session, pairing["url"], pairing["token"])
            await interaction.message.edit(embed=new_embed, view=DashboardView(self.owner_id, self.bot, self.pairing_name))
        except Exception as e:
            log.debug("Refresh error: %s", e)


class TwitchDropsBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.users_data: dict = {}
        self._live_stats_messages: dict[int, int] = load_live_stats()
        self._live_stats_last_update: float = 0.0

    async def _build_global_stats(self, session: aiohttp.ClientSession, owner_uid: str | None = None) -> discord.Embed:
        GITHUB_URL = "https://github.com/SimpliAj/twitchdropsminer"
        total_drops = 0
        today_drops = 0
        from zoneinfo import ZoneInfo
        from collections import Counter
        today_str = datetime.now(ZoneInfo("Europe/Vienna")).strftime("%Y-%m-%d")
        cp_today_total = 0
        total_cp = 0
        pred_wins = 0
        pred_losses = 0
        pred_net = 0
        pred_total_bet = 0
        strategy_counter: Counter = Counter()

        # Only iterate the requesting user's pairings (or all if no owner specified)
        source = {owner_uid: self.users_data[owner_uid]} if owner_uid and owner_uid in self.users_data else self.users_data
        for uid, pairings_dict in source.items():
            for name, pairing in pairings_dict.items():
                try:
                    history = await api_get(session, pairing["url"], pairing["token"], "/api/drops-history")
                    if isinstance(history, list):
                        total_drops += len(history)
                        today_drops += sum(1 for d in history if d.get("timestamp", "").startswith(today_str))
                except Exception:
                    pass
                total_cp += pairing.get("cp_total_earned", 0)
                if pairing.get("cp_today_date") == today_str:
                    cp_today_total += pairing.get("cp_today", 0)
                try:
                    pred_data = await api_get(session, pairing["url"], pairing["token"], "/api/predictions")
                    preds = pred_data.get("predictions", []) if isinstance(pred_data, dict) else []
                    for p in preds:
                        strategy = p.get("strategy", "")
                        if strategy:
                            strategy_counter[strategy] += 1
                        if p.get("result") == "WIN":
                            pred_wins += 1
                            pred_net += p.get("points_won", 0) - p.get("points_bet", 0)
                            pred_total_bet += p.get("points_bet", 0)
                        elif p.get("result") == "LOSE":
                            pred_losses += 1
                            pred_net -= p.get("points_bet", 0)
                            pred_total_bet += p.get("points_bet", 0)
                except Exception:
                    pass

        desc = (
            f"🎁  **{total_drops}** drops total  ·  **{today_drops}** today\n"
            f"💰  **{total_cp:,}** pts total  ·  **{cp_today_total:,}** today\n\n"
            f"[View on GitHub]({GITHUB_URL})"
        )
        embed = discord.Embed(
            title="📊 TwitchDropsMiner — Live Stats",
            url=GITHUB_URL,
            description=desc,
            color=COLOR_TWITCH,
        )
        if pred_wins + pred_losses > 0:
            total_resolved = pred_wins + pred_losses
            win_rate = int(pred_wins / total_resolved * 100)
            net_str = f"+{pred_net:,}" if pred_net >= 0 else f"{pred_net:,}"
            top_strategy = strategy_counter.most_common(1)[0][0] if strategy_counter else None
            pred_val = f"✅ {pred_wins}W / ❌ {pred_losses}L  ·  {win_rate}%\nNet: **{net_str} pts**  ·  Bet: {pred_total_bet:,}"
            if top_strategy:
                pred_val += f"\nStrategy: **{top_strategy}**"
            embed.add_field(name="🎲 Predictions", value=pred_val, inline=False)
        embed.set_footer(text="Auto-updates every 30 min")
        embed.timestamp = datetime.now(timezone.utc)
        return embed

    async def setup_hook(self):
        self.users_data = load_pairings()
        if self.users_data:
            log.info("Loaded pairings for users: %s", list(self.users_data.keys()))
        else:
            log.info("No pairings loaded (fresh start)")

        register_commands(self)
        await self.tree.sync()
        log.info("Slash commands synced globally")
        self.poll_task.start()

    async def on_ready(self):
        log.info("Logged in as %s (ID: %s)", self.user, self.user.id)

    @tasks.loop(seconds=30)
    async def poll_task(self):
        if not self.users_data:
            return
        # Reload from disk to pick up external changes (e.g. profile_name set by web app)
        fresh = load_pairings()
        for uid, fresh_pairings in fresh.items():
            if uid in self.users_data:
                for name, fresh_pairing in fresh_pairings.items():
                    if name in self.users_data[uid]:
                        profile_name = fresh_pairing.get("profile_name") or ""
                        self.users_data[uid][name]["profile_name"] = profile_name

        async with aiohttp.ClientSession() as session:
            for user_id, pairings_dict in list(self.users_data.items()):
                for pairing_name, pairing in list(pairings_dict.items()):
                    try:
                        drop_count = pairing.get("last_drop_count", 0)
                        watching_channel = None
                        cp_balance = None
                        paused = False

                        # Check for new drops. Tracked by timestamp, not list length:
                        # /api/drops-history is capped at 500 entries (oldest evicted
                        # first), so once an account's lifetime total passes 500,
                        # len(history) gets stuck at 500 forever and a length-based
                        # diff never detects a new drop again — confirmed live, this
                        # is exactly why drop notifications silently stopped once an
                        # account crossed 500 total claimed drops.
                        history = await api_get(session, pairing["url"], pairing["token"], "/api/drops-history")
                        if isinstance(history, list):
                            drop_count = len(history)
                            last_ts = pairing.get("last_drop_timestamp")
                            if last_ts is None:
                                # First poll after upgrading from count-based tracking
                                # (or a brand new pairing) — start tracking from here
                                # instead of retroactively announcing all of history.
                                new_drops = []
                            else:
                                new_drops = [d for d in history if d.get("timestamp", "") > last_ts]
                            if new_drops:
                                for drops_ch_id in _channel_ids(pairing, "drops"):
                                    ch = self.get_channel(drops_ch_id)
                                    if ch is None:
                                        try:
                                            ch = await self.fetch_channel(drops_ch_id)
                                        except Exception:
                                            ch = None
                                    if ch:
                                        for drop in new_drops[-10:]:
                                            game = drop.get("game", "Unknown")
                                            drop_name = drop.get("drop", "")
                                            reward = drop.get("reward") or drop_name or "Unknown"
                                            image_url = drop.get("image_url")
                                            embed = discord.Embed(title="🎁 Drop Claimed!", color=COLOR_TWITCH)
                                            embed.add_field(name="Game", value=game, inline=False)
                                            if drop_name and drop_name != reward:
                                                embed.add_field(name="Drop", value=drop_name, inline=False)
                                            embed.add_field(name="Reward", value=f"**{reward}**", inline=False)
                                            if image_url:
                                                embed.set_thumbnail(url=image_url)
                                            footer = get_footer_text(pairing)
                                            embed.set_footer(text=footer)
                                            embed.timestamp = datetime.now(timezone.utc)
                                            await ch.send(embed=embed)
                                        log.info("Drops notification sent to %s (%d new drops)", drops_ch_id, len(new_drops))
                                    else:
                                        log.warning("Drops channel %s not found for user %s/%s", drops_ch_id, user_id, pairing_name)
                            if history:
                                self.users_data[user_id][pairing_name]["last_drop_timestamp"] = history[0].get("timestamp", last_ts)
                            self.users_data[user_id][pairing_name]["last_drop_count"] = drop_count
                            if new_drops or last_ts is None:
                                save_pairings(self.users_data)

                        # Channel points tracking
                        try:
                            idle = await api_get(session, pairing["url"], pairing["token"], "/api/idle-watch/status")
                            watching_channel = idle.get("watching")
                            if watching_channel:
                                cp_data = await api_get(session, pairing["url"], pairing["token"], f"/api/channel-points/{watching_channel}")
                                cp_balance = cp_data.get("balance", 0)
                                last_cp = pairing.get("last_cp", {})
                                prev_balance = last_cp.get(watching_channel)

                                # last_notified_cp: balance at time of last notification (not last poll)
                                last_notified = pairing.get("last_notified_cp", {}).get(watching_channel, prev_balance)

                                if prev_balance is not None and cp_balance > prev_balance:
                                    gained_this_poll = cp_balance - prev_balance
                                    log.info("CP gain for %s/%s on %s: +%d (prev=%d now=%d)", user_id, pairing_name, watching_channel, gained_this_poll, prev_balance, cp_balance)

                                    # Check if a chest was claimed since last notification
                                    last_chest = cp_data.get("last_chest", {})
                                    last_chest_ts = last_chest.get("ts", "")
                                    chest_bonus = last_chest.get("bonus", 0)
                                    last_seen_chest_ts = pairing.get("last_cp_meta", {}).get(watching_channel, {}).get("chest_ts", "")
                                    chest_new = (chest_bonus > 0 and last_chest_ts and last_chest_ts != last_seen_chest_ts)

                                    if chest_new:
                                        # Total since last notification = chest + all watch points since then
                                        total_since_notify = cp_balance - (last_notified or cp_balance - gained_this_poll)
                                        watch_pts = max(0, total_since_notify - chest_bonus)
                                        if watch_pts > 0:
                                            desc = (
                                                f"🎁 **Bonus Chest: +{chest_bonus:,} pts** on **{watching_channel}**\n"
                                                f"📺 From watching: +{watch_pts:,} pts\n"
                                                f"Balance: **{cp_balance:,} pts**"
                                            )
                                        else:
                                            desc = (
                                                f"🎁 **Bonus Chest: +{chest_bonus:,} pts** on **{watching_channel}**\n"
                                                f"Balance: **{cp_balance:,} pts**"
                                            )
                                        notify = True
                                    elif gained_this_poll >= 25:
                                        desc = (
                                            f"📺 **+{gained_this_poll:,} pts** from watching **{watching_channel}**\n"
                                            f"Balance: **{cp_balance:,} pts**"
                                        )
                                        notify = True
                                    else:
                                        notify = False

                                    if notify:
                                        for pts_ch_id in _channel_ids(pairing, "points"):
                                            pts_ch = self.get_channel(pts_ch_id)
                                            if pts_ch is None:
                                                try:
                                                    pts_ch = await self.fetch_channel(pts_ch_id)
                                                except Exception as fe:
                                                    log.warning("Could not fetch points channel %s: %s", pts_ch_id, fe)
                                                    pts_ch = None
                                            if pts_ch:
                                                embed = discord.Embed(
                                                    title="💰 Channel Points",
                                                    description=desc,
                                                    color=COLOR_TWITCH,
                                                )
                                                make_footer(embed, pairing)
                                                await pts_ch.send(embed=embed)
                                                log.info("CP notification sent to channel %s", pts_ch_id)
                                            else:
                                                log.warning("Points channel %s not found for user %s/%s", pts_ch_id, user_id, pairing_name)

                                        # Update last_notified_cp so next chest diff is calculated correctly
                                        self.users_data[user_id][pairing_name].setdefault("last_notified_cp", {})[watching_channel] = cp_balance
                                        if chest_new:
                                            self.users_data[user_id][pairing_name].setdefault("last_cp_meta", {}).setdefault(watching_channel, {})["chest_ts"] = last_chest_ts
                                        save_pairings(self.users_data)

                                # Track today's earned CP (reset on date change)
                                from zoneinfo import ZoneInfo as _ZI
                                today_str_cp = datetime.now(_ZI("Europe/Vienna")).strftime("%Y-%m-%d")
                                if pairing.get("cp_today_date") != today_str_cp:
                                    self.users_data[user_id][pairing_name]["cp_today"] = 0
                                    self.users_data[user_id][pairing_name]["cp_today_date"] = today_str_cp
                                if prev_balance is not None and cp_balance > prev_balance:
                                    delta = cp_balance - prev_balance
                                    new_cp_today = pairing.get("cp_today", 0) + delta
                                    self.users_data[user_id][pairing_name]["cp_today"] = new_cp_today
                                    new_cp_total = pairing.get("cp_total_earned", 0) + delta
                                    self.users_data[user_id][pairing_name]["cp_total_earned"] = new_cp_total
                                    try:
                                        await api_post(session, pairing["url"], pairing["token"], "/api/daily-points", {"total": new_cp_today})
                                    except Exception:
                                        pass

                                self.users_data[user_id][pairing_name].setdefault("last_cp", {})[watching_channel] = cp_balance
                                save_pairings(self.users_data)
                        except Exception as e:
                            log.debug("CP tracking error for %s/%s: %s", user_id, pairing_name, e)

                        # Poll prediction results → send to "bets" channel
                        try:
                            pred_data = await api_get(session, pairing["url"], pairing["token"], "/api/predictions")
                            preds = pred_data.get("predictions", []) if isinstance(pred_data, dict) else []
                            seen_ids: set = set(pairing.get("seen_pred_ids", []))
                            new_seen: list[str] = []
                            for pred in preds:
                                eid = pred.get("event_id", "")
                                if not eid or eid in seen_ids:
                                    continue
                                result = pred.get("result", "PENDING")
                                if result not in ("WIN", "LOSE"):
                                    continue
                                new_seen.append(eid)
                                for bets_ch_id in _channel_ids(pairing, "bets"):
                                    bets_ch = self.get_channel(bets_ch_id)
                                    if bets_ch is None:
                                        try:
                                            bets_ch = await self.fetch_channel(bets_ch_id)
                                        except Exception:
                                            bets_ch = None
                                    if bets_ch:
                                        color = 0x00b368 if result == "WIN" else 0xeb4a4a
                                        pts_won = pred.get("points_won", 0)
                                        pts_bet = pred.get("points_bet", 0)
                                        embed = discord.Embed(
                                            title=f"{'✅ Win' if result == 'WIN' else '❌ Lose'} — Prediction",
                                            color=color,
                                        )
                                        embed.add_field(name="Channel", value=pred.get("channel", "?"), inline=True)
                                        embed.add_field(name="Prediction", value=pred.get("title", "?")[:100], inline=False)
                                        profit = pts_won - pts_bet if result == "WIN" else -pts_bet
                                        embed.add_field(name="Bet", value=f"{pts_bet:,} pts on {pred.get('outcome_chosen','?')}", inline=True)
                                        embed.add_field(name="Profit", value=f"+{profit:,} pts" if result == "WIN" else f"−{pts_bet:,} pts", inline=True)
                                        make_footer(embed, pairing)
                                        await bets_ch.send(embed=embed)
                            if new_seen:
                                all_seen = list(seen_ids | set(new_seen))
                                self.users_data[user_id][pairing_name]["seen_pred_ids"] = all_seen[-500:]
                                save_pairings(self.users_data)
                        except Exception as e:
                            log.debug("Prediction poll error for %s/%s: %s", user_id, pairing_name, e)

                        # Poll campaigns ending soon → send to "campaigns" channel
                        try:
                            camp_data = await api_get(session, pairing["url"], pairing["token"], "/api/campaigns")
                            campaigns_list = camp_data.get("campaigns", []) if isinstance(camp_data, dict) else []
                            # Only alert for user's wanted games
                            try:
                                wanted_resp = await api_get(session, pairing["url"], pairing["token"], "/api/wanted-items")
                                wanted_names: set[str] = {
                                    g.get("game_name", "").lower()
                                    for g in (wanted_resp.get("wanted_items", []) if isinstance(wanted_resp, dict) else [])
                                }
                            except Exception:
                                wanted_names = set()
                            alerted_camps: set = set(pairing.get("alerted_campaign_ids", []))
                            new_alerted: list[str] = []
                            now_ts = datetime.now(timezone.utc)
                            for camp in campaigns_list:
                                cid = camp.get("id", "")
                                if not cid or cid in alerted_camps or camp.get("expired"):
                                    continue
                                # Skip campaigns for games not in the user's wanted list
                                if wanted_names:
                                    camp_game = (camp.get("game_name") or camp.get("game", "")).lower()
                                    if camp_game not in wanted_names:
                                        continue
                                ends_at_str = camp.get("ends_at", "")
                                if not ends_at_str:
                                    continue
                                try:
                                    ends_at = datetime.fromisoformat(ends_at_str.replace("Z", "+00:00"))
                                    hours_left = (ends_at - now_ts).total_seconds() / 3600
                                    if hours_left > 24 or hours_left < 0:
                                        continue
                                except Exception:
                                    continue
                                remaining_drops = sum(1 for d in camp.get("drops", []) if not d.get("is_claimed"))
                                if remaining_drops == 0:
                                    continue
                                new_alerted.append(cid)
                                for camp_ch_id in _channel_ids(pairing, "campaigns"):
                                    camp_ch = self.get_channel(camp_ch_id)
                                    if camp_ch is None:
                                        try:
                                            camp_ch = await self.fetch_channel(camp_ch_id)
                                        except Exception:
                                            camp_ch = None
                                    if camp_ch:
                                        embed = discord.Embed(
                                            title="⏰ Campaign Ending Soon",
                                            description=f"**{camp.get('name', '?')}** ends in ~{int(hours_left)}h",
                                            color=0xFF4500,
                                        )
                                        embed.add_field(name="Game", value=camp.get("game_name") or camp.get("game", "?"), inline=True)
                                        embed.add_field(name="Unclaimed Drops", value=str(remaining_drops), inline=True)
                                        make_footer(embed, pairing)
                                        await camp_ch.send(embed=embed)
                            if new_alerted:
                                all_alerted = list(alerted_camps | set(new_alerted))
                                self.users_data[user_id][pairing_name]["alerted_campaign_ids"] = all_alerted[-200:]
                                save_pairings(self.users_data)
                        except Exception as e:
                            log.debug("Campaign alert poll error for %s/%s: %s", user_id, pairing_name, e)

                        # Get paused state for state key
                        try:
                            status_data = await api_get(session, pairing["url"], pairing["token"], "/api/status")
                            paused = status_data.get("paused", False)
                        except Exception:
                            pass

                        # Update live dashboard embed only when state changed
                        dashboard = pairing.get("dashboard_embed")
                        if dashboard:
                            pred_count = len(pairing.get("seen_pred_ids", []))
                            state_key = f"{paused}|{watching_channel}|{cp_balance}|{drop_count}|{pred_count}"
                            last_state = pairing.get("last_embed_state", "")
                            if state_key != last_state:
                                ch = self.get_channel(int(dashboard["channel_id"]))
                                if ch:
                                    try:
                                        msg = await ch.fetch_message(int(dashboard["message_id"]))
                                        new_embed = await build_dashboard_embed(session, pairing["url"], pairing["token"], pairing)
                                        view = DashboardView(int(user_id), self, pairing_name)
                                        await msg.edit(embed=new_embed, view=view)
                                        self.users_data[user_id][pairing_name]["last_embed_state"] = state_key
                                        save_pairings(self.users_data)
                                    except discord.NotFound:
                                        self.users_data[user_id][pairing_name].pop("dashboard_embed", None)
                                        save_pairings(self.users_data)
                                    except Exception as e:
                                        log.debug("Dashboard embed update error: %s", e)

                    except Exception as e:
                        log.debug("Poll error for user %s/%s: %s", user_id, pairing_name, e)

            # Update live public stats embeds every 30 minutes
            import time as _time
            now = _time.monotonic()
            if self._live_stats_messages and (now - self._live_stats_last_update) >= 1800:
                async with aiohttp.ClientSession() as s2:
                    stats_embed = await self._build_global_stats(s2)
                for ch_id, msg_id in list(self._live_stats_messages.items()):
                    ch = self.get_channel(ch_id)
                    if ch is None:
                        try:
                            ch = await self.fetch_channel(ch_id)
                        except Exception:
                            ch = None
                    if ch:
                        try:
                            msg = await ch.fetch_message(msg_id)
                            await msg.edit(embed=stats_embed)
                        except discord.NotFound:
                            self._live_stats_messages.pop(ch_id, None)
                            save_live_stats(self._live_stats_messages)
                        except Exception:
                            pass
                self._live_stats_last_update = now

    @poll_task.before_loop
    async def before_poll(self):
        await self.wait_until_ready()


client = TwitchDropsBot()


def not_linked_embed() -> discord.Embed:
    return error_embed("❌ No dashboard linked. Use `/link <url> <code>` to connect.")


def register_commands(bot: TwitchDropsBot):

    async def name_autocomplete(interaction: discord.Interaction, current: str):
        uid = str(interaction.user.id)
        names = list(bot.users_data.get(uid, {}).keys())
        if not names:
            names = ["default"]
        return [
            app_commands.Choice(name=n, value=n)
            for n in names if current.lower() in n.lower()
        ][:25]

    async def name_autocomplete_with_all(interaction: discord.Interaction, current: str):
        uid = str(interaction.user.id)
        names = list(bot.users_data.get(uid, {}).keys())
        if not names:
            names = ["default"]
        options = (["all"] + names) if len(names) > 1 else names
        return [
            app_commands.Choice(name=n, value=n)
            for n in options if current.lower() in n.lower()
        ][:25]

    @bot.tree.command(name="link", description="Link your TwitchDropsMiner dashboard")
    @app_commands.describe(
        url="Dashboard URL (e.g. http://your-vps:8081)",
        code="Pairing code from Settings → Discord Bot",
        name="Instance name (default: 'default')",
    )
    async def cmd_link(interaction: discord.Interaction, url: str, code: str, name: str = "default"):
        await interaction.response.defer(ephemeral=True)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{url.rstrip('/')}/api/pair/claim",
                    json={"code": code},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

            token = data.get("token")
            if not token:
                await interaction.followup.send(embed=error_embed("❌ No token received. Is the code correct?"), ephemeral=True)
                return

            uid = str(interaction.user.id)
            if uid not in bot.users_data:
                bot.users_data[uid] = {}
            bot.users_data[uid][name] = {
                "url": url.rstrip("/"),
                "token": token,
                "last_drop_count": 0,
                "last_drop_timestamp": None,
                "channels": {"drops": None, "logs": None, "points": None, "bets": None, "campaigns": None},
            }
            save_pairings(bot.users_data)
            log.info("User %s linked instance '%s' to %s", uid, name, url)
            label = f"instance `{name}` " if name != "default" else ""

            # Check if Account 2 is available at /acc2/
            acc2_note = ""
            base = url.rstrip("/")
            already_linked_acc2 = any(
                p.get("url", "").rstrip("/") == f"{base}/acc2"
                for p in bot.users_data.get(uid, {}).values()
            )
            if not already_linked_acc2:
                try:
                    async with aiohttp.ClientSession() as s2:
                        async with s2.get(f"{base}/acc2/api/instance", timeout=aiohttp.ClientTimeout(total=3)) as r2:
                            if r2.status == 200:
                                inst2 = await r2.json()
                                acc2_login = inst2.get("login", "Account 2")
                                acc2_note = (
                                    f"\n\n**Account 2 detected** (`{acc2_login}` at `{base}/acc2`)\n"
                                    f"Link it separately: `/link {base}/acc2 <code> acc2`"
                                )
                except Exception:
                    pass

            await interaction.followup.send(embed=success_embed(f"✅ Connected {label}to `{url}`\n\nUse `/dashboard` to post a live stats embed with control buttons.{acc2_note}"), ephemeral=True)
        except aiohttp.ClientError:
            await interaction.followup.send(embed=error_embed("❌ Dashboard unreachable. Is the URL correct?"), ephemeral=True)
        except Exception as e:
            log.error("Link error: %s", e)
            await interaction.followup.send(embed=error_embed(f"❌ Error connecting: {e}"), ephemeral=True)

    @bot.tree.command(name="unlink", description="Unlink your dashboard")
    @app_commands.describe(name="Instance name to unlink (default: 'default')")
    @app_commands.autocomplete(name=name_autocomplete)
    async def cmd_unlink(interaction: discord.Interaction, name: str = "default"):
        uid = str(interaction.user.id)
        if uid not in bot.users_data or name not in bot.users_data[uid]:
            await interaction.response.send_message(embed=error_embed(f"❌ No instance `{name}` linked."), ephemeral=True)
            return
        del bot.users_data[uid][name]
        if not bot.users_data[uid]:
            del bot.users_data[uid]
        save_pairings(bot.users_data)
        label = f"instance `{name}`" if name != "default" else "dashboard"
        await interaction.response.send_message(embed=success_embed(f"{label.capitalize()} unlinked."), ephemeral=True)

    @bot.tree.command(name="setchannel", description="Set notification channel for drops, points, bets or campaigns")
    @app_commands.describe(
        type="What to post here",
        name="Instance name — use 'all' to set for all linked accounts",
    )
    @app_commands.choices(type=[
        app_commands.Choice(name="drops", value="drops"),
        app_commands.Choice(name="points", value="points"),
        app_commands.Choice(name="bets", value="bets"),
        app_commands.Choice(name="campaigns", value="campaigns"),
    ])
    @app_commands.autocomplete(name=name_autocomplete_with_all)
    async def cmd_setchannel(interaction: discord.Interaction, type: str, name: str = "all"):
        uid = str(interaction.user.id)
        channel_id = interaction.channel_id
        channel_name = interaction.channel.name if interaction.channel else str(channel_id)
        guild_name = interaction.guild.name if interaction.guild else ""
        new_entry = {"id": channel_id, "name": channel_name, "guild": guild_name}

        labels = {
            "drops": "Drop notifications",
            "points": "Channel Points notifications",
            "bets": "Prediction (bet) results",
            "campaigns": "Campaign ending soon alerts",
        }
        label = labels.get(type, type.capitalize())

        target_names = list(bot.users_data.get(uid, {}).keys()) if name == "all" else [name]
        if not target_names:
            await interaction.response.send_message(embed=not_linked_embed(), ephemeral=True)
            return

        updated = []
        for inst_name in target_names:
            pairing = get_user_pairing(bot.users_data, interaction.user.id, inst_name)
            if not pairing:
                continue
            channels = bot.users_data[uid][inst_name].setdefault("channels", {})
            existing = channels.get(type)
            if existing is None:
                channel_list = []
            elif isinstance(existing, list):
                channel_list = existing
            else:
                channel_list = [existing]
            existing_ids = [int(e["id"]) if isinstance(e, dict) else int(e) for e in channel_list if e]
            if channel_id not in existing_ids:
                channel_list.append(new_entry)
            channels[type] = channel_list
            updated.append(inst_name)

        if not updated:
            await interaction.response.send_message(embed=not_linked_embed(), ephemeral=True)
            return

        save_pairings(bot.users_data)
        if len(updated) > 1:
            instances_str = ", ".join(f"`{n}`" for n in updated)
            await interaction.response.send_message(
                embed=success_embed(f"✅ {label} will be posted in <#{channel_id}> for all instances: {instances_str}"),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                embed=success_embed(f"✅ {label} will now be posted in <#{channel_id}>."),
                ephemeral=True,
            )

    @bot.tree.command(name="unsetchannel", description="Remove notification channel (this channel or all channels for a type)")
    @app_commands.describe(
        type="Which notification type to remove",
        name="Instance name — use 'all' for all linked accounts",
    )
    @app_commands.choices(type=[
        app_commands.Choice(name="drops", value="drops"),
        app_commands.Choice(name="points", value="points"),
        app_commands.Choice(name="bets", value="bets"),
        app_commands.Choice(name="campaigns", value="campaigns"),
        app_commands.Choice(name="all types", value="all"),
    ])
    @app_commands.autocomplete(name=name_autocomplete_with_all)
    async def cmd_unsetchannel(interaction: discord.Interaction, type: str, name: str = "all"):
        uid = str(interaction.user.id)
        target_names = list(bot.users_data.get(uid, {}).keys()) if name == "all" else [name]
        if not target_names:
            await interaction.response.send_message(embed=not_linked_embed(), ephemeral=True)
            return

        types_to_clear = ["drops", "points", "bets", "campaigns"] if type == "all" else [type]
        cleared = []
        for inst_name in target_names:
            pairing = get_user_pairing(bot.users_data, interaction.user.id, inst_name)
            if not pairing:
                continue
            channels = bot.users_data[uid][inst_name].setdefault("channels", {})
            for t in types_to_clear:
                if channels.get(t):
                    channels[t] = None
            cleared.append(inst_name)

        if not cleared:
            await interaction.response.send_message(embed=not_linked_embed(), ephemeral=True)
            return

        save_pairings(bot.users_data)
        type_str = "all types" if type == "all" else type
        inst_str = ", ".join(f"`{n}`" for n in cleared)
        await interaction.response.send_message(
            embed=success_embed(f"✅ Cleared **{type_str}** notification channels for {inst_str}."),
            ephemeral=True,
        )

    @bot.tree.command(name="dashboard", description="Post a live-updating stats embed with control buttons")
    @app_commands.describe(name="Instance name (default: 'default')")
    @app_commands.autocomplete(name=name_autocomplete)
    async def cmd_dashboard(interaction: discord.Interaction, name: str = "default"):
        await interaction.response.defer()
        uid = str(interaction.user.id)
        pairing = get_user_pairing(bot.users_data, interaction.user.id, name)
        if not pairing:
            await interaction.followup.send(embed=not_linked_embed(), ephemeral=True)
            return
        try:
            async with aiohttp.ClientSession() as session:
                embed = await build_dashboard_embed(session, pairing["url"], pairing["token"], pairing)

            view = DashboardView(interaction.user.id, bot, name)
            msg = await interaction.followup.send(embed=embed, view=view)

            bot.users_data[uid][name]["dashboard_embed"] = {
                "channel_id": str(interaction.channel_id),
                "message_id": str(msg.id),
            }
            save_pairings(bot.users_data)
        except aiohttp.ClientError:
            await interaction.followup.send(embed=error_embed("❌ Dashboard unreachable. Still connected?"))
        except Exception as e:
            log.error("Dashboard command error: %s", e)
            await interaction.followup.send(embed=error_embed(f"❌ Error: {e}"))


    DEV_USER_ID = 774679828594163802

    class DevPanelView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)

        @discord.ui.button(label="Post Live Stats Here", style=discord.ButtonStyle.primary, emoji="📊")
        async def post_stats(self, interaction: discord.Interaction, button: discord.ui.Button):
            if interaction.user.id != DEV_USER_ID:
                await interaction.response.send_message("❌ Dev only.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            async with aiohttp.ClientSession() as session:
                embed = await bot._build_global_stats(session, str(interaction.user.id))
            msg = await interaction.channel.send(embed=embed)
            bot._live_stats_messages[interaction.channel_id] = msg.id
            save_live_stats(bot._live_stats_messages)
            await interaction.followup.send("✅ Live stats posted — auto-updates every 30 min.", ephemeral=True)

        @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄")
        async def refresh_stats(self, interaction: discord.Interaction, button: discord.ui.Button):
            if interaction.user.id != DEV_USER_ID:
                await interaction.response.send_message("❌ Dev only.", ephemeral=True)
                return
            await interaction.response.defer()
            async with aiohttp.ClientSession() as session:
                embed = await bot._build_global_stats(session, str(interaction.user.id))
            await interaction.message.edit(embed=embed, view=self)

    @bot.tree.command(name="devpanel", description="Developer panel — restricted")
    async def cmd_devpanel(interaction: discord.Interaction):
        if interaction.user.id != DEV_USER_ID:
            await interaction.response.send_message(embed=error_embed("❌ Access denied."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with aiohttp.ClientSession() as session:
            embed = await bot._build_global_stats(session, str(interaction.user.id))
        view = DevPanelView()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


if __name__ == "__main__":
    client.run(BOT_TOKEN)
