"""Opt-in periodic memory/resource logging for diagnosing long-run leaks.

2026-09-17, GitHub issue #12: a self-hosted user reported the process
becoming unresponsive after "an extended period" in Docker, with VmRSS
~1.17 GiB / VmData ~4.08 GiB / VmSwap ~2.84 GiB and mostly anonymous
(non-heap) memory — but by the time the report was filed the instance had
already crashed, leaving nothing to actually diagnose. This module exists
so the NEXT occurrence (this user's or anyone else's) has real numbers to
look at: opt-in via the TDM_DEBUG_MEMORY env var, logs process RSS plus a
handful of long-lived collections' sizes on an interval, so a slow climb in
one specific collection (rather than a diffuse, unlocatable leak) shows up
directly in the logs instead of needing a live profiler session.

Deliberately uses stdlib `resource` (RSS via getrusage) rather than adding
psutil as a new dependency — this project doesn't already depend on it
(checked pyproject.toml/uv.lock) and RSS is all this needs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from src.utils.async_helpers import task_wrapper

# 2026-09-18, GitHub issue #13: `resource` is POSIX-only stdlib -- doesn't
# exist on Windows at all. This module was only ever meant to be opt-in
# (TDM_DEBUG_MEMORY env var), but src/core/client.py imports
# run_debug_memory_logger unconditionally at startup, so the bare
# `import resource` here crashed EVERY Windows-from-source user on launch
# with ModuleNotFoundError, regardless of whether they ever set the env
# var. Soft-fail instead: run_debug_memory_logger checks availability and
# logs+returns instead of starting on a platform that can't support it.
try:
    import resource
except ImportError:
    resource = None  # type: ignore[assignment]


if TYPE_CHECKING:
    from src.core.client import Twitch


logger = logging.getLogger("TwitchDrops")

DEFAULT_INTERVAL_SECONDS = 300  # 5 minutes


def current_rss_mb() -> float:
    """Current process resident set size, in MiB (Linux: ru_maxrss is KiB)."""
    assert resource is not None
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def collection_sizes(twitch: "Twitch") -> dict[str, int]:
    """
    Sizes of the long-lived collections most likely to grow unbounded over
    a multi-day run, for spotting which one (if any) is actually climbing.
    """
    return {
        "campaigns": len(twitch._campaigns),
        "drops": len(twitch._drops),
        "claim_finalized_drops": len(twitch._claim_finalized_drops),
        "channels": len(twitch.channels),
        "idle_channels": len(twitch._idle_channels_set),
        "asyncio_tasks": len(asyncio.all_tasks()),
    }


@task_wrapper
async def run_debug_memory_logger(
    twitch: "Twitch", interval_seconds: float = DEFAULT_INTERVAL_SECONDS
) -> None:
    """Log RSS + long-lived collection sizes every `interval_seconds`, forever."""
    if resource is None:
        logger.warning(
            "[debug-memory] TDM_DEBUG_MEMORY is set, but the stdlib `resource` "
            "module isn't available on this platform (Windows) -- skipping."
        )
        return
    while True:
        await asyncio.sleep(interval_seconds)
        sizes = collection_sizes(twitch)
        sizes_str = ", ".join(f"{name}={count}" for name, count in sizes.items())
        logger.info(f"[debug-memory] RSS={current_rss_mb():.1f}MiB {sizes_str}")
