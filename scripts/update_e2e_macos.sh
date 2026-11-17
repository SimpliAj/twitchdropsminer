#!/bin/bash
# CI: run the in-app updater end to end on a copy of the built app (zip from dist/, no network).
set -euo pipefail
cd "$(dirname "$0")/.."
work="$RUNNER_TEMP/tdm-update-e2e"
rm -rf "$work"; mkdir -p "$work/source" "$work/apps"
zip=$(ls dist/TwitchDropsMiner-*-macos-*.zip | head -1)
cp "$zip" "$work/source/"
(cd "$work/source" && shasum -a 256 "$(basename "$zip")" > SHA256SUMS-macos.txt)
ditto dist/TwitchDropsMiner.app "$work/apps/TwitchDropsMiner.app"
echo old > "$work/apps/TwitchDropsMiner.app/Contents/OLD_MARKER"

export TDM_DATA_DIR="$work/data" TDM_PORT=18081 TDM_UPDATE_LOCAL_DIR="$work/source"
"$work/apps/TwitchDropsMiner.app/Contents/MacOS/TwitchDropsMiner" --no-tray --no-browser &
healthy() { curl -fsS http://127.0.0.1:18081/healthz >/dev/null 2>&1; }
for i in $(seq 1 30); do sleep 2; healthy && break; done
healthy || { echo "copy of the app did not start"; exit 1; }

curl -fsS -X POST http://127.0.0.1:18081/api/self-update; echo
ok=0
for i in $(seq 1 60); do
  sleep 3
  if [ ! -e "$work/apps/TwitchDropsMiner.app/Contents/OLD_MARKER" ] && healthy; then ok=1; break; fi
done
sleep 8
healthy || ok=0
curl -fsS http://127.0.0.1:18081/api/update/status || true; echo
pkill -f "$work/apps/TwitchDropsMiner.app" || true
[ "$ok" = 1 ] || { echo "the app was not replaced and restarted"; exit 1; }
[ -d "$work/apps/TwitchDropsMiner.app.old" ] || echo "note: backup already gone"
[ -d "$work/data" ] || { echo "user data dir vanished"; exit 1; }
echo "Update end-to-end OK"
