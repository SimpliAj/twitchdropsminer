"""In-app updater for the packaged desktop builds (Windows .exe, macOS .app).

Downloads the release zip for this platform, verifies it against the release's SHA256SUMS file,
stages it next to the install and swaps it in. User data lives in DATA_DIR, outside the install,
and is never touched.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from src.config.paths import IS_FROZEN
from src.desktop import runtime
from src.services.update_announcer import fetch_latest_release


logger = logging.getLogger("TwitchDrops")

ALLOWED_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}
MAX_DOWNLOAD = 200 * 1024 * 1024
STAGING_NAME = ".TwitchDropsMiner-update"
BACKUP_SUFFIX = ".old"

# Test hook (CI end-to-end): a folder holding the zip and its SHA256SUMS file replaces GitHub.
LOCAL_SOURCE_ENV = "TDM_UPDATE_LOCAL_DIR"

# One update at a time; the dashboard polls this.
status: dict = {"state": "idle", "error": ""}


class UpdateError(Exception):
    pass


def platform_tag(system: str | None = None, machine: str | None = None) -> str:
    system = system or sys.platform
    machine = (machine or platform.machine()).lower()
    if system == "win32":
        return "windows-x64"
    if system == "darwin":
        return "macos-apple-silicon" if machine in ("arm64", "aarch64") else "macos-intel"
    raise UpdateError("In-app updates are only available for the Windows and macOS apps")


def asset_names(version: str, tag: str) -> tuple[str, str]:
    """(zip name, checksum file name) of a release for a platform tag."""
    return f"TwitchDropsMiner-{version}-{tag}.zip", f"SHA256SUMS-{tag.split('-')[0]}.txt"


def check_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise UpdateError("Refusing to download from an untrusted address")
    return url


def expected_sha256(sums_text: str, filename: str) -> str:
    for line in sums_text.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[1].strip().lstrip("*") == filename:
            return parts[0].lower()
    raise UpdateError(f"No checksum published for {filename}")


def verify_sha256(path: Path, expected: str) -> None:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected.lower():
        raise UpdateError("Checksum mismatch, the download was discarded")


def safe_members(archive: zipfile.ZipFile, dest: Path) -> list[str]:
    """Member names, refusing anything that would land outside ``dest`` (zip slip)."""
    root = dest.resolve()
    names = archive.namelist()
    for name in names:
        target = (root / name).resolve()
        if name.startswith(("/", "\\")) or not target.is_relative_to(root):
            raise UpdateError(f"Unsafe path in the update archive: {name}")
    return names


def extract(zip_path: Path, dest: Path) -> None:
    with zipfile.ZipFile(zip_path) as archive:
        safe_members(archive, dest)
        if sys.platform == "darwin":
            # ditto keeps the symlinks and executable bits a .app bundle needs; zipfile drops them
            subprocess.run(["/usr/bin/ditto", "-x", "-k", str(zip_path), str(dest)], check=True)  # noqa: S603
        else:
            archive.extractall(dest)  # noqa: S202  (names validated above)


def install_paths() -> tuple[Path, Path]:
    """(thing to replace, folder that holds it): the app folder on Windows, the .app on macOS."""
    exe = Path(sys.executable).resolve()
    if sys.platform == "darwin":
        bundle = exe.parents[2]  # <name>.app/Contents/MacOS/<exe>
        if bundle.suffix != ".app":
            raise UpdateError("Could not find the app bundle to update")
        return bundle, bundle.parent
    return exe.parent, exe.parent.parent


def powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def windows_script(pid: int, install: Path, new: Path, backup: Path, staging: Path, args: list[str]) -> str:
    exe = Path(sys.executable).name
    arg_list = ",".join(powershell_literal(a) for a in args) or "@()"
    return f"""$ErrorActionPreference = 'Stop'
