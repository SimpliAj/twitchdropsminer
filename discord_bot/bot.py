import os
import json
import asyncio
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
    """Build a rich live-stats embed from multiple API calls."""
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

    # Parse watching channel from status string (e.g. "💤 Idle watching: ohnePixel")
    watching_channel = None
    if "watching:" in status_str.lower():
        watching_channel = status_str.split(":")[-1].strip()

    if paused:
        color = COLOR_PAUSED
        state_icon = "⏸️"
        state_label = "Paused"
    elif "idle" in status_str.lower():
        color = 0x5865F2
        state_icon = "💤"
        state_label = "Idle Watching"
    elif status_str:
        color = COLOR_SUCCESS
        state_icon = "🟢"
        state_label = "Mining Drops"
    else:
        color = COLOR_ERROR
        state_icon = "🔴"
        state_label = "Unknown"

    lines = [f"**{state_icon} {state_label}**"]
    if watching_channel:
        lines.append(f"📺 Watching: **{watching_channel}**")
    if account:
        lines.append(f"👤 Account: `{account}`")

    embed = discord.Embed(
        title="TwitchDropsMiner — Live Dashboard",
        description="\n".join(lines),
        color=color,
    )

    # Active campaign + next drop
    try:
        camp_data = await api_get(session, url, token, "/api/campaigns")
        campaigns = camp_data.get("campaigns", []) if isinstance(camp_data, dict) else camp_data
        active = [c for c in campaigns if c.get("active") or (not c.get("expired") and not c.get("upcoming"))]
        if not active:
            active = [c for c in campaigns if not c.get("expired")]

        featured = None
        thumb_url = None
        for c in active:
            drops = c.get("drops", [])
            has_progress = any(not d.get("is_claimed") for d in drops)
            if has_progress:
                featured = c
                thumb_url = c.get("game_box_art_url")
                break
        if not featured and active:
            featured = active[0]
            thumb_url = featured.get("game_box_art_url")

        if thumb_url:
            embed.set_thumbnail(url=thumb_url)

        if featured:
            game = featured.get("game_name") or featured.get("name", "Unknown")
            claimed = featured.get("claimed_drops", 0)
            total = featured.get("total_drops", 0)
            embed.add_field(
                name="🎮 Campaign",
                value=f"**{featured['name']}**\n{game}\n`{claimed}/{total}` drops",
                inline=True,
            )

            # Find next unclaimed drop
            drops = featured.get("drops", [])
            claimable = [d for d in drops if d.get("can_claim")]
            in_progress = [d for d in drops if not d.get("is_claimed") and not d.get("can_claim") and d.get("current_minutes", 0) > 0]
            upcoming = [d for d in drops if not d.get("is_claimed") and not d.get("can_claim") and d.get("current_minutes", 0) == 0]

            if claimable:
                nd = claimable[0]
                benefit = nd.get("benefits", [{}])[0].get("name", nd.get("name", "Drop"))
                drop_val = f"**{benefit}**\n✅ Ready to claim!"
            elif in_progress:
                nd = in_progress[0]
                benefit = nd.get("benefits", [{}])[0].get("name", nd.get("name", "Drop"))
                watched = nd.get("current_minutes", 0)
                required = nd.get("required_minutes", 0)
                pct = min(int(watched / required * 100), 100) if required else 0
                filled = int(pct / 10)
                bar = "█" * filled + "░" * (10 - filled)
                remaining = max(required - watched, 0)
                drop_val = f"**{benefit}**\n`{bar}` {pct}%\n⏱️ ~{remaining}m left"
            elif upcoming:
                nd = upcoming[0]
                benefit = nd.get("benefits", [{}])[0].get("name", nd.get("name", "Drop"))
                drop_val = f"**{benefit}**\nWaiting to start…"
            else:
                drop_val = "All drops claimed ✅"

            embed.add_field(name="🎁 Next Drop", value=drop_val, inline=True)
        else:
            embed.add_field(name="🎮 Campaigns", value="No active campaigns", inline=True)

    except Exception as ex:
        log.debug("Dashboard campaigns error: %s", ex)

    # Last drop + total
    try:
        history = await api_get(session, url, token, "/api/drops-history")
        if isinstance(history, list) and history:
            last = history[-1]
            reward = last.get("reward") or last.get("drop") or "?"
            game = last.get("game", "")
            ts = last.get("timestamp", "")[:10] if last.get("timestamp") else ""
            embed.add_field(
                name="🏆 Last Drop",
                value=f"**{reward}**\n{game}{' · ' + ts if ts else ''}",
                inline=True,
            )
            embed.add_field(name="📈 Total Claimed", value=f"**{len(history)}** drops", inline=True)
    except Exception:
        pass

    embed.set_footer(text="TwitchDropsMiner Bot • Updates every 30s")
    embed.timestamp = datetime.now(timezone.utc)
    return embed


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

                    # Update live dashboard embed if configured
                    dashboard = pairing.get("dashboard_embed")
                    if dashboard:
                        ch = self.get_channel(int(dashboard["channel_id"]))
                        if ch:
                            try:
                                msg = await ch.fetch_message(int(dashboard["message_id"]))
                                new_embed = await build_dashboard_embed(session, pairing["url"], pairing["token"])
                                await msg.edit(embed=new_embed)
                            except discord.NotFound:
                                # Message was deleted, clear it
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
    @app_commands.describe(url="Dashboard URL (e.g. http://your-vps:8081)", code="Pairing code (e.g. DROPS-A1B2C3D4)")
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
            await interaction.followup.send(embed=success_embed(f"✅ Connected to `{url}`"), ephemeral=True)
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

    @bot.tree.command(name="status", description="Show current miner status")
    async def cmd_status(interaction: discord.Interaction):
        await interaction.response.defer()
        pairing = get_user_pairing(bot.users_data, interaction.user.id)
        if not pairing:
            await interaction.followup.send(embed=not_linked_embed(), ephemeral=True)
            return
        try:
            async with aiohttp.ClientSession() as session:
                data = await api_get(session, pairing["url"], pairing["token"], "/api/status")

            paused = data.get("paused", False)
            status = data.get("status", "unknown")
            login_info = data.get("login", {})
            account = login_info.get("user_login") if isinstance(login_info, dict) else str(login_info)
            manual = data.get("manual_mode", {})
            manual_active = manual.get("active") if isinstance(manual, dict) else bool(manual)

            if paused:
                color, status_str = COLOR_PAUSED, "⏸️ Paused"
            elif status and "idle" in status.lower():
                color, status_str = 0x5865F2, status
            elif status:
                color, status_str = COLOR_SUCCESS, status
            else:
                color, status_str = COLOR_ERROR, "🔴 Unknown"

            embed = discord.Embed(title="TwitchDropsMiner Status", color=color)
            embed.add_field(name="Status", value=status_str, inline=False)
            embed.add_field(name="Account", value=f"`{account}`", inline=True)
            embed.add_field(name="Paused", value="Yes" if paused else "No", inline=True)
            embed.add_field(name="Manual Mode", value="Yes" if manual_active else "No", inline=True)
            make_footer(embed)
            await interaction.followup.send(embed=embed)
        except aiohttp.ClientError:
            await interaction.followup.send(embed=error_embed("❌ Dashboard unreachable. Still connected?"))

    @bot.tree.command(name="pause", description="Pause the miner")
    async def cmd_pause(interaction: discord.Interaction):
        await interaction.response.defer()
        pairing = get_user_pairing(bot.users_data, interaction.user.id)
        if not pairing:
            await interaction.followup.send(embed=not_linked_embed(), ephemeral=True)
            return
        try:
            async with aiohttp.ClientSession() as session:
                await api_post(session, pairing["url"], pairing["token"], "/api/pause")
            embed = discord.Embed(description="⏸️ Miner paused", color=COLOR_PAUSED)
            make_footer(embed)
            await interaction.followup.send(embed=embed)
        except aiohttp.ClientError:
            await interaction.followup.send(embed=error_embed("❌ Dashboard unreachable. Still connected?"))

    @bot.tree.command(name="resume", description="Resume the miner")
    async def cmd_resume(interaction: discord.Interaction):
        await interaction.response.defer()
        pairing = get_user_pairing(bot.users_data, interaction.user.id)
        if not pairing:
            await interaction.followup.send(embed=not_linked_embed(), ephemeral=True)
            return
        try:
            async with aiohttp.ClientSession() as session:
                await api_post(session, pairing["url"], pairing["token"], "/api/resume")
            embed = discord.Embed(description="▶️ Miner resumed", color=COLOR_SUCCESS)
            make_footer(embed)
            await interaction.followup.send(embed=embed)
        except aiohttp.ClientError:
            await interaction.followup.send(embed=error_embed("❌ Dashboard unreachable. Still connected?"))

    @bot.tree.command(name="campaigns", description="List active drop campaigns")
    async def cmd_campaigns(interaction: discord.Interaction):
        await interaction.response.defer()
        pairing = get_user_pairing(bot.users_data, interaction.user.id)
        if not pairing:
            await interaction.followup.send(embed=not_linked_embed(), ephemeral=True)
            return
        try:
            async with aiohttp.ClientSession() as session:
                data = await api_get(session, pairing["url"], pairing["token"], "/api/campaigns")

            campaigns = data.get("campaigns", []) if isinstance(data, dict) else data
            if not campaigns:
                embed = discord.Embed(description="No active campaigns.", color=COLOR_TWITCH)
                make_footer(embed)
                await interaction.followup.send(embed=embed)
                return

            embed = discord.Embed(title="🎮 Active Campaigns", color=COLOR_TWITCH)
            for camp in campaigns[:10]:
                name = camp.get("name") or camp.get("game") or "Unknown"
                drops = camp.get("drops", [])
                claimed = sum(1 for d in drops if d.get("is_claimed")) if drops else camp.get("claimed", 0)
                total = len(drops) if drops else camp.get("total", "?")
                game = camp.get("game", "")
                value = f"{game}\n{claimed}/{total} drops" if game and game != name else f"{claimed}/{total} drops"
                embed.add_field(name=name, value=value, inline=True)
            make_footer(embed)
            await interaction.followup.send(embed=embed)
        except aiohttp.ClientError:
            await interaction.followup.send(embed=error_embed("❌ Dashboard unreachable. Still connected?"))

    @bot.tree.command(name="drops", description="Show recent claimed drops")
    async def cmd_drops(interaction: discord.Interaction):
        await interaction.response.defer()
        pairing = get_user_pairing(bot.users_data, interaction.user.id)
        if not pairing:
            await interaction.followup.send(embed=not_linked_embed(), ephemeral=True)
            return
        try:
            async with aiohttp.ClientSession() as session:
                history = await api_get(session, pairing["url"], pairing["token"], "/api/drops-history")

            if not history:
                embed = discord.Embed(description="No drops claimed yet.", color=COLOR_TWITCH)
                make_footer(embed)
                await interaction.followup.send(embed=embed)
                return

            embed = discord.Embed(title="🎁 Recent Drops", color=COLOR_TWITCH)
            for drop in history[-10:][::-1]:
                game = drop.get("game", "Unknown")
                reward = drop.get("reward") or drop.get("drop") or "Unknown"
                ts = drop.get("timestamp", "")[:10] if drop.get("timestamp") else ""
                embed.add_field(name=game, value=f"**{reward}**\n{ts}", inline=True)
            make_footer(embed)
            await interaction.followup.send(embed=embed)
        except aiohttp.ClientError:
            await interaction.followup.send(embed=error_embed("❌ Dashboard unreachable. Still connected?"))

    @bot.tree.command(name="accounts", description="List linked Twitch accounts")
    async def cmd_accounts(interaction: discord.Interaction):
        await interaction.response.defer()
        pairing = get_user_pairing(bot.users_data, interaction.user.id)
        if not pairing:
            await interaction.followup.send(embed=not_linked_embed(), ephemeral=True)
            return
        try:
            async with aiohttp.ClientSession() as session:
                accounts = await api_get(session, pairing["url"], pairing["token"], "/api/accounts")

            embed = discord.Embed(title="👤 Accounts", color=COLOR_TWITCH)
            items = accounts if isinstance(accounts, list) else accounts.get("accounts", [])
            labels = []
            for acc in items:
                if isinstance(acc, str):
                    labels.append(acc)
                elif isinstance(acc, dict):
                    label = acc.get("username") or acc.get("name") or acc.get("login") or str(acc)
                    labels.append(label)
            embed.description = "\n".join(f"• {l}" for l in labels) if labels else "No accounts found."
            make_footer(embed)
            await interaction.followup.send(embed=embed)
        except aiohttp.ClientError:
            await interaction.followup.send(embed=error_embed("❌ Dashboard unreachable. Still connected?"))

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

    @bot.tree.command(name="dashboard", description="Post a live-updating stats embed in this channel")
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

            msg = await interaction.followup.send(embed=embed)

            # Store message + channel so the poll loop can update it
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
    client.run(BOT_TOKEN, log_handler=None)
