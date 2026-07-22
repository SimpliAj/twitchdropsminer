import re
from pathlib import Path


APP_JS = Path(__file__).resolve().parents[1] / "web" / "static" / "app.js"


def _auto_clean_wanted_queue_source() -> str:
    source = APP_JS.read_text(encoding="utf-8")
    match = re.search(
        r"function autoCleanWantedQueue\(\).*?\n\}", source, re.DOTALL
    )
    assert match, "autoCleanWantedQueue() not found in web/static/app.js"
    return match.group(0)


def test_does_not_use_naive_claimed_drops_equality():
    # A campaign with a sub-gated tier (required_subs > 0, e.g. an
    # "UltraViolet"-style reward) can never satisfy claimed_drops ===
    # total_drops — Twitch never lets that claim through without a real
    # subscription. Checking for literal equality kept the game in the
    # watch queue forever, endlessly re-picked as an earn target, even
    # though every earnable drop was actually done.
    source = _auto_clean_wanted_queue_source()
    assert "c.claimed_drops === c.total_drops" not in source


def test_treats_sub_gated_and_locally_completed_drops_as_done():
    source = _auto_clean_wanted_queue_source()
    assert "required_subs" in source
    assert "current_minutes" in source and "required_minutes" in source
