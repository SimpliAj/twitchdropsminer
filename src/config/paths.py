"""Path-related configuration and environment detection."""

from __future__ import annotations

import json
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
import os as _os
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_data_dir_env = _os.environ.get("TDM_DATA_DIR")
DATA_DIR = Path(_data_dir_env) if _data_dir_env else PROJECT_ROOT / "data"

# Ensure data directory exists
if not DATA_DIR.exists():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

# Translations path
# NOTE: These don't have to be available to the end-user, so the path points to the internal dir
LANG_PATH = PROJECT_ROOT / "lang"

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
