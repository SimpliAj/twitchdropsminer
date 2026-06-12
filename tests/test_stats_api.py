import json
import pathlib
import pytest


def _make_history(tmp_path):
    data = [
        {"timestamp": "2026-06-10T10:00:00+00:00", "game": "R6", "drop": "Pack", "reward": "Pack 1", "image_url": "https://example.com/img.jpg"},
        {"timestamp": "2026-06-10T12:00:00+00:00", "game": "R6", "drop": "Pack", "reward": "Pack 2"},
        {"timestamp": "2026-06-11T08:00:00+00:00", "game": "Rust", "drop": "Skin", "reward": "Skin 1"},
        {"timestamp": "2026-06-11T09:00:00+00:00", "game": "R6", "drop": "Pack", "reward": "Pack 3"},
    ]
    f = tmp_path / "drops_history.json"
    f.write_text(json.dumps(data))
    return f


def test_aggregate_stats(tmp_path, monkeypatch):
    hist_file = _make_history(tmp_path)
    import src.web.app as app_module
    monkeypatch.setattr(app_module, "_DATA_DIR", tmp_path)

    result = app_module._aggregate_stats()
    assert result["total_claims"] == 4
    assert result["by_game"][0]["game"] == "R6"
    assert result["by_game"][0]["count"] == 3
    assert result["by_game"][1]["game"] == "Rust"
    assert result["by_game"][1]["count"] == 1
    assert len(result["recent"]) <= 10
    assert result["recent"][0]["image_url"] == "https://example.com/img.jpg"


def test_aggregate_stats_empty(tmp_path, monkeypatch):
    import src.web.app as app_module
    monkeypatch.setattr(app_module, "_DATA_DIR", tmp_path)
    result = app_module._aggregate_stats()
    assert result["total_claims"] == 0
    assert result["by_game"] == []
    assert result["by_day"] == []
    assert result["recent"] == []
