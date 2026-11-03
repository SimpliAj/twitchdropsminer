import io
import json
import zipfile

import pytest

from src.services import data_backup as db


def _setup(root):
    data = root / "data"
    (data / "accounts" / "a").mkdir(parents=True)
    (data / "accounts" / "a" / "cookies.jar").write_text("cookie")
    (data / "web_config.json").write_text('{"x": 1}')
    (data / "logs").mkdir()
    (data / "logs" / "TDM.log").write_text("log")
    (data / "dashboard-window").mkdir()
    (data / "dashboard-window" / "p").write_text("chromium")
    (data / "import-backup-1").mkdir()
    (data / "import-backup-1" / "old").write_text("old")
    pair = root / "state" / "discord_bot" / "pairings.json"
    pair.parent.mkdir(parents=True)
    pair.write_text("{}")
    return data, pair


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


MAN = json.dumps({"format": 1})


def test_roundtrip(tmp_path):
    data, pair = _setup(tmp_path / "src")
    raw = db.build_export(data, pair, "1.2.3")
    names = zipfile.ZipFile(io.BytesIO(raw)).namelist()
    assert "tdm-backup.json" in names and "discord_bot/pairings.json" in names
    assert not any(n.startswith(("logs", "dashboard-window", "import-backup-")) for n in names)

    dst = tmp_path / "dst" / "data"
    dst.mkdir(parents=True)
    (dst / "web_config.json").write_text("old")
    dpair = tmp_path / "dst" / "discord_bot" / "pairings.json"
    backup = db.restore_import(raw, dst, dpair)
    assert (dst / "accounts" / "a" / "cookies.jar").read_text() == "cookie"
    assert (dst / "web_config.json").read_text() == '{"x": 1}'
    assert dpair.read_text() == "{}"
    assert (backup / "web_config.json").read_text() == "old"


@pytest.mark.parametrize("name", ["../evil", "/abs", "C:/x", "a/../../b", "a\\b"])
def test_zip_slip_rejected(tmp_path, name):
    with pytest.raises(db.BackupError):
        db.restore_import(_zip({"tdm-backup.json": MAN, name: "x"}), tmp_path / "d", tmp_path / "p.json")
    assert not (tmp_path / "evil").exists()


def test_missing_manifest_rejected(tmp_path):
    with pytest.raises(db.BackupError):
        db.restore_import(_zip({"web_config.json": "{}"}), tmp_path / "d", tmp_path / "p.json")


def test_excluded_dirs_skipped(tmp_path):
    raw = _zip({"tdm-backup.json": MAN, "web_config.json": "{}", "logs/TDM.log": "x", "dashboard-window/a": "x"})
    d = tmp_path / "d"
    db.restore_import(raw, d, tmp_path / "p.json")
    assert (d / "web_config.json").exists()
    assert not (d / "logs").exists() and not (d / "dashboard-window").exists()
