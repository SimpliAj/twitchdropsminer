import re
from pathlib import Path


APP_JS = Path(__file__).resolve().parents[1] / "web" / "static" / "app.js"


def test_select_all_games_merges_instead_of_replacing():
    """
    Regression test (ported from upstream rangermix/TwitchDropsMiner #95):
    selectAllGames() used to do
        state.settings.games_to_watch = Array.from(availableGames).sort();
    which REPLACES the watch list outright from availableGames -- a set
    that only fills in as campaigns finish loading. Clicking "Select All"
    before that finished silently wiped out every manually-added game not
    yet in availableGames (real data loss, not just a display glitch).
    It must instead merge into the existing selection, the same way its
    sibling selectLinkedGames/selectBadgeEmoteGames already do.
    """
    source = APP_JS.read_text(encoding="utf-8")
    match = re.search(
        r"function selectAllGames\(\)\s*\{(.*?)\n\}",
        source,
        re.S,
    )
    assert match is not None, "selectAllGames() not found in app.js"
    body = match.group(1)

    assert "Array.from(availableGames).sort()" not in body, (
        "selectAllGames() still replaces games_to_watch outright from "
        "availableGames instead of merging -- the exact data-loss bug"
    )
    # Must actually read the existing selection before assigning a new one.
    assert re.search(r"state\.settings\.games_to_watch\s*\|\|\s*\[\]", body), (
        "selectAllGames() doesn't appear to read the existing selection "
        "before building the merged list"
    )


if __name__ == "__main__":
    import unittest

    class _Run(unittest.TestCase):
        def test(self):
            test_select_all_games_merges_instead_of_replacing()

    unittest.main()
