"""Run a browser on a separate, invisible Windows desktop.

The integrity token is minted in a real (not headless -- Twitch rejects those) browser window.
Chromium moves windows back into the visible screen area, so window-position tricks still
flashed a twitch.tv/robots.txt window on every start. A process started on its own desktop
(CreateDesktop + STARTUPINFO.lpDesktop) renders and runs JavaScript normally, but nothing of it
ever shows on the user's screen.

``spawn_on_hidden_desktop`` returns an object with the parts of asyncio.subprocess.Process that
PlainChrome uses (returncode, wait(), kill()), or raises OSError so the caller can fall back.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import subprocess
from ctypes import wintypes


_GENERIC_ALL = 0x10000000
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_CREATE_NO_WINDOW = 0x08000000
_STILL_ACTIVE = 259
_DESKTOP_NAME = f"TwitchDropsMiner-{os.getpid()}"


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


_desktop_handle = None  # created once per miner process and kept open (cheap, reused)


def _api():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.CreateDesktopW.restype = wintypes.HANDLE
    user32.CreateDesktopW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p
    ]
    kernel32.CreateProcessW.restype = wintypes.BOOL
    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL,
        wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
        ctypes.POINTER(_STARTUPINFOW), ctypes.POINTER(_PROCESS_INFORMATION),
    ]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return user32, kernel32


def _ensure_desktop(user32) -> str:
    global _desktop_handle
    if _desktop_handle is None:
        handle = user32.CreateDesktopW(_DESKTOP_NAME, None, None, 0, _GENERIC_ALL, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        _desktop_handle = handle
    return _DESKTOP_NAME


def _environment_block(env: dict[str, str]) -> ctypes.Array:
    text = "".join(f"{key}={value}\0" for key, value in sorted(env.items(), key=lambda kv: kv[0].upper()))
    return ctypes.create_unicode_buffer(text + "\0")


class HiddenDesktopProcess:
    """The asyncio.subprocess.Process subset PlainChrome needs."""

    def __init__(self, kernel32, handle, pid: int) -> None:
        self._kernel32 = kernel32
        self._handle = handle
        self.pid = pid
        self._returncode: int | None = None

    @property
    def returncode(self) -> int | None:
        if self._returncode is None and self._handle:
            code = wintypes.DWORD()
            if self._kernel32.GetExitCodeProcess(self._handle, ctypes.byref(code)) and code.value != _STILL_ACTIVE:
                self._returncode = code.value
                self._kernel32.CloseHandle(self._handle)
                self._handle = None
        return self._returncode

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0.2)
        return self._returncode  # type: ignore[return-value]

    def kill(self) -> None:
        if self.returncode is None and self._handle:
            self._kernel32.TerminateProcess(self._handle, 1)


def spawn_on_hidden_desktop(args: list[str], env: dict[str, str]) -> HiddenDesktopProcess:
    """Start ``args`` on the miner's invisible desktop. Raises OSError if Windows refuses."""
    user32, kernel32 = _api()
    desktop = _ensure_desktop(user32)
    startup = _STARTUPINFOW()
    startup.cb = ctypes.sizeof(_STARTUPINFOW)
    startup.lpDesktop = desktop
    info = _PROCESS_INFORMATION()
    command = ctypes.create_unicode_buffer(subprocess.list2cmdline(args))
    if not kernel32.CreateProcessW(
        None, command, None, None, False,
        _CREATE_UNICODE_ENVIRONMENT | _CREATE_NO_WINDOW,
        ctypes.cast(_environment_block(env), ctypes.c_void_p), None,
        ctypes.byref(startup), ctypes.byref(info),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    kernel32.CloseHandle(info.hThread)
    return HiddenDesktopProcess(kernel32, info.hProcess, info.dwProcessId)
