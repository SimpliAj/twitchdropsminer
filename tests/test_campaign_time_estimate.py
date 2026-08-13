import re
from pathlib import Path


APP_JS = Path(__file__).resolve().parents[1] / "web" / "static" / "app.js"


def test_remaining_minutes_uses_max_not_sum():
    """
    Regression test: drops within a campaign share one watch-minutes clock
    and progress in parallel — required-vs-current is watched simultaneously
    for every not-yet-earned tier, so the campaign's remaining time is the
    max across unclaimed drops, not their sum. Reported as a time-estimate
    glitch (Discord, 2026-08-13, screenshot attached): the UI added up every
    drop's remaining minutes as if they ran sequentially.
    """
    app_source = APP_JS.read_text(encoding="utf-8")

    reduce_calls = re.findall(
        r"unclaimed(?:Drops)?\.reduce\(\((\w+), \w+\)\s*=>\s*(.+), 0\);",
        app_source,
    )
    assert len(reduce_calls) == 2, (
        f"expected 2 remaining-minutes reduce() calls, found {len(reduce_calls)}"
    )
    for accumulator, body in reduce_calls:
        # accumulator must be handed into Math.max (max-so-far), not added to
        # (old buggy code did `{acc} + Math.max(0, ...)`, which also happens
        # to contain the substring "Math.max" — check the accumulator's own
        # usage, not just presence of Math.max anywhere in the body)
        assert f"Math.max({accumulator}," in body, (
            f"reduce body doesn't feed the accumulator into Math.max: {body!r}"
        )
        assert re.search(rf"\b{accumulator}\s*\+", body) is None, (
            f"reduce body still sums into the accumulator instead of taking the max: {body!r}"
        )
