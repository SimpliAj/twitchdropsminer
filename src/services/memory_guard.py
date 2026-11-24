"""Restart before the miner's memory can freeze the computer.

Users saw the process grow to 7-12 GB until Windows hard-locked (issue #46); a
Raspberry Pi's kernel kills it at ~500 MB. The cause isn't found yet, so this
guard restarts the miner well before that and, from halfway up, traces Python
allocations so the log names the code that grew.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import faulthandler
import gc
import logging
import os
import sys
import tracemalloc
from collections.abc import Callable


logger = logging.getLogger("TwitchDrops")

CHECK_INTERVAL = 60
MAX_LIMIT = 2 * 1024**3  # never let it grow past 2 GB
MIN_LIMIT = 300 * 1024**2


def _windows_memory() -> tuple[int | None, int | None]:
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (n, ctypes.c_size_t) for n in (
                "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage",
                "PrivateUsage",
            )
        ]

    class Status(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD)] + [
            (n, ctypes.c_ulonglong) for n in (
                "ullTotalPhys", "ullAvailPhys", "ullTotalPageFile", "ullAvailPageFile",
                "ullTotalVirtual", "ullAvailVirtual", "ullAvailExtendedVirtual",
            )
        ]

    counters = Counters(cb=ctypes.sizeof(Counters))
    psapi = ctypes.WinDLL("psapi")
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    ok = psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
    status = Status(dwLength=ctypes.sizeof(Status))
    total = status.ullTotalPhys if kernel32.GlobalMemoryStatusEx(ctypes.byref(status)) else None
    return (counters.PrivateUsage if ok else None), total


def memory() -> tuple[int | None, int | None]:
    """(this process's memory in bytes, total RAM in bytes); None where unknown."""
    try:
        if sys.platform == "win32":
            return _windows_memory()
        if sys.platform.startswith("linux"):
            with open("/proc/self/statm") as f:
                rss = int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
            with open("/proc/meminfo") as f:
                total = int(f.readline().split()[1]) * 1024
            return rss, total
    except Exception:  # a reading problem must never stop the miner
        pass
    return None, None  # shortcut: macOS isn't covered (no cheap current-RSS call), add if reported


def limit_for(total: int | None) -> int:
    if not total:
        return MAX_LIMIT
    return max(MIN_LIMIT, min(MAX_LIMIT, total // 2))


TRIM_EVERY = 10  # checks, i.e. about every 10 minutes
STALL_TIMEOUT = 180  # seconds the event loop may stand still before its stack is dumped
LOW_AVAILABLE = 0.08  # share of RAM left for the whole system that counts as nearly out
LOW_MEMORY_RSS = 150 * 1024**2  # restart for low system memory only once we're this big


def available_memory() -> int | None:
    """Memory the whole system still has, in bytes (Linux only)."""
    # shortcut: Linux only (the Pi case); Windows/macOS rely on the per-process limit
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _supervised() -> bool:
    """A supervisor (systemd, Docker, pm2) starts the process again after it exits."""
    return bool(os.environ.get("INVOCATION_ID") or os.environ.get("pm_id")) or os.path.exists("/.dockerenv")  # noqa: SIM112 (pm2 sets lowercase pm_id)


def _log_stream():
    for handler in logging.getLogger("TwitchDrops").handlers + logging.getLogger().handlers:
        if isinstance(handler, logging.FileHandler) and handler.stream:
            return handler.stream
    return sys.stderr


def arm_stall_dump() -> None:
    """If the event loop stops for STALL_TIMEOUT, the C watchdog thread writes every thread's
    stack to the log (works while Python itself is stuck) and, when supervised, exits so the
    supervisor restarts the miner before a frozen, growing process takes the machine down."""
    with contextlib.suppress(Exception):
        stream = _log_stream()
        stream.flush()
        faulthandler.dump_traceback_later(STALL_TIMEOUT, file=stream, exit=_supervised())


def release_free_memory() -> None:
    """Collect garbage and hand freed heap memory back to the OS (glibc keeps it otherwise)."""
    gc.collect()
    if sys.platform.startswith("linux"):
        with contextlib.suppress(OSError, AttributeError):
            ctypes.CDLL("libc.so.6").malloc_trim(0)


async def run(
    restart: Callable[[], None],
    read: Callable[[], tuple[int | None, int | None]] = memory,
    available: Callable[[], int | None] = available_memory,
) -> None:
    tracing = False
    checks = 0
    while True:
        arm_stall_dump()
        await asyncio.sleep(CHECK_INTERVAL)
        checks += 1
        if checks % TRIM_EVERY == 0:
            release_free_memory()
        rss, total = read()
        if rss is None:
            continue
        limit = limit_for(total)
        if not tracing and rss > limit // 2:
            tracing = True
            tracemalloc.start(8)
            logger.warning(f"Memory use is high ({rss >> 20} MB); tracing allocations to find out why")
        free = available()
        if total and free is not None and free < total * LOW_AVAILABLE and rss > LOW_MEMORY_RSS:
            logger.warning(
                f"System memory nearly gone ({free >> 20} MB left, miner uses {rss >> 20} MB); restarting the miner"
            )
            faulthandler.cancel_dump_traceback_later()
            restart()
            return
        if rss > limit:
            if tracing:
                top = tracemalloc.take_snapshot().statistics("traceback")[:8]
                for stat in top:
                    logger.warning(f"[memory] {stat.size >> 20} MB in {stat.count} blocks: {stat.traceback.format()[-1].strip()}")
            logger.warning(f"Memory use reached {rss >> 20} MB (limit {limit >> 20} MB); restarting the miner")
            faulthandler.cancel_dump_traceback_later()
            restart()
            return
