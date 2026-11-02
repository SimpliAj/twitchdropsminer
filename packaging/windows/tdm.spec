# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Windows desktop build.
#
#   pyinstaller --noconfirm --clean packaging/windows/tdm.spec
#
# Produces dist/TwitchDropsMiner/TwitchDropsMiner.exe (one-folder build: starts faster than
# one-file, nothing is unpacked into %TEMP% on every launch, and it triggers fewer antivirus
# false positives).
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent.parent

hiddenimports = (
    # src/__main__.py is launched through runpy, which PyInstaller cannot see statically.
    collect_submodules("src")
    + collect_submodules("uvicorn")
    + collect_submodules("engineio")
    + collect_submodules("socketio")
    + ["engineio.async_drivers.asgi", "pystray._win32"]
)

datas = [
    (str(ROOT / "web"), "web"),
    (str(ROOT / "lang"), "lang"),
    (str(ROOT / "scripts" / "tdm_login_helper.py"), "scripts"),
] + collect_data_files("tzdata")

a = Analysis(
    [str(ROOT / "packaging" / "windows" / "entry.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hiddenimports,
    # Playwright ships a ~100 MB Node driver and is only a fallback for finding a browser;
    # every Windows has Edge, so it is left out (see src/auth/browser_login.py).
    excludes=["playwright", "tkinter", "pytest", "coverage", "mypy", "ruff"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TwitchDropsMiner",
    console=False,  # tray app: no console window (logs go to %APPDATA%\TwitchDropsMiner\logs)
    icon=str(ROOT / "web" / "favicon.ico"),
    upx=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="TwitchDropsMiner",
    upx=False,
)
