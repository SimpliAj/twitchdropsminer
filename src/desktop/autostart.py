"""Start the miner automatically when the current Windows user logs in.

Uses the per-user ``Run`` registry key (HKCU), which needs no administrator rights and applies
only to the user who enabled it -- exactly "runs when the user that runs the program logs in".

Windows also lets the user switch entries off in Task Manager -> Startup apps. That is stored
separately under ``StartupApproved\\Run``; this manager honours it when reading the state and
clears it again when the user re-enables the option from the dashboard.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from src.config.paths import IS_FROZEN, PROJECT_ROOT


logger = logging.getLogger("TwitchDrops")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
APP_NAME = "TwitchDropsMiner"
# Flag added to the startup command: launch into the tray without opening a browser tab.
MINIMIZED_FLAG = "--minimized"


class AutostartError(RuntimeError):
    """Raised when the startup entry cannot be read or written."""


class AutostartManager:
    """Reads and writes the Windows run-at-login entry for this install."""

    def __init__(
        self,
        app_name: str = APP_NAME,
        command: str | None = None,
        registry: Any | None = None,
    ) -> None:
        self.app_name = app_name
        self._command = command
        self._registry = registry

    # -- environment -----------------------------------------------------------------------

    @property
    def supported(self) -> bool:
        return self._registry is not None or sys.platform == "win32"

    def _winreg(self) -> Any:
        if self._registry is not None:
            return self._registry
        if sys.platform != "win32":
            raise AutostartError("Run at startup is only available on Windows")
        import winreg  # noqa: PLC0415  (Windows-only module)

        return winreg

    # -- command line ----------------------------------------------------------------------

    @staticmethod
    def _quote(path: str | Path) -> str:
        return subprocess.list2cmdline([str(path)])

    def build_command(self) -> str:
        """The exact command Windows should run at login."""
        if self._command is not None:
            return self._command
        if IS_FROZEN:
            return f"{self._quote(sys.executable)} {MINIMIZED_FLAG}"
        # Running from source: use pythonw.exe so no console window appears at login.
        interpreter = Path(sys.executable)
        windowless = interpreter.with_name("pythonw.exe")
        if windowless.exists():
            interpreter = windowless
        return f"{self._quote(interpreter)} {self._quote(PROJECT_ROOT / 'main.py')} {MINIMIZED_FLAG}"

    # -- registry --------------------------------------------------------------------------

    def registered_command(self) -> str | None:
        reg = self._winreg()
        try:
            with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_READ) as key:
                value, _kind = reg.QueryValueEx(key, self.app_name)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise AutostartError(f"Could not read the startup entry: {exc}") from exc
        return str(value)

    def _disabled_by_user(self) -> bool:
        """True when the entry was switched off in Task Manager -> Startup apps."""
        reg = self._winreg()
        try:
            with reg.OpenKey(reg.HKEY_CURRENT_USER, APPROVED_KEY, 0, reg.KEY_READ) as key:
                value, _kind = reg.QueryValueEx(key, self.app_name)
        except (FileNotFoundError, OSError):
            return False
        # Binary blob: first byte even (0x02/0x06) = enabled, odd (0x01/0x03) = disabled.
        return bool(value) and (bytes(value)[0] & 1) == 1

    def _clear_user_disable(self) -> None:
        reg = self._winreg()
        try:
            with reg.OpenKey(reg.HKEY_CURRENT_USER, APPROVED_KEY, 0, reg.KEY_SET_VALUE) as key:
                reg.DeleteValue(key, self.app_name)
        except (FileNotFoundError, OSError):
            pass  # nothing to clear

    def is_enabled(self) -> bool:
        if not self.supported:
            return False
        return self.registered_command() is not None and not self._disabled_by_user()

    def is_stale(self) -> bool:
        """Registered, but pointing at a different command (e.g. the app folder was moved)."""
        registered = self.registered_command()
        return registered is not None and registered != self.build_command()

    def enable(self) -> None:
        reg = self._winreg()
        try:
            with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
                reg.SetValueEx(key, self.app_name, 0, reg.REG_SZ, self.build_command())
        except OSError as exc:
            raise AutostartError(f"Could not create the startup entry: {exc}") from exc
        self._clear_user_disable()
        logger.info("Run at Windows startup: enabled")

    def disable(self) -> None:
        reg = self._winreg()
        try:
            with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
                reg.DeleteValue(key, self.app_name)
        except FileNotFoundError:
            pass  # already absent
        except OSError as exc:
            raise AutostartError(f"Could not remove the startup entry: {exc}") from exc
        logger.info("Run at Windows startup: disabled")

    def set_enabled(self, enabled: bool) -> None:
        if not self.supported:
            raise AutostartError("Run at startup is only available on Windows")
        self.enable() if enabled else self.disable()

    def repair_if_stale(self) -> bool:
        """Re-point an existing entry at the current executable. Returns True if rewritten."""
        if not self.supported:
            return False
        try:
            if self.is_stale():
                self.enable()
                return True
        except AutostartError as exc:
            logger.warning(f"Could not refresh the startup entry: {exc}")
        return False

    def status(self) -> dict[str, Any]:
        """JSON-friendly state for the dashboard."""
        if not self.supported:
            return {"supported": False, "enabled": False}
        try:
            return {
                "supported": True,
                "enabled": self.is_enabled(),
                "disabled_in_task_manager": (
                    self.registered_command() is not None and self._disabled_by_user()
                ),
                "command": self.registered_command(),
            }
        except AutostartError as exc:
            return {"supported": True, "enabled": False, "error": str(exc)}
