"""Allow only one miner per user session (and port) on Windows and macOS."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any


ERROR_ALREADY_EXISTS = 183


class SingleInstanceGuard:
    """Named-mutex guard on Windows, flock on a lock file in the data folder on macOS; a no-op
    everywhere else (Docker/Linux are unaffected).

    The mutex lives in the ``Local\\`` namespace, so two Windows users on one PC (fast user
    switching) can each run their own miner, while one user cannot start a second copy on the
    same port -- which would otherwise fail halfway through boot with "address already in use".
    """

    def __init__(
        self,
        port: int,
        kernel32: Any | None = None,
        last_error: Callable[[], int] | None = None,
    ) -> None:
        self.port = port
        self._lock_file: Any = None
        self.name = f"Local\\TwitchDropsMiner-{port}"
        self._kernel32 = kernel32
        self._last_error = last_error
        self._handle: Any = None

    def _api(self) -> Any | None:
        if self._kernel32 is not None:
            return self._kernel32
        if sys.platform != "win32":
            return None
        import ctypes  # noqa: PLC0415

        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._last_error = ctypes.get_last_error
        return self._kernel32

    def _acquire_lock_file(self) -> bool:
        import fcntl  # noqa: PLC0415

        from src.config.paths import DATA_DIR  # noqa: PLC0415

        try:
            handle = open(DATA_DIR / f"instance-{self.port}.lock", "w")  # noqa: SIM115
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        except OSError:
            return True  # cannot tell -- never block startup on a guard failure
        self._lock_file = handle  # held (and so locked) until the process exits
        return True

    def acquire(self) -> bool:
        """True if this process now owns the instance, False if another one already does."""
        if sys.platform == "darwin":
            return self._acquire_lock_file()
        api = self._api()
        if api is None:
            return True
        handle = api.CreateMutexW(None, False, self.name)
        if not handle:
            return True  # cannot tell -- never block startup on a guard failure
        if self._last_error is not None and self._last_error() == ERROR_ALREADY_EXISTS:
            api.CloseHandle(handle)
            return False
        self._handle = handle  # held until the process exits
        return True

    def release(self) -> None:
        if self._lock_file is not None:
            self._lock_file.close()
            self._lock_file = None
        if self._kernel32 is not None and self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None
