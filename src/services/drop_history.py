"""Persists claimed drops to the per-account drops_history.json used by the WebUI stats."""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
from typing import TYPE_CHECKING

from src.config.paths import DATA_DIR

if TYPE_CHECKING:
    from src.core.client import Twitch
    from src.models.campaign import DropsCampaign
    from src.models.drop import TimedDrop

logger = logging.getLogger(__name__)

_WEB_CONFIG_FILE = DATA_DIR / "web_config.json"
_MAX_ENTRIES = 500


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


def save_drop_claim(game_name: str, drop_name: str, reward_text: str, image_url: str | None) -> None:
    acct_dir = _account_data_dir()
    hist_file = acct_dir / "drops_history.json"
    entry = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "game": game_name,
        "drop": drop_name,
        "reward": reward_text,
        "image_url": image_url,
    }
    try:
        history = json.loads(hist_file.read_text()) if hist_file.exists() else []
    except Exception:
        history = []
    try:
        history.insert(0, entry)
        hist_file.write_text(json.dumps(history[:_MAX_ENTRIES], indent=2))
    except Exception as e:
        logger.warning(f"Failed to save drop claim to history: {e}")

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
        total_file.write_text(json.dumps({"count": total + 1}))
    except Exception as e:
        logger.warning(f"Failed to update drops total counter: {e}")


def finalize_drop_claim(twitch: "Twitch", campaign: "DropsCampaign", drop: "TimedDrop") -> None:
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
    if drop.id in twitch._claim_finalized_drops:
        return
    twitch._claim_finalized_drops.add(drop.id)

    save_drop_claim(
        campaign.game.name,
        drop.name,
        drop.rewards_text(),
        drop.benefits[0].image_url if drop.benefits else None,
    )

    webhook_url = twitch.settings.discord_webhook_drops
    if not webhook_url:
        return
    embed: dict = {
        "title": "🎁 Drop Claimed!",
        "color": 0x9147FF,
        "fields": [
            {"name": "Game", "value": campaign.game.name, "inline": True},
            {"name": "Drop", "value": drop.name, "inline": True},
            {"name": "Reward", "value": drop.rewards_text(), "inline": False},
        ],
    }
    try:
        _acct = json.loads(_WEB_CONFIG_FILE.read_text()).get("active_account", "") if _WEB_CONFIG_FILE.exists() else ""
    except Exception:
        _acct = ""
    if _acct:
        embed["footer"] = {"text": f"Account: {_acct}"}
    if drop.benefits:
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