Start-Transcript -Path (Join-Path $env:TEMP 'tdm-update.log') -Force | Out-Null
$install = {powershell_literal(str(install))}
$new = {powershell_literal(str(new))}
$backup = {powershell_literal(str(backup))}
$exe = Join-Path $install {powershell_literal(exe)}
$appArgs = @({arg_list})
function Start-App {{
  if ($appArgs.Count -gt 0) {{ Start-Process -FilePath $exe -ArgumentList $appArgs -WorkingDirectory $install -PassThru }}
  else {{ Start-Process -FilePath $exe -WorkingDirectory $install -PassThru }}
}}
function Restore {{
  if (Test-Path -LiteralPath $install) {{ Remove-Item -LiteralPath $install -Recurse -Force -ErrorAction SilentlyContinue }}
  Move-Item -LiteralPath $backup -Destination $install
  Start-App | Out-Null
}}
Wait-Process -Id {int(pid)} -Timeout 60 -ErrorAction SilentlyContinue
$moved = $false
foreach ($i in 1..30) {{
  try {{ Move-Item -LiteralPath $install -Destination $backup; $moved = $true; break }}
  catch {{ Start-Sleep -Seconds 1 }}
}}
if (-not $moved) {{ Start-App | Out-Null; exit 1 }}
try {{
  Move-Item -LiteralPath $new -Destination $install
  $proc = Start-App
  Start-Sleep -Seconds 10
  if ($proc.HasExited) {{ throw 'the new version exited right after starting' }}
}} catch {{
  Restore
  exit 1
}}
Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath {powershell_literal(str(staging))} -Recurse -Force -ErrorAction SilentlyContinue
"""


def _apply_windows(install: Path, new: Path, staging: Path) -> None:
    backup = install.with_name(install.name + BACKUP_SUFFIX)
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    handle, script = tempfile.mkstemp(suffix=".ps1", prefix="tdm-update-")
    with os.fdopen(handle, "w", encoding="utf-8-sig") as out:
        out.write(windows_script(os.getpid(), install, new, backup, staging, sys.argv[1:]))
    env = {**os.environ, runtime.RESTARTED_ENV: "1", "PYINSTALLER_RESET_ENVIRONMENT": "1"}
    subprocess.Popen(  # noqa: S603
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", script],  # noqa: S607
        env=env,
        creationflags=0x00000008 | 0x00000200 | 0x08000000,  # DETACHED | NEW_PROCESS_GROUP | NO_WINDOW
        close_fds=True,
    )
    runtime.quit_for_update()


def _apply_macos(bundle: Path, new: Path, staging: Path) -> None:
    backup = bundle.with_name(bundle.name + BACKUP_SUFFIX)
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    bundle.rename(backup)
    try:
        new.rename(bundle)
    except OSError:
        backup.rename(bundle)
        raise
    # The running process keeps its (renamed) files open; drop the backup once the new app is up.
    subprocess.Popen(  # noqa: S603
        ["/bin/sh", "-c", 'sleep 120; rm -rf "$1" "$2"', "sh", str(backup), str(staging)],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    runtime.restart_process()  # execs the (now new) bundle executable


async def _download(url: str, dest: Path, limit: int = MAX_DOWNLOAD) -> None:
    async with (
        aiohttp.ClientSession() as session,
        session.get(check_url(url), timeout=aiohttp.ClientTimeout(total=600)) as response,
    ):
        if response.status != 200:
            raise UpdateError(f"Download failed (HTTP {response.status})")
        check_url(str(response.url))  # redirects must stay on GitHub's hosts too
        if response.content_length and response.content_length > limit:
            raise UpdateError("The update file is larger than expected")
        size = 0
        with open(dest, "wb") as out:
            async for chunk in response.content.iter_chunked(1 << 20):
                size += len(chunk)
                if size > limit:
                    raise UpdateError("The update file is larger than expected")
                out.write(chunk)


async def _fetch_sources(staging: Path) -> tuple[Path, str]:
    """Download (or, in the CI test, copy) the zip; return it with its expected SHA-256."""
    tag = platform_tag()
    local = os.environ.get(LOCAL_SOURCE_ENV)
    if local:
        zip_src = next(Path(local).glob(f"TwitchDropsMiner-*-{tag}.zip"))
        sums_name = asset_names("", tag)[1]
        zip_path = staging / zip_src.name
        shutil.copyfile(zip_src, zip_path)
        return zip_path, expected_sha256((Path(local) / sums_name).read_text(), zip_src.name)
    release = await fetch_latest_release()
    if not release:
        raise UpdateError("Could not reach GitHub to look up the latest release")
    zip_name, sums_name = asset_names(release["version"], tag)
    assets = release["assets"]
    if zip_name not in assets or sums_name not in assets:
        raise UpdateError(f"The release has no {zip_name} with a checksum file")
    sums = staging / sums_name
    await _download(assets[sums_name], sums, 1 << 20)
    zip_path = staging / zip_name
    await _download(assets[zip_name], zip_path)
    return zip_path, expected_sha256(sums.read_text(), zip_name)


def ensure_ready() -> tuple[Path, Path]:
    """Checks that can fail fast, before the background task starts."""
    if not IS_FROZEN:
        raise UpdateError("In-app updates are only available for the Windows and macOS apps")
    if status["state"] in ("downloading", "installing"):
        raise UpdateError("An update is already running")
    platform_tag()
    target, parent = install_paths()
    if not os.access(parent, os.W_OK) or (sys.platform == "darwin" and not os.access(target, os.W_OK)):
        raise UpdateError(f"No permission to modify {parent}. Move the app to a folder you own, or update it by hand.")
    return target, parent


async def run_update(target: Path, parent: Path) -> None:
    staging = parent / STAGING_NAME
    try:
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir()
        status.update(state="downloading", error="")
        zip_path, expected = await _fetch_sources(staging)
        verify_sha256(zip_path, expected)
        status["state"] = "installing"
        unpacked = staging / "unpacked"
        await asyncio.to_thread(extract, zip_path, unpacked)
        new = unpacked / target.name if sys.platform == "darwin" else unpacked / "TwitchDropsMiner"
        if not new.is_dir():
            raise UpdateError("The update archive does not contain the app")
        # Blocking on purpose: both variants end the process (Windows) or replace it (macOS).
        (_apply_macos if sys.platform == "darwin" else _apply_windows)(target, new, staging)
    except Exception as exc:
        logger.warning(f"Update failed: {type(exc).__name__}: {exc}")
        status.update(state="error", error=str(exc) if isinstance(exc, UpdateError) else f"{type(exc).__name__}: {exc}")
        shutil.rmtree(staging, ignore_errors=True)
