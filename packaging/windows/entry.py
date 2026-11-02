"""PyInstaller entry point: the repository's main.py launcher, plus a safety net.

The packaged app has no console, so an error during start-up would otherwise just make the
process vanish. Here (before any miner code runs, using only the standard library) the start is
noted in ``%APPDATA%\\TwitchDropsMiner\\logs\\startup.log``, and an unexpected error is written
there too and shown in a message box.
"""

import multiprocessing
import os
import runpy
import sys
import traceback
from datetime import datetime
from pathlib import Path


def _logs_dir() -> Path:
    base = os.environ.get("TDM_DATA_DIR")
    root = Path(base) if base else Path(os.environ.get("APPDATA") or Path.home()) / "TwitchDropsMiner"
    return root / "logs"


def _note(text: str) -> None:
    """Append a line to startup.log (best effort; never raises)."""
    try:
        logs = _logs_dir()
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / "startup.log", "a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} pid={os.getpid()} {text}\n")
    except OSError:
        pass


def _show_error(details: str) -> None:
    """A message box, since there is no console to print to (Windows only, best effort)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        last_lines = "\n".join(details.strip().splitlines()[-6:])
        ctypes.windll.user32.MessageBoxW(
            0,
            f"Twitch Drops Miner stopped.\n\n{last_lines}\n\n"
            f"Details: {_logs_dir() / 'startup.log'}",
            "Twitch Drops Miner",
            0x10,  # MB_ICONERROR
        )
    except Exception:
        pass


def main() -> None:
    multiprocessing.freeze_support()
    _note(f"starting args={sys.argv[1:]}")
    try:
        runpy.run_module("src", run_name="__main__")
    except SystemExit as exc:
        _note(f"exited with code {exc.code!r}")
        if exc.code not in (None, 0):
            _show_error(
                f"The app stopped unexpectedly (exit code {exc.code!r}).\n"
                "The log file TDM.log in the same folder may say why."
            )
        raise
    except BaseException:
        details = traceback.format_exc()
        _note(f"CRASH\n{details}")
        _show_error(details)
        raise
    _note("returned normally")


if __name__ == "__main__":
    main()
