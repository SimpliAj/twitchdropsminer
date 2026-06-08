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

COLOR_TWITCH = 0x9147FF
COLOR_SUCCESS = 0x00B04F
COLOR_ERROR = 0xEB4A4A
COLOR_PAUSED = 0xFF9900


def load_pairings() -> dict:
    if PAIRINGS_PATH.exists():
        with open(PAIRINGS_PATH) as f:
            data = json.load(f)
        return data.get("users", {})
    return {}


def save_pairings(users: dict) -> None:
    with open(PAIRINGS_PATH, "w") as f:
        json.dump({"users": users}, f, indent=2)


def get_user_pairing(users: dict, user_id: int) -> dict | None:
    return users.get(str(user_id))


def _channel_ids(pairing: dict, type: str) -> list[int]:
    """Return list of channel IDs for a notification type (supports single int or list)."""
    val = pairing.get("channels", {}).get(type)
    if val is None:
        return []
    if isinstance(val, list):
        return [int(v) for v in val if v]
    return [int(val)]


def make_footer(embed: discord.Embed) -> None:
    embed.set_footer(text="TwitchDropsMiner Bot")
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


async def build_dashboard_embed(session: aiohttp.ClientSession, url: str, token: str) -> discord.Embed:
    try:
        status_data = await api_get(session, url, token, "/api/status")
    except Exception:
        e = discord.Embed(title="TwitchDropsMiner — Dashboard", description="❌ Dashboard unreachable", color=COLOR_ERROR)
        e.set_footer(text="TwitchDropsMiner Bot • Updates every 30s")
        e.timestamp = datetime.now(timezone.utc)
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

    embed.set_footer(text="TwitchDropsMiner Bot • Auto-updates on change")
    embed.timestamp = datetime.now(timezone.utc)
    return embed


class DashboardView(discord.ui.View):
    def __init__(self, owner_id: int, bot: "TwitchDropsBot"):
        super().__init__(timeout=None)
        self.owner_id = owner_id
        self.bot = bot

    def _pairing(self) -> dict | None:
        return self.bot.users_data.get(str(self.owner_id))

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
            new_embed = await build_dashboard_embed(session, pairing["url"], pairing["token"])
        await interaction.message.edit(embed=new_embed, view=DashboardView(self.owner_id, self.bot))

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
            await interaction.message.edit(embed=new_embed, view=DashboardView(self.owner_id, self.bot))
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
            await interaction.message.edit(embed=new_embed, view=DashboardView(self.owner_id, self.bot))
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
            await interaction.message.edit(embed=new_embed, view=DashboardView(self.owner_id, self.bot))
        except Exception as e:
            log.debug("Refresh error: %s", e)


class TwitchDropsBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.users_data: dict = {}

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
        async with aiohttp.ClientSession() as session:
            for user_id, pairing in list(self.users_data.items()):
                try:
                    drop_count = pairing.get("last_drop_count", 0)
                    watching_channel = None
                    cp_balance = None
                    paused = False

                    # Fetch account name once for notifications
                    account_name = None
                    try:
                        status_data_pre = await api_get(session, pairing["url"], pairing["token"], "/api/status")
                        login_info = status_data_pre.get("login", {})
                        account_name = login_info.get("user_login") if isinstance(login_info, dict) else str(login_info) if login_info else None
                    except Exception:
                        pass

                    # Check for new drops
                    history = await api_get(session, pairing["url"], pairing["token"], "/api/drops-history")
                    if isinstance(history, list):
                        drop_count = len(history)
                        last_count = pairing.get("last_drop_count", 0)
                        if drop_count > last_count:
                            new_drops = history[:drop_count - last_count]  # newest-first list
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
                                        footer = f"Account: {account_name}" if account_name else "TwitchDropsMiner Bot"
                                        embed.set_footer(text=footer)
                                        embed.timestamp = datetime.now(timezone.utc)
                                        await ch.send(embed=embed)
                                    log.info("Drops notification sent to %s (%d new drops)", drops_ch_id, len(new_drops))
                                else:
                                    log.warning("Drops channel %s not found for user %s", drops_ch_id, user_id)
                            self.users_data[user_id]["last_drop_count"] = drop_count
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

                            if prev_balance is not None and cp_balance > prev_balance:
                                gained = cp_balance - prev_balance
                                log.info("CP gain for %s on %s: +%d (prev=%d now=%d)", user_id, watching_channel, gained, prev_balance, cp_balance)
                                if gained >= 25:
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
                                                description=f"**+{gained:,} pts** on **{watching_channel}**\nBalance: **{cp_balance:,} pts**",
                                                color=COLOR_TWITCH,
                                            )
                                            if account_name:
                                                embed.set_footer(text=f"Account: {account_name}")
                                                embed.timestamp = datetime.now(timezone.utc)
                                            else:
                                                make_footer(embed)
                                            await pts_ch.send(embed=embed)
                                            log.info("CP notification sent to channel %s", pts_ch_id)
                                        else:
                                            log.warning("Points channel %s not found for user %s", pts_ch_id, user_id)

                            self.users_data[user_id].setdefault("last_cp", {})[watching_channel] = cp_balance
                            save_pairings(self.users_data)
                    except Exception as e:
                        log.debug("CP tracking error for %s: %s", user_id, e)

                    # Get paused state for state key
                    try:
                        status_data = await api_get(session, pairing["url"], pairing["token"], "/api/status")
                        paused = status_data.get("paused", False)
                    except Exception:
                        pass

                    # Update live dashboard embed only when state changed
                    dashboard = pairing.get("dashboard_embed")
                    if dashboard:
                        state_key = f"{paused}|{watching_channel}|{cp_balance}|{drop_count}"
                        last_state = pairing.get("last_embed_state", "")
                        if state_key != last_state:
                            ch = self.get_channel(int(dashboard["channel_id"]))
                            if ch:
                                try:
                                    msg = await ch.fetch_message(int(dashboard["message_id"]))
                                    new_embed = await build_dashboard_embed(session, pairing["url"], pairing["token"])
                                    view = DashboardView(int(user_id), self)
                                    await msg.edit(embed=new_embed, view=view)
                                    self.users_data[user_id]["last_embed_state"] = state_key
                                    save_pairings(self.users_data)
                                except discord.NotFound:
                                    self.users_data[user_id].pop("dashboard_embed", None)
                                    save_pairings(self.users_data)
                                except Exception as e:
                                    log.debug("Dashboard embed update error: %s", e)

                except Exception as e:
                    log.debug("Poll error for user %s: %s", user_id, e)

    @poll_task.before_loop
    async def before_poll(self):
        await self.wait_until_ready()


