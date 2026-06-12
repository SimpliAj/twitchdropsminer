import json
import pathlib

lang_dir = pathlib.Path("lang")

new_settings_keys = {
    "notifications_header": "Notifications",
    "push_enabled": "Browser Push Notifications",
    "push_sound": "Notification Sound",
    "campaign_end_alerts_enabled": "Campaign End Alerts (24h warning)"
}

# All keys that must exist in every lang file (previously missing from TypedDict but present in English)
required_settings_keys = {
    "select_linked": "Select Linked",
    "password_header": "Login & Password",
    "password_current_label": "Current Password",
    "password_current_placeholder": "Leave empty if no password set",
    "password_new_label": "New Password",
    "password_new_placeholder": "New password (empty = disable login)",
    "password_confirm_label": "Confirm New Password",
    "password_confirm_placeholder": "Confirm",
    "password_save": "Save Password",
    "password_disable": "Disable Login",
    "password_saved": "Password saved.",
    "password_disabled_msg": "Login disabled.",
    "password_mismatch": "Passwords don't match.",
    "password_status_active": "\U0001f512 Login active — Password set",
    "password_status_inactive": "\U0001f513 No login — publicly accessible",
    "discord_bot": {
        "header": "Discord Bot",
        "description": "Pair a Discord bot to control TwitchDropsMiner via chat commands.",
        "not_connected": "Not connected",
        "connected": "✅ Connected",
        "generate_code": "Generate code",
        "generating": "Generating...",
        "disconnect": "Disconnect",
        "disconnect_confirm": "Disconnect the Discord bot?",
        "invite_bot": "Invite bot",
        "expires": "Expires in 10 min"
    },
}
required_settings_keys.update(new_settings_keys)

for filepath in sorted(lang_dir.glob("*.json")):
    if filepath.name == "English.json":
        continue
    data = json.loads(filepath.read_text(encoding="utf-8"))
    changed = False
    # Add new/missing settings keys if missing
    for key, value in required_settings_keys.items():
        if key not in data["gui"]["settings"]:
            data["gui"]["settings"][key] = value
            changed = True
    # Add stats tab if missing
    if "stats" not in data["gui"]["tabs"]:
        data["gui"]["tabs"]["stats"] = "Stats"
        changed = True
    # Add system tab if missing
    if "system" not in data["gui"]["tabs"]:
        data["gui"]["tabs"]["system"] = "System"
        changed = True
    if changed:
        filepath.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Updated {filepath.name}")
    else:
        print(f"No changes needed: {filepath.name}")
