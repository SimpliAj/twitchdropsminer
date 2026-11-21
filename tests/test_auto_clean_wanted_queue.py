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


def _campaign_done_source() -> str:
    match = re.search(r"function campaignDone\(c\).*?\n\}", APP_JS.read_text(encoding="utf-8"), re.DOTALL)
    assert match, "campaignDone() not found in web/static/app.js"
    return match.group(0)


def test_auto_clean_and_auto_add_share_the_done_rule():
    # Different rules made auto-add re-add a finished linked game that auto-clean removed
    # on every dashboard visit, restarting the channel search each time.
    source = APP_JS.read_text(encoding="utf-8")
    assert "gameCampaigns.every(campaignDone)" in _auto_clean_wanted_queue_source()
    add = re.search(r"function autoAddLinkedGames\(\).*?\n\}", source, re.DOTALL).group(0)
    assert "!campaignDone(c)" in add


def test_treats_sub_gated_and_locally_completed_drops_as_done():
    source = _campaign_done_source()
    assert "required_subs" in source
    assert "current_minutes" in source and "required_minutes" in source