client = TwitchDropsBot()


def not_linked_embed() -> discord.Embed:
    return error_embed("❌ No dashboard linked. Use `/link <url> <code>` to connect.")


def register_commands(bot: TwitchDropsBot):

    @bot.tree.command(name="link", description="Link your TwitchDropsMiner dashboard")
    @app_commands.describe(url="Dashboard URL (e.g. http://your-vps:8081)", code="Pairing code from Settings → Discord Bot")
    async def cmd_link(interaction: discord.Interaction, url: str, code: str):
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
            bot.users_data[uid] = {
                "url": url.rstrip("/"),
                "token": token,
                "last_drop_count": 0,
                "channels": {"drops": None, "logs": None},
            }
            save_pairings(bot.users_data)
            log.info("User %s linked to %s", uid, url)
            await interaction.followup.send(embed=success_embed(f"✅ Connected to `{url}`\n\nUse `/dashboard` to post a live stats embed with control buttons."), ephemeral=True)
        except aiohttp.ClientError:
            await interaction.followup.send(embed=error_embed("❌ Dashboard unreachable. Is the URL correct?"), ephemeral=True)
        except Exception as e:
            log.error("Link error: %s", e)
            await interaction.followup.send(embed=error_embed(f"❌ Error connecting: {e}"), ephemeral=True)

    @bot.tree.command(name="unlink", description="Unlink your dashboard")
    async def cmd_unlink(interaction: discord.Interaction):
        uid = str(interaction.user.id)
        if uid not in bot.users_data:
            await interaction.response.send_message(embed=error_embed("❌ No dashboard linked."), ephemeral=True)
            return
        del bot.users_data[uid]
        save_pairings(bot.users_data)
        await interaction.response.send_message(embed=success_embed("Dashboard unlinked."), ephemeral=True)

    @bot.tree.command(name="setchannel", description="Set notification channel for drops or channel points")
    @app_commands.describe(type="What to post here: drops or points")
    @app_commands.choices(type=[
        app_commands.Choice(name="drops", value="drops"),
        app_commands.Choice(name="points", value="points"),
    ])
    async def cmd_setchannel(interaction: discord.Interaction, type: str):
        uid = str(interaction.user.id)
        pairing = get_user_pairing(bot.users_data, interaction.user.id)
        if not pairing:
            await interaction.response.send_message(embed=not_linked_embed(), ephemeral=True)
            return
        channel_id = interaction.channel_id
        channels = bot.users_data[uid].setdefault("channels", {})
        existing = channels.get(type)
        # Normalize to list
        if existing is None:
            channel_list = []
        elif isinstance(existing, list):
            channel_list = existing
        else:
            channel_list = [existing]
        if channel_id not in channel_list:
            channel_list.append(channel_id)
        channels[type] = channel_list
        save_pairings(bot.users_data)
        label = "Drop notifications" if type == "drops" else "Channel Points notifications"
        if len(channel_list) > 1:
            await interaction.response.send_message(
                embed=success_embed(f"✅ {label} will now also be posted in <#{channel_id}>.\n({len(channel_list)} channels total)"),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                embed=success_embed(f"✅ {label} will now be posted in <#{channel_id}>."),
                ephemeral=True,
            )

    @bot.tree.command(name="dashboard", description="Post a live-updating stats embed with control buttons")
    async def cmd_dashboard(interaction: discord.Interaction):
        await interaction.response.defer()
        uid = str(interaction.user.id)
        pairing = get_user_pairing(bot.users_data, interaction.user.id)
        if not pairing:
            await interaction.followup.send(embed=not_linked_embed(), ephemeral=True)
            return
        try:
            async with aiohttp.ClientSession() as session:
                embed = await build_dashboard_embed(session, pairing["url"], pairing["token"])

            view = DashboardView(interaction.user.id, bot)
            msg = await interaction.followup.send(embed=embed, view=view)

            bot.users_data[uid]["dashboard_embed"] = {
                "channel_id": str(interaction.channel_id),
                "message_id": str(msg.id),
            }
            save_pairings(bot.users_data)
        except aiohttp.ClientError:
            await interaction.followup.send(embed=error_embed("❌ Dashboard unreachable. Still connected?"))
        except Exception as e:
            log.error("Dashboard command error: %s", e)
            await interaction.followup.send(embed=error_embed(f"❌ Error: {e}"))


if __name__ == "__main__":
    client.run(BOT_TOKEN)
