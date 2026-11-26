"""Persists claimed drops to the per-account drops_history.json used by the WebUI stats."""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
from typing import TYPE_CHECKING

from src.config.paths import DATA_DIR
from src.i18n import _


if TYPE_CHECKING:
    from src.core.client import Twitch
    from src.models.campaign import DropsCampaign
    from src.models.drop import TimedDrop

logger = logging.getLogger(__name__)

_WEB_CONFIG_FILE = DATA_DIR / "web_config.json"
_MAX_ENTRIES = 500
_manual_claim_notified: set[str] = set()


def _account_data_dir():
    try:
        cfg = json.loads(_WEB_CONFIG_FILE.read_text()) if _WEB_CONFIG_FILE.exists() else {}
        account = cfg.get("active_account")
        if account:
            d = DATA_DIR / "accounts" / account
            d.mkdir(parents=True, exist_ok=True)
            return d
    except Exception:
        pass
    return DATA_DIR


def _write_atomic(path, text: str) -> None:
    """Write via a temp file and rename, so a crash or power loss never leaves half a file."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_history(hist_file) -> list | None:
    """The saved history; [] for none. A broken file is moved aside to .bad (never overwritten);
    None when the file can't be read at all, so the caller doesn't save over it."""
    if not hist_file.exists():
        return []
    try:
        text = hist_file.read_text(encoding="utf-8-sig")
    except OSError as e:
        logger.warning(f"Drop history can't be read, not saving this claim: {e}")
        return None
    try:
        history = json.loads(text)
        if isinstance(history, list) and all(isinstance(x, dict) for x in history):
            return history
    except ValueError:
        pass
    bad = hist_file.with_name(hist_file.name + ".bad")
    n = 0
    while bad.exists():
        n += 1
        bad = hist_file.with_name(f"{hist_file.name}.bad.{n}")
    try:
        os.replace(hist_file, bad)
    except OSError:
        return None
    logger.warning(f"Drop history was damaged; moved it to {bad.name} and started a new one")
    return []


def save_drop_claim(
    game_name: str, drop_name: str, reward_text: str, image_url: str | None
) -> bool:
    """Append a history entry. Returns False when the entry could not be written."""
    acct_dir = _account_data_dir()
    hist_file = acct_dir / "drops_history.json"
    entry = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "game": game_name,
        "drop": drop_name,
        "reward": reward_text,
        "image_url": image_url,
    }
    history = _read_history(hist_file)
    if history is None:
        return False
    try:
        history.insert(0, entry)
        _write_atomic(hist_file, json.dumps(history[:_MAX_ENTRIES], indent=2))
    except Exception as e:
        logger.warning(f"Failed to save drop claim to history: {e}")
        return False

    # drops_history.json is capped at _MAX_ENTRIES for display purposes, so it can't
    # be used as the lifetime total once an account passes that many claims — track
    # the real total separately. Bootstrap from the pre-existing history length so
    # accounts that already had claims before this counter existed don't reset to 0.
    total_file = acct_dir / "drops_total.json"
    try:
        if total_file.exists():
            total = json.loads(total_file.read_text()).get("count", 0)
        else:
            total = len(history) - 1  # history already has the new entry inserted above
        _write_atomic(total_file, json.dumps({"count": total + 1}))
    except Exception as e:
        logger.warning(f"Failed to update drops total counter: {e}")
    return True


def finalize_drop_claim(twitch: Twitch, campaign: DropsCampaign, drop: TimedDrop) -> None:
    """Records a claimed drop exactly once, regardless of which of the three
    code paths (local progress-complete detection, websocket drop-claim
    confirmation, periodic inventory reconciliation) observed the claim.

    `Drop._claim()` returns True for a drop that was already claimed earlier,
    not just on a fresh claim, so every call site sees `claimed=True` when two
    paths race the same drop. Without a single shared gate here, that produced
    duplicate drops_history.json entries (multiple callers each saving) and,
    separately, silently dropped Discord notifications (only two of the three
    call sites had webhook code, so whichever path lacked it "won" silently).
    """
    if getattr(drop, "quiet_claim", False):
        return  # reward grants are recorded one by one from grantCount (record_reward_grant)
    if drop.id in twitch._claim_finalized_drops:
        return
    twitch._claim_finalized_drops.add(drop.id)

    save_drop_claim(
        campaign.game.name,
        drop.name,
        drop.rewards_text(),
        drop.benefits[0].image_url if drop.benefits else None,
    )

    _send_drop_webhook(twitch, drop, {
        "title": "🎁 Drop Claimed!",
        "color": 0x9147FF,
        "fields": [
            {"name": "Game", "value": campaign.game.name, "inline": True},
            {"name": "Drop", "value": drop.name, "inline": True},
            {"name": "Reward", "value": drop.rewards_text(), "inline": False},
        ],
    })


def record_reward_grant(campaign: DropsCampaign, drop: TimedDrop, number: int) -> bool:
    """One history entry per observed grant of a repeating reward goal."""
    return save_drop_claim(
        campaign.game.name,
        f"{drop.name} #{number}",
        drop.rewards_text(),
        drop.benefits[0].image_url if drop.benefits else None,
    )


def announce_reward_grant(twitch: Twitch, campaign: DropsCampaign, drop: TimedDrop, number: int) -> None:
    """Print the grant and send the claim webhook (same message as a claimed drop)."""
    text = f"{campaign.game.name} {drop.rewards_text()} (#{number})"
    twitch.print(_.t["status"]["claimed_drop"].format(drop=text))
    _send_drop_webhook(twitch, drop, {
        "title": "🎁 Drop Claimed!",
        "color": 0x9147FF,
        "fields": [
            {"name": "Game", "value": campaign.game.name, "inline": True},
            {"name": "Drop", "value": f"{drop.name} #{number}", "inline": True},
            {"name": "Reward", "value": drop.rewards_text(), "inline": False},
        ],
    })


def finalize_manual_claim(twitch: Twitch, campaign: DropsCampaign, drop: TimedDrop) -> bool:
    """A drop that was integrity-blocked and now shows as claimed in a fresh
    inventory was claimed by hand: stop tracking the block and record it."""
    from src.models.drop import _blocked_claims

    if not drop.is_claimed or _blocked_claims.pop(drop.id, None) is None:
        return False
    logger.info(f"Drop {drop.name} ({drop.id}) was claimed manually")
    finalize_drop_claim(twitch, campaign, drop)
    return True


def notify_manual_claim(twitch: Twitch, campaign: DropsCampaign, drop: TimedDrop) -> None:
    """One Discord message per drop and process when Twitch's integrity check
    blocks the auto-claim, so the user knows to claim it by hand."""
    if drop.id in _manual_claim_notified:
        return
    _manual_claim_notified.add(drop.id)
    _send_drop_webhook(twitch, drop, {
        "title": "⚠️ Drop needs manual claim",
        "color": 0xF5A623,
        "fields": [
            {"name": "Game", "value": campaign.game.name, "inline": True},
            {"name": "Drop", "value": drop.name, "inline": True},
            {"name": "Reward", "value": drop.rewards_text(), "inline": False},
            {"name": "Claim it here", "value": "https://www.twitch.tv/drops/inventory", "inline": False},
        ],
    })


def _send_drop_webhook(twitch: Twitch, drop: TimedDrop, embed: dict) -> None:
    webhook_url = twitch.settings.discord_webhook_drops
    if not webhook_url:
        return
    try:
        _acct = json.loads(_WEB_CONFIG_FILE.read_text()).get("active_account", "") if _WEB_CONFIG_FILE.exists() else ""
    except Exception:
        _acct = ""
    if _acct:
        embed["footer"] = {"text": f"Account: {_acct}"}
    if drop.benefits and drop.benefits[0].image_url:
        embed["thumbnail"] = {"url": drop.benefits[0].image_url}
    asyncio.create_task(
        twitch._message_handler_service._send_discord_webhook(webhook_url, {"embeds": [embed]})
    )


def get_total_claims(account_dir=None) -> int:
    d = account_dir if account_dir is not None else _account_data_dir()
    total_file = d / "drops_total.json"
    try:
        if total_file.exists():
            return json.loads(total_file.read_text()).get("count", 0)
    except Exception:
        pass
    hist_file = d / "drops_history.json"
    try:
        if hist_file.exists():
            return len(json.loads(hist_file.read_text()))
    except Exception:
        pass
    return 0
