"""Only open the Twitch login window while somebody is at the PC to use it.

Without a saved session the miner opens a browser window on twitch.tv/login. That is fine when
you launched the app yourself, but not when Windows started it at sign-in (``--minimized``), and
not over and over after you closed the window. In the tray build the prompt therefore waits until
the user presses "Log in to Twitch" in the dashboard (the status says so). If that login window is
closed or times out without a login, the gate closes again and the next prompt waits for the next
button press -- it never re-opens by itself. Only this miner process (one account) is affected.
"""

from __future__ import annotations

import asyncio


class LoginGate:
    def __init__(self) -> None:
        self._armed = False
        self.enabled = False  # True while a tray exists that can release the gate
        self._event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def pending(self) -> bool:
        """A login window is being held back until the user asks for it (tray build only)."""
        return self.enabled and (self._armed or self.waiting)

    @property
    def waiting(self) -> bool:
        return self._event is not None and not self._event.is_set()

    def arm(self) -> None:
        """Defer the next login prompt. Only call when something can call release()."""
        self._armed = True

    def rearm(self) -> bool:
        """After a login window ended without a login: wait for the user again.

        Returns False (and does nothing) when there is no tray to release the gate, so a plain
        source/Docker run keeps retrying exactly as it always did."""
        if not self.enabled:
            return False
        self._armed = True
        return True

    async def wait(self) -> None:
        """Called by the auth flow before it opens a login window. Returns at once unless armed."""
        if not self._armed:
            return
        self._armed = False  # only the first prompt is deferred; retries behave as before
        self._loop = asyncio.get_running_loop()
        self._event = asyncio.Event()
        await self._event.wait()

    def release(self) -> None:
        """Let the login window open. Safe to call from any thread, at any time."""
        self._armed = False
        if self._event is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(self._event.set)


login_gate = LoginGate()
