"""
Static consistency check for GitHub issue #10 ("Language selection does not
persist on reload"): on a fresh page load, the 'initial_state' socket event
(settings, wanted items, channels, campaigns) arrives and triggers renders
(renderGamesToWatch, renderChannels, renderInventory, renderWantedItems)
before the separate async GET /api/translations fetch resolves. Those
render functions read state.translations at render time and silently fall
back to hardcoded English text when it's still {} - and since
applyTranslations() never re-ran them, they stayed stuck on English until an
unrelated event (e.g. changing the language again, which re-triggers
updateSettingsUI via the settings_updated broadcast) happened to re-render
them.

Fix: fetchAndApplyTranslations() now re-invokes those render functions
(using the state each of them already caches) once translations actually
arrive, so a plain page reload picks up the persisted language everywhere,
not just in the elements applyTranslations() directly touches.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_JS = REPO_ROOT / "web" / "static" / "app.js"


def _fetch_and_apply_translations_body() -> str:
    js = APP_JS.read_text(encoding="utf-8")
    start = js.index("async function fetchAndApplyTranslations(")
    end = js.index("\nfunction applyTranslations(", start)
    return js[start:end]


def test_refreshes_dynamic_lists_that_render_with_translations():
    body = _fetch_and_apply_translations_body()
    assert "applyTranslations(data)" in body
    for call in (
        "renderGamesToWatch()",
        "renderChannels()",
        "renderInventory()",
        "renderWantedItems(_wantedTree)",
    ):
        assert call in body, f"fetchAndApplyTranslations() no longer refreshes {call}"


if __name__ == "__main__":
    import unittest

    unittest.main()
