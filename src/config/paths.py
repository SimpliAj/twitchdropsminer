"""Path-related configuration and environment detection."""

from __future__ import annotations

import json
import os as _os
import sys as _sys
from pathlib import Path
from typing import Any


def _merge_vars(base_vars: dict[str, Any], vars: dict[str, Any]) -> None:
    """
    Merge variables recursively.

    NOTE: This modifies base_vars in place.
    """
    for k, v in vars.items():
        if k not in base_vars:
            base_vars[k] = v
        elif isinstance(v, dict):
            if isinstance(base_vars[k], dict):
                _merge_vars(base_vars[k], v)
            elif base_vars[k] is Ellipsis:
                # unspecified base, use the passed in var
                base_vars[k] = v
            else:
                raise RuntimeError(f"Var is a dict, base is not: '{k}'")
        elif isinstance(base_vars[k], dict):
            raise RuntimeError(f"Base is a dict, var is not: '{k}'")
        else:
            # simple overwrite
            base_vars[k] = v
    # ensure none of the vars are ellipsis (unset value)
    for k, v in base_vars.items():
        if v is Ellipsis:
            raise RuntimeError(f"Unspecified variable: '{k}'")


# Base Paths - environment-specific resolution


# True when running from a PyInstaller bundle (the Windows desktop build).
IS_FROZEN = bool(getattr(_sys, "frozen", False))

# Read-only bundled resources (web/, lang/, scripts/). In a PyInstaller bundle this is the
# extraction dir (sys._MEIPASS); from source it is the repository root.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
RESOURCE_DIR = Path(getattr(_sys, "_MEIPASS", PROJECT_ROOT)) if IS_FROZEN else PROJECT_ROOT


def _default_data_dir() -> Path:
    """Where user data lives when TDM_DATA_DIR is not set.

    From source / Docker this stays <project_root>/data. A frozen build must NOT write next to
    the executable (it may sit in a read-only location, or be unpacked into a temp dir that is
    wiped on exit), so it uses the per-user application data folder instead.
    """
    if not IS_FROZEN:
        return PROJECT_ROOT / "data"
    base = _os.environ.get("APPDATA") or _os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / "TwitchDropsMiner"
    return Path.home() / ".twitchdropsminer"


_data_dir_env = _os.environ.get("TDM_DATA_DIR")
DATA_DIR = Path(_data_dir_env) if _data_dir_env else _default_data_dir()

# Ensure data directory exists
if not DATA_DIR.exists():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

# Writable per-install state that historically lived in the repo root (logs/, instances.json,
# discord_bot/pairings.json). Frozen builds keep all of it in DATA_DIR.
APP_STATE_DIR = DATA_DIR if IS_FROZEN else PROJECT_ROOT
LOGS_DIR = APP_STATE_DIR / "logs"

# Translations path
# NOTE: These don't have to be available to the end-user, so the path points to the internal dir
LANG_PATH = RESOURCE_DIR / "lang"
WEB_DIR = RESOURCE_DIR / "web"
SCRIPTS_DIR = RESOURCE_DIR / "scripts"

# Persistent storage paths — account-aware
def _get_account_data_dir() -> Path:
    """Return the data dir for the active account, or DATA_DIR root as fallback."""
    config_file = DATA_DIR / "web_config.json"
    if config_file.exists():
        try:
            cfg = json.loads(config_file.read_text())
            account = cfg.get("active_account")
            if account:
                account_dir = DATA_DIR / "accounts" / account
                account_dir.mkdir(parents=True, exist_ok=True)
                return account_dir
        except Exception:
            pass
    return DATA_DIR

_ACCOUNT_DATA_DIR = _get_account_data_dir()
COOKIES_PATH = _ACCOUNT_DATA_DIR / "cookies.jar"
SETTINGS_PATH = _ACCOUNT_DATA_DIR / "settings.json"
