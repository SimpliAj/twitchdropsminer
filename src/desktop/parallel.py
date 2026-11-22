"""Parallel accounts in the desktop build: more copies of the app, each with its own port and data folder."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from src.config.paths import DATA_DIR

from .runtime import _CREATE_NEW_PROCESS_GROUP, RESTART_PARENT_ENV, RESTARTED_ENV, port_in_use


logger = logging.getLogger("TwitchDrops")

# Set on the copies we start: only the copy the user started themselves relaunches the others.
SPAWNED_ENV = "TDM_SPAWNED"
FIRST_PORT = 8081
_INHERITED_FLAGS = {"--no-tray", "--no-browser", "--browser-tab"}


def pick_slot(instances: list[dict[str, Any]]) -> tuple[int, Path]:
    """Next free port from 8081 up and the next unused ``TwitchDropsMiner<N>`` folder beside DATA_DIR."""
    taken = {i["port"] for i in instances if "port" in i}
    port = FIRST_PORT
    while port in taken or port_in_use(port):
        port += 1
    n = 2
    while (DATA_DIR.parent / f"TwitchDropsMiner{n}").exists():
        n += 1
    return port, DATA_DIR.parent / f"TwitchDropsMiner{n}"


def launch(port: int, data_dir: Path, label: str, minimized: bool = False) -> None:
    """Start another copy of this app, detached, so it outlives a restart of this one."""
    env = {
        **os.environ,
        "TDM_PORT": str(port),
        "TDM_DATA_DIR": str(data_dir),
        "TDM_LABEL": label,
        SPAWNED_ENV: "1",
        "PYINSTALLER_RESET_ENVIRONMENT": "1",  # a new app, not a child of this one
    }
    env.pop(RESTART_PARENT_ENV, None)
    env.pop(RESTARTED_ENV, None)
    # --minimized is only for a start at login; the window flags carry over from this copy
    args = [sys.executable, *(a for a in sys.argv[1:] if a in _INHERITED_FLAGS), *(["--minimized"] if minimized else [])]
    extra: dict[str, Any] = {"creationflags": _CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {"start_new_session": True}
    subprocess.Popen(args, env=env, close_fds=True, **extra)  # noqa: S603
    logger.info(f"Started parallel account '{label}' on port {port} ({data_dir})")


def autostart(instances: list[dict[str, Any]], minimized: bool) -> None:
    """Relaunch the spawned copies that are not running (after a reboot or a quit of the whole app)."""
    for inst in instances:
        if inst.get("spawned") and not port_in_use(inst["port"]):
            launch(inst["port"], Path(inst["data_dir"]), inst["label"], minimized)
