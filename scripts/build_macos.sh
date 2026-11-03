#!/usr/bin/env bash
# Builds the macOS app: dist/TwitchDropsMiner.app (ad-hoc signed) and
# dist/TwitchDropsMiner-<version>-macos-<arm64|x64>.zip. Run on a Mac, from anywhere.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ ! -d .build-venv ]; then
  python3 -m venv .build-venv
fi
.build-venv/bin/python -m pip install --upgrade pip
.build-venv/bin/python -m pip install -r packaging/macos/requirements.txt

# Icon: web/logo.png -> .icns (best effort; the app builds without it)
rm -rf build/macos && mkdir -p build/macos/icon.iconset
for size in 16 32 128 256 512; do
  sips -z $size $size web/logo.png --out "build/macos/icon.iconset/icon_${size}x${size}.png" >/dev/null
  sips -z $((size * 2)) $((size * 2)) web/logo.png --out "build/macos/icon.iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns build/macos/icon.iconset -o build/macos/TwitchDropsMiner.icns || echo "iconutil failed; building without an icon"

rm -rf dist/TwitchDropsMiner dist/TwitchDropsMiner.app
.build-venv/bin/python -m PyInstaller --noconfirm --clean packaging/macos/tdm.spec

codesign --force --deep --sign - dist/TwitchDropsMiner.app
codesign --verify --deep --strict dist/TwitchDropsMiner.app

version=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/version.py)
case "$(uname -m)" in arm64) arch=arm64 ;; *) arch=x64 ;; esac
zip="dist/TwitchDropsMiner-${version}-macos-${arch}.zip"
rm -f "$zip"
ditto -c -k --keepParent dist/TwitchDropsMiner.app "$zip"
echo "Built $zip"
