import re
from pathlib import Path


APP_JS = Path(__file__).resolve().parents[1] / "web" / "static" / "app.js"
SETTINGS_PY = Path(__file__).resolve().parents[1] / "src" / "config" / "settings.py"
SETTINGS_MANAGER_PY = (
    Path(__file__).resolve().parents[1] / "src" / "web" / "managers" / "settings.py"
)


def _function_source(name: str) -> str:
    source = APP_JS.read_text(encoding="utf-8")
    match = re.search(
        r"function " + re.escape(name) + r"\(.*?\n\}", source, re.DOTALL
    )
    assert match, f"{name}() not found in web/static/app.js"
    return match.group(0)


def test_auto_add_linked_games_skips_excluded_games():
    # Without this, autoAddLinkedGames() re-adds every linked game on each
    # poll — a game manually removed from the queue comes right back unless
    # the whole Twitch/Epic account gets unlinked (killing every other
    # linked game too, e.g. all Epic Games titles at once for Fortnite).
    source = _function_source("autoAddLinkedGames")
    assert "auto_add_excluded_games" in source
    assert "excluded.has(c.game_name)" in source


def test_manual_removal_marks_game_excluded():
    toggle_source = _function_source("toggleGameWatch")
    remove_source = _function_source("removeGameFromWatch")
    assert "setAutoAddExcluded(gameName, true)" in toggle_source
    assert "setAutoAddExcluded(gameName, true)" in remove_source


def test_manual_re_add_paths_clear_exclusion():
    # Re-adding a game (checkbox, search, or the bulk "select linked/badge"
    # helpers) should let it be auto-tracked again going forward.
    for fn in (
        "toggleGameWatch",
        "addGameFromSearch",
        "selectLinkedGames",
        "selectBadgeEmoteGames",
        "selectAllGames",
    ):
        source = _function_source(fn)
        assert "setAutoAddExcluded" in source or "auto_add_excluded_games = []" in source, fn


def test_excluded_games_setting_has_default():
    source = SETTINGS_PY.read_text(encoding="utf-8")
    assert '"auto_add_excluded_games": []' in source
    assert "auto_add_excluded_games: list[str]" in source


def test_excluded_games_setting_is_persisted_by_backend():
    source = SETTINGS_MANAGER_PY.read_text(encoding="utf-8")
    assert '"auto_add_excluded_games", settings_data.get("auto_add_excluded_games")' in source
