# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the macOS app.
#
#   pyinstaller --noconfirm --clean packaging/macos/tdm.spec
#
# Produces dist/TwitchDropsMiner.app (one-folder build inside an .app bundle). It is a background
# app (LSUIElement): no Dock icon and no menu bar, the dashboard opens in the default browser.
import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent.parent
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "src" / "version.py").read_text()).group(1)
ICON = ROOT / "build" / "macos" / "TwitchDropsMiner.icns"

hiddenimports = (
    collect_submodules("src")
    + collect_submodules("uvicorn")
    + collect_submodules("engineio")
    + collect_submodules("socketio")
    + ["engineio.async_drivers.asgi"]
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
    excludes=["playwright", "tkinter", "pytest", "coverage", "mypy", "ruff", "pystray", "PIL"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TwitchDropsMiner",
    console=False,
    upx=False,
)

coll = COLLECT(exe, a.binaries, a.datas, name="TwitchDropsMiner", upx=False)

app = BUNDLE(
    coll,
    name="TwitchDropsMiner.app",
    icon=str(ICON) if ICON.exists() else None,
    bundle_identifier="io.github.simpliaj.twitchdropsminer",
    version=VERSION,
    info_plist={
        "CFBundleName": "Twitch Drops Miner",
        "CFBundleDisplayName": "Twitch Drops Miner",
        "CFBundleShortVersionString": VERSION,
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
    },
)
