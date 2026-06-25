#!/bin/bash
# TwitchDropsMiner Update Script
# Applies upstream updates while preserving all custom modifications.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== TwitchDropsMiner Update ==="
echo ""

# Check for upstream changes
git fetch origin main --quiet
UPSTREAM=$(git rev-parse origin/main)
LOCAL=$(git rev-parse HEAD)

if [ "$UPSTREAM" = "$LOCAL" ]; then
    echo "Already up to date. No update needed."
    exit 0
fi

echo "Update available:"
git log HEAD..origin/main --oneline
echo ""

# Backup extra files/dirs not tracked by git
EXTRAS=(
    "web/logo.png"
    "web/favicon.ico"
    "web/favicon.png"
    "web/manifest.json"
    "data/web_config.json"
    "data/accounts"
)

BACKUP_DIR="/tmp/tdm_update_backup_$(date +%s)"
mkdir -p "$BACKUP_DIR"

for f in "${EXTRAS[@]}"; do
    if [ -e "$f" ]; then
        mkdir -p "$BACKUP_DIR/$(dirname $f)"
        cp -r "$f" "$BACKUP_DIR/$f"
    fi
done

echo "Stashing custom modifications..."
git stash push -m "custom-mods-$(date +%Y%m%d-%H%M%S)"

echo "Pulling upstream changes..."
git pull origin main

echo ""
echo "Reapplying custom modifications..."
if git stash pop; then
    echo "✓ All custom changes applied cleanly."
else
    echo ""
    echo "⚠ Merge conflicts detected. Attempting auto-resolution..."
    # Accept ours for conflicted files (our changes take priority for UI/logic)
    git checkout --ours -- web/static/app.js web/index.html web/static/styles.css src/web/app.py src/config/paths.py src/core/client.py 2>/dev/null || true
    git add -A
    echo "✓ Conflicts resolved (kept our modifications)."
fi

# Restore untracked extra files/dirs
echo "Restoring custom files..."
for f in "${EXTRAS[@]}"; do
    if [ -e "$BACKUP_DIR/$f" ]; then
        mkdir -p "$(dirname $f)"
        cp -r "$BACKUP_DIR/$f" "$f"
    fi
done
rm -rf "$BACKUP_DIR"

# Install any new Python dependencies
echo "Checking dependencies..."
venv/bin/pip install -r requirements.txt --quiet 2>/dev/null || true

# Re-apply language keys (safe merge — only adds, never overwrites)
echo "Syncing language files..."
venv/bin/python3 - << 'PYEOF'
import json
from pathlib import Path

LANG_DIR = Path("lang")

NEW_KEYS = {
    ("gui", "tabs", "system"): "System",
    ("gui", "settings", "select_linked"): "Select Linked",
    ("gui", "settings", "password_header"): "Login & Password",
    ("gui", "settings", "password_current_label"): "Current Password",
    ("gui", "settings", "password_current_placeholder"): "Leave empty if no password set",
    ("gui", "settings", "password_new_label"): "New Password",
    ("gui", "settings", "password_new_placeholder"): "New password (empty = disable login)",
    ("gui", "settings", "password_confirm_label"): "Confirm New Password",
    ("gui", "settings", "password_confirm_placeholder"): "Confirm",
    ("gui", "settings", "password_save"): "Save Password",
    ("gui", "settings", "password_disable"): "Disable Login",
    ("gui", "settings", "password_saved"): "Password saved.",
    ("gui", "settings", "password_disabled_msg"): "Login disabled.",
    ("gui", "settings", "password_mismatch"): "Passwords don't match.",
    ("gui", "settings", "password_status_active"): "🔒 Login active — Password set",
    ("gui", "settings", "password_status_inactive"): "🔓 No login — publicly accessible",
    ("gui", "system", "header"): "System",
    ("gui", "system", "miner_header"): "Miner",
    ("gui", "system", "miner_desc"): "Reload campaigns and restart the mining loop.",
    ("gui", "system", "reload_btn"): "Reload Campaigns",
    ("gui", "system", "restart_header"): "Restart",
    ("gui", "system", "restart_desc"): "Stop the miner process. PM2 will restart it automatically.",
    ("gui", "system", "restart_btn"): "Restart Miner",
    ("gui", "system", "session_header"): "Session",
    ("gui", "system", "session_desc"): "Log out of the web interface.",
    ("gui", "system", "logout_btn"): "Logout",
    ("gui", "system", "reload_ok"): "Campaigns reload triggered.",
    ("gui", "system", "restart_confirm"): "Restart the miner? PM2 will restart it automatically.",
    ("gui", "system", "restart_ok"): "Miner restarting via PM2...",
    ("gui", "inventory", "filter_linked"): "Linked",
}

def set_nested(d, path, value):
    for key in path[:-1]:
        d = d.setdefault(key, {})
    if path[-1] not in d:
        d[path[-1]] = value

updated = 0
for f in sorted(LANG_DIR.glob("*.json")):
    data = json.loads(f.read_text(encoding="utf-8"))
    before = json.dumps(data)
    for path, val in NEW_KEYS.items():
        set_nested(data, path, val)
    after = json.dumps(data)
    if before != after:
        f.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")
        updated += 1

print(f"  Language files updated: {updated}")
PYEOF

# Update cache-bust hashes in index.html (app.js + styles.css)
if command -v md5sum &>/dev/null; then
    JS_HASH=$(md5sum web/static/app.js | cut -c1-8)
    CSS_HASH=$(md5sum web/static/styles.css | cut -c1-8)
elif command -v md5 &>/dev/null; then
    JS_HASH=$(md5 -q web/static/app.js | cut -c1-8)
    CSS_HASH=$(md5 -q web/static/styles.css | cut -c1-8)
fi
for html in web/index.html src/web/index.html; do
    if [ -f "$html" ]; then
        [ -n "$JS_HASH" ]  && sed -i "s|app.js?v=[^\"]*|app.js?v=$JS_HASH|g" "$html"
        [ -n "$CSS_HASH" ] && sed -i "s|styles.css?v=[^\"]*|styles.css?v=$CSS_HASH|g" "$html"
    fi
done
echo "  Cache hashes updated: js=$JS_HASH css=$CSS_HASH"

# Restart via PM2
echo ""
echo "Restarting miners..."
pm2 restart twitchdrops twitchdrops2 2>/dev/null || pm2 restart twitchdrops 2>/dev/null || true

echo ""
echo "=== Update complete! ==="
pm2 status | grep twitchdrops
