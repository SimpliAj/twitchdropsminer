import asyncio
import os
import subprocess
import sys
from types import SimpleNamespace


def _cookie_name(port):
    env = {k: v for k, v in os.environ.items() if k != "TDM_PORT"}
    if port is not None:
        env["TDM_PORT"] = str(port)
    out = subprocess.run(
        [sys.executable, "-c", "import src.web.app as a; print(a._SESSION_COOKIE)"],
        capture_output=True, text=True, env=env, cwd=".",
    )
    assert out.returncode == 0, out.stderr[-500:]
    return out.stdout.strip().splitlines()[-1]


def test_session_cookie_is_per_port():
    assert _cookie_name(8080) != _cookie_name(8082)
    assert _cookie_name(8082).endswith("8082")
    assert _cookie_name(None) == "__tdm_session"


def test_excluded_drop_is_not_broadcast_as_current():
    from src.web.managers.campaigns import CampaignProgressManager

    emitted = []

    class Broadcaster:
        async def emit(self, name, payload):
            emitted.append(name)

    async def run():
        mgr = CampaignProgressManager(Broadcaster())
        drop = SimpliedDrop(excluded=True)
        mgr.update(drop, 60)
        await asyncio.sleep(0)

    asyncio.run(run())
    assert emitted == []


def SimpliedDrop(excluded):
    campaign = SimpleNamespace(name="c", id="cid", game=SimpleNamespace(name="g"))
    return SimpleNamespace(
        id="d", name="n", campaign=campaign, current_minutes=1, required_minutes=2,
        progress=0.5, excluded=excluded,
    )
