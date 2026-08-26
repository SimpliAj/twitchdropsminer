"""
Static consistency checks for the "Blacklisted Games" settings UI (GitHub
issue #6 follow-up): lets users blacklist a game directly from Settings,
without first adding it to Games to Watch and removing it. It reuses the
existing auto_add_excluded_games field/mechanism that already prevented
"Auto-add linked games" from resurrecting a manually-removed game.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_JS = REPO_ROOT / "web" / "static" / "app.js"
INDEX_HTML = REPO_ROOT / "web" / "index.html"


def test_html_has_blacklisted_games_controls():
    html = INDEX_HTML.read_text(encoding="utf-8")
    for element_id in (
        "blacklisted-games-list",
        "blacklisted-game-input",
        "blacklisted-game-add-btn",
    ):
        assert f'id="{element_id}"' in html, f"missing #{element_id} in web/index.html"


def test_app_js_defines_render_and_add_functions():
    js = APP_JS.read_text(encoding="utf-8")
    assert "function renderBlacklistedGames(" in js
    assert "function addGameToBlacklist(" in js


def test_add_button_and_enter_key_are_wired():
    js = APP_JS.read_text(encoding="utf-8")
    assert "getElementById('blacklisted-game-add-btn')" in js
    assert "getElementById('blacklisted-game-input')" in js
    # Enter key in the input should trigger the same add flow as the button.
    enter_block = re.search(
        r"getElementById\('blacklisted-game-input'\)\?\.addEventListener\('keydown',[^}]*\}",
        js,
    )
    assert enter_block is not None
    assert "blacklisted-game-add-btn" in enter_block.group(0)


def test_games_to_watch_render_refreshes_blacklist_list():
    # Removing a game via the normal Games-to-Watch "x" button must keep the
    # Blacklisted Games panel in sync (it shares auto_add_excluded_games).
    js = APP_JS.read_text(encoding="utf-8")
    render_games_to_watch = re.search(
        r"function renderGamesToWatch\(\)\s*\{.*?\n\}", js, re.DOTALL
    )
    assert render_games_to_watch is not None
    assert "renderBlacklistedGames(" in render_games_to_watch.group(0)


def test_add_game_to_blacklist_reuses_remove_from_watch_when_currently_watched():
    # A game that's currently in Games to Watch must go through
    # removeGameFromWatch() (which already sets the exclusion flag, saves, and
    # re-renders) instead of duplicating that logic.
    js = APP_JS.read_text(encoding="utf-8")
    add_fn = re.search(
        r"function addGameToBlacklist\(gameName\)\s*\{.*?\n\}", js, re.DOTALL
    )
    assert add_fn is not None
    assert "removeGameFromWatch(name)" in add_fn.group(0)
    assert "setAutoAddExcluded(name, true)" in add_fn.group(0)
