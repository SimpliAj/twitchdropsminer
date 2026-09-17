import re
from pathlib import Path


APP_JS = Path(__file__).resolve().parents[1] / "web" / "static" / "app.js"


def test_change_game_priority_repositions_within_bounds():
    """
    Regression test (ported from upstream rangermix/TwitchDropsMiner #94):
    changeGamePriority(gameName, newIndex) must remove the game from its
    current slot and reinsert it at a clamped newIndex, then persist.
    """
    source = APP_JS.read_text(encoding="utf-8")
    match = re.search(
        r"function changeGamePriority\(gameName, newIndex\)\s*\{(.*?)\n\}",
        source,
        re.S,
    )
    assert match is not None, "changeGamePriority() not found in app.js"
    body = match.group(1)

    assert "games.splice(currentIndex, 1)" in body
    assert "games.splice(newIndex, 0, gameName)" in body
    assert "Math.max(0, Math.min(newIndex, games.length))" in body
    assert "saveSettings();" in body


def test_priority_input_wired_to_change_priority():
    source = APP_JS.read_text(encoding="utf-8")
    render_match = re.search(
        r"function renderSelectedGames\(games\)\s*\{(.*?)\n\}\n",
        source,
        re.S,
    )
    assert render_match is not None, "renderSelectedGames() not found in app.js"
    body = render_match.group(1)

    assert "class: 'priority-input'" in body
    assert "changeGamePriority(game, priority - 1)" in body


if __name__ == "__main__":
    import unittest

    class _Run(unittest.TestCase):
        def test_a(self):
            test_change_game_priority_repositions_within_bounds()

        def test_b(self):
            test_priority_input_wired_to_change_priority()

    unittest.main()
