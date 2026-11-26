import json
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from src.services import drop_history
from src.web.app import SettingsUpdate


def _save(tmp_path):
    with patch.object(drop_history, "_account_data_dir", return_value=tmp_path):
        return drop_history.save_drop_claim("Game", "Drop", "Reward", None)


def test_damaged_history_is_kept_aside_not_overwritten(tmp_path):
    (tmp_path / "drops_history.json").write_text('[{"game": "old"}, {"ga')  # cut off mid-write
    assert _save(tmp_path)
    assert (tmp_path / "drops_history.json.bad").read_text().startswith('[{"game": "old"}')
    assert [e["game"] for e in json.loads((tmp_path / "drops_history.json").read_text())] == ["Game"]
    (tmp_path / "drops_history.json").write_text("{}")
    assert _save(tmp_path)
    assert (tmp_path / "drops_history.json.bad.1").exists()


def test_history_with_bom_loads_and_keeps_entries(tmp_path):
    (tmp_path / "drops_history.json").write_text('﻿[{"game": "old"}]', encoding="utf-8")
    assert _save(tmp_path)
    assert [e["game"] for e in json.loads((tmp_path / "drops_history.json").read_text())] == ["Game", "old"]
    assert not (tmp_path / "drops_history.json.tmp").exists()


def test_unreadable_history_is_not_saved_over(tmp_path):
    (tmp_path / "drops_history.json").write_text('[{"game": "old"}]')
    with patch.object(type(tmp_path), "read_text", side_effect=OSError("busy")):
        assert not _save(tmp_path)
    assert json.loads((tmp_path / "drops_history.json").read_text()) == [{"game": "old"}]


def test_api_rejects_out_of_range_refresh():
    with pytest.raises(ValidationError):
        SettingsUpdate(minimum_refresh_interval_minutes=0)
    with pytest.raises(ValidationError):
        SettingsUpdate(connection_quality=7)
    assert SettingsUpdate(minimum_refresh_interval_minutes=30).minimum_refresh_interval_minutes == 30


def test_settings_load_clamps_hand_edited_values(tmp_path):
    from src.config import settings as settings_mod

    with patch.object(settings_mod, "json_load", return_value={
        **settings_mod.default_settings, "minimum_refresh_interval_minutes": 0, "connection_quality": "x",
    }):
        s = settings_mod.Settings.__new__(settings_mod.Settings)
        s.load()
    assert s.minimum_refresh_interval_minutes == 1
    assert s.connection_quality == settings_mod.default_settings["connection_quality"]
