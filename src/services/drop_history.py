"""Persists claimed drops to the per-account drops_history.json used by the WebUI stats."""

from __future__ import annotations

import datetime
import json
import logging

from src.config.paths import DATA_DIR

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
