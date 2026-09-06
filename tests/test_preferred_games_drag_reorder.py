"""
Static consistency checks for GitHub issue #9: the Preferred Games list
showed a grab cursor (via the shared .sortable-item CSS) but had no actual
drag-and-drop reorder wiring - renderPreferredGames() never made its items
draggable or attached any of the drag handlers renderSelectedGames() uses.

Fix (contributed by PatrikDrex in the issue thread, tested against a live
instance): make each item draggable, tag it with dataset.game so a drop can
recover the new order, reuse the existing generic dragstart/dragover/drop
handlers, and add a dedicated dragend handler (renderPreferredGames operates
on settings.preferred_games / #preferred-games-list, unlike the shared
handleDragEnd which is hardcoded to #selected-games-list / games_to_watch).
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_JS = REPO_ROOT / "web" / "static" / "app.js"


def _render_preferred_games_body() -> str:
    js = APP_JS.read_text(encoding="utf-8")
    start = js.index("function renderPreferredGames(")
    end = js.index("\nfunction ", start + 1)
    return js[start:end]


def test_preferred_games_items_are_draggable_and_tagged():
    body = _render_preferred_games_body()
    assert "item.draggable = true" in body
    assert "item.dataset.game" in body


def test_preferred_games_reuses_shared_drag_handlers():
    body = _render_preferred_games_body()
    assert "item.addEventListener('dragstart', handleDragStart)" in body
    assert "item.addEventListener('dragover', handleDragOver)" in body
    assert "item.addEventListener('drop', handleDrop)" in body


def test_preferred_games_has_its_own_dragend_handler():
    js = APP_JS.read_text(encoding="utf-8")
    # Must NOT reuse the shared handleDragEnd, which is hardcoded to
    # #selected-games-list / state.settings.games_to_watch.
    body = _render_preferred_games_body()
    assert "handlePreferredGamesDragEnd" in body
    assert "handleDragEnd)" not in body  # would be the wrong (games_to_watch) handler

    assert "function handlePreferredGamesDragEnd(" in js
    handler_start = js.index("function handlePreferredGamesDragEnd(")
    handler_end = js.index("\n}", handler_start) + 2
    handler_body = js[handler_start:handler_end]
    assert "preferred-games-list" in handler_body
    assert "state.settings.preferred_games" in handler_body
    assert "saveSettings()" in handler_body


if __name__ == "__main__":
    import unittest

    unittest.main()
