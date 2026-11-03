from __future__ import annotations

import io
import json
import shutil
import stat
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

MANIFEST = "tdm-backup.json"
PAIRINGS = "discord_bot/pairings.json"
EXCLUDED_DIRS = {"logs", "dashboard-window"}
EXCLUDED_PREFIXES = ("import-backup-",)
EXCLUDED_SUFFIXES = (".lock", ".tmp", ".log")
MAX_UPLOAD = 50 * 1024 * 1024
MAX_UNCOMPRESSED = 200 * 1024 * 1024


class BackupError(ValueError):
    pass


def _excluded(parts: tuple[str, ...]) -> bool:
    if parts[0] in EXCLUDED_DIRS or parts[0].startswith(EXCLUDED_PREFIXES):
        return True
    return parts[-1].endswith(EXCLUDED_SUFFIXES)


def build_export(data_dir: Path, pairings_file: Path, version: str) -> bytes:
    buf = io.BytesIO()
    manifest = {
        "format": 1,
        "app_version": version,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(MANIFEST, json.dumps(manifest, indent=2))
        if data_dir.is_dir():
            for path in sorted(data_dir.rglob("*")):
                if path.is_symlink() or not path.is_file():
                    continue
                rel = path.relative_to(data_dir)
                if not _excluded(rel.parts):
                    zf.write(path, rel.as_posix())
        if pairings_file.is_file():
            zf.write(pairings_file, PAIRINGS)
    return buf.getvalue()


def _safe_parts(name: str) -> tuple[str, ...]:
    if "\\" in name or name.startswith("/"):
        raise BackupError(f"Unsafe path in backup: {name}")
    parts = PurePosixPath(name).parts
    if not parts or any(p in ("..", ".", "") or ":" in p for p in parts):
        raise BackupError(f"Unsafe path in backup: {name}")
    return parts


def restore_import(raw: bytes, data_dir: Path, pairings_file: Path) -> Path | None:
    if len(raw) > MAX_UPLOAD:
        raise BackupError("Backup file is too large")
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as e:
        raise BackupError("Not a valid zip file") from e
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if sum(i.file_size for i in infos) > MAX_UNCOMPRESSED:
            raise BackupError("Backup contents are too large")
        try:
            manifest = json.loads(zf.read(MANIFEST))
        except (KeyError, ValueError) as e:
            raise BackupError("Not a TDM backup (tdm-backup.json missing)") from e
        if not isinstance(manifest, dict) or manifest.get("format") != 1:
            raise BackupError("Unsupported backup format")

        entries: list[tuple[zipfile.ZipInfo, Path]] = []
        for info in infos:
            parts = _safe_parts(info.filename)
            if stat.S_ISLNK(info.external_attr >> 16):
                raise BackupError(f"Unsafe path in backup: {info.filename}")
            if parts == (MANIFEST,):
                continue
            if "/".join(parts) == PAIRINGS:
                entries.append((info, pairings_file))
            elif not _excluded(parts):
                entries.append((info, data_dir.joinpath(*parts)))
        if not entries:
            raise BackupError("Backup contains no data")

        backup_dir = data_dir / f"import-backup-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        for _, target in entries:
            if target.is_file():
                rel = target.relative_to(data_dir) if target.is_relative_to(data_dir) else Path(PAIRINGS)
                dest = backup_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), dest)
        for info, target in entries:
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    return backup_dir if backup_dir.exists() else None
