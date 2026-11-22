#!/bin/bash
# CI: "Add parallel account" end to end on a copy of the built app.
set -euo pipefail
cd "$(dirname "$0")/.."
work="$RUNNER_TEMP/tdm-parallel-e2e"
rm -rf "$work"; mkdir -p "$work"
ditto dist/TwitchDropsMiner.app "$work/TwitchDropsMiner.app"
exe="$work/TwitchDropsMiner.app/Contents/MacOS/TwitchDropsMiner"

export TDM_DATA_DIR="$work/data" TDM_PORT=18082
healthy() { curl -fsS "http://127.0.0.1:$1/healthz" >/dev/null 2>&1; }
wait_healthy() { for i in $(seq 1 40); do sleep 2; healthy "$1" && return 0; done; echo "nothing answers on port $1"; return 1; }
stop_all() { pkill -f "$work/TwitchDropsMiner.app" || true; sleep 3; }

"$exe" --no-tray --no-browser &
wait_healthy 18082
# first-run password setup would otherwise redirect every API call
curl -sS -o /dev/null -d action=skip http://127.0.0.1:18082/__setup_post
resp=$(curl -fsS -X POST http://127.0.0.1:18082/api/instances/spawn)
echo "spawn: $resp"
port=$(echo "$resp" | python3 -c 'import json,sys; print([i for i in json.load(sys.stdin)["instances"] if i.get("spawned")][0]["port"])')
wait_healthy "$port"
curl -fsS http://127.0.0.1:18082/api/instances | python3 -c "import json,sys; assert any(i.get('spawned') and i['port']==$port for i in json.load(sys.stdin)['instances'])"
[ -d "$work/TwitchDropsMiner2" ] || { echo "child data dir missing"; exit 1; }
echo "child answers on port $port"

stop_all
healthy 18082 && { echo "parent still running"; exit 1; }
"$exe" --no-tray --no-browser &
wait_healthy 18082
wait_healthy "$port"
echo "child came back with the app"
stop_all
echo "Parallel accounts end-to-end OK"
