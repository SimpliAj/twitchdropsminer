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

    desc_parts = [state_line]
    if account:
        desc_parts.append(f"👤  `{account}`")

    embed = discord.Embed(
        title="TwitchDropsMiner",
        description="  ·  ".join(desc_parts),
        color=color,
    )

    # Watching channel + channel points
    try:
        idle_data = await api_get(session, url, token, "/api/idle-watch/status")
        w_login = idle_data.get("watching")
        w_name = idle_data.get("display_name") or w_login
        online = idle_data.get("online", False)
        if w_name:
            watch_val = f"**{w_name}**  {'🟢 Live' if online else '⚫ Offline'}"
            watching_login = w_login or watching_login
            if watching_login:
                try:
                    cp_data = await api_get(session, url, token, f"/api/channel-points/{watching_login}")
                    balance = cp_data.get("balance")
                    if balance is not None:
                        watch_val += f"\n💰 **{balance:,}** pts"
                except Exception:
                    pass
            embed.add_field(name="📺 Watching", value=watch_val, inline=False)
    except Exception:
        if watching_login:
            embed.add_field(name="📺 Watching", value=f"**{watching_login}**", inline=False)

    # Thumbnail from active campaign
    try:
        camp_data = await api_get(session, url, token, "/api/campaigns")
        campaigns = camp_data.get("campaigns", []) if isinstance(camp_data, dict) else camp_data
        for c in campaigns:
            if not c.get("expired") and any(not d.get("is_claimed") for d in c.get("drops", [])):
                thumb = c.get("game_box_art_url")
                if thumb:
                    embed.set_thumbnail(url=thumb)
                break
    except Exception:
        pass

    # Total drops
    try:
        history = await api_get(session, url, token, "/api/drops-history")
        if isinstance(history, list):
            embed.add_field(name="📈 Total Drops", value=f"**{len(history)}**", inline=True)
    except Exception:
        pass

    # Wanted Queue
    try:
        wanted_data = await api_get(session, url, token, "/api/wanted-items")
        wanted_games = wanted_data.get("wanted_items", []) if isinstance(wanted_data, dict) else []
        if wanted_games:
            lines = []
            for game in wanted_games[:5]:
                gname = game.get("game_name", "Unknown")
                drop_count = sum(len(c.get("drops", [])) for c in game.get("campaigns", []))
                lines.append(f"• **{gname}** — {drop_count} drop{'s' if drop_count != 1 else ''}")
            if len(wanted_games) > 5:
                lines.append(f"…and {len(wanted_games) - 5} more")
            embed.add_field(name="🎯 Wanted Queue", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="🎯 Wanted Queue", value="No wanted drops configured", inline=False)
    except Exception:
        pass

    embed.set_footer(text="TwitchDropsMiner Bot • Updates every 30s")
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
                for camp in campaigns[:10]:
                    name = camp.get("name") or "Unknown"
                    drops = camp.get("drops", [])
                    claimed = sum(1 for d in drops if d.get("is_claimed"))
                    total = len(drops)
                    game = camp.get("game_name", "")
                    val = f"{game}\n{claimed}/{total} drops" if game else f"{claimed}/{total} drops"
                    embed.add_field(name=name, value=val, inline=True)
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
                for drop in history[-10:][::-1]:
                    game = drop.get("game", "Unknown")
                    reward = drop.get("reward") or drop.get("drop") or "Unknown"
                    ts = drop.get("timestamp", "")[:10] if drop.get("timestamp") else ""
                    embed.add_field(name=game, value=f"**{reward}**\n{ts}", inline=True)
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
                    # Check for new drops
                    history = await api_get(session, pairing["url"], pairing["token"], "/api/drops-history")
                    if isinstance(history, list):
                        current_count = len(history)
                        last_count = pairing.get("last_drop_count", 0)
                        if current_count > last_count:
                            new_drops = history[last_count:current_count]
                            self.users_data[user_id]["last_drop_count"] = current_count
                            save_pairings(self.users_data)

                            drops_channel_id = pairing.get("channels", {}).get("drops")
                            if drops_channel_id:
                                channel = self.get_channel(int(drops_channel_id))
                                if channel:
                                    embed = discord.Embed(title="🎁 New Drops Claimed!", color=COLOR_TWITCH)
                                    for drop in new_drops[-10:]:
                                        game = drop.get("game", "Unknown")
                                        reward = drop.get("reward") or drop.get("drop") or "Unknown"
                                        ts = drop.get("timestamp", "")[:10] if drop.get("timestamp") else ""
                                        embed.add_field(name=game, value=f"**{reward}**\n{ts}", inline=True)
                                    make_footer(embed)
                                    await channel.send(embed=embed)

                    # Update live dashboard embed
                    dashboard = pairing.get("dashboard_embed")
                    if dashboard:
                        ch = self.get_channel(int(dashboard["channel_id"]))
                        if ch:
                            try:
                                msg = await ch.fetch_message(int(dashboard["message_id"]))
                                new_embed = await build_dashboard_embed(session, pairing["url"], pairing["token"])
                                view = DashboardView(int(user_id), self)
                                await msg.edit(embed=new_embed, view=view)
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

    @bot.tree.command(name="setchannel", description="Set notification channel for drops or logs")
    @app_commands.describe(type="Channel type: drops or logs")
    @app_commands.choices(type=[
        app_commands.Choice(name="drops", value="drops"),
        app_commands.Choice(name="logs", value="logs"),
    ])
    async def cmd_setchannel(interaction: discord.Interaction, type: str):
        uid = str(interaction.user.id)
        pairing = get_user_pairing(bot.users_data, interaction.user.id)
        if not pairing:
            await interaction.response.send_message(embed=not_linked_embed(), ephemeral=True)
            return
        channel_id = interaction.channel_id
        bot.users_data[uid]["channels"][type] = channel_id
        save_pairings(bot.users_data)
        label = "Drop notifications" if type == "drops" else "Log messages"
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
