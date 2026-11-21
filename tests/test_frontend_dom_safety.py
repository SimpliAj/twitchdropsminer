import re
from pathlib import Path


APP_JS = Path(__file__).resolve().parents[1] / "web" / "static" / "app.js"


def test_app_js_only_uses_innerhtml_to_clear_elements():
    app_source = APP_JS.read_text(encoding="utf-8")
    unsafe_assignments = []

    for match in re.finditer(r"\binnerHTML\s*=\s*([^;\n]+)", app_source):
        assigned_value = match.group(1).strip()
        if assigned_value not in {"''", '""'}:
            line_number = app_source.count("\n", 0, match.start()) + 1
            unsafe_assignments.append(f"line {line_number}: {match.group(0).strip()}")

    assert unsafe_assignments == []


INDEX_HTML = APP_JS.parents[1] / "index.html"


def test_channel_points_channel_names_link_to_twitch():
    app_source = APP_JS.read_text(encoding="utf-8")
    html_source = INDEX_HTML.read_text(encoding="utf-8")

    # Shared helper builds a safe, encoded, noopener link.
    assert "https://www.twitch.tv/' + encodeURIComponent(login)" in app_source
    assert "el.rel = 'noopener noreferrer'" in app_source

    # Dashboard panel, tracker list and Channel Points tab all use the helper.
    assert app_source.count("setChannelLink(") >= 4  # definition + 3 call sites
    assert '<a id="channel-points-channel"' in html_source
