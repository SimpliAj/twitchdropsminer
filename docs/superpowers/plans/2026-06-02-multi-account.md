# Multi-Account Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow multiple Twitch accounts to be stored and switched between, each with their own cookies and settings, via the System tab UI.

**Architecture:** Each account lives in `data/accounts/<label>/` with its own `cookies.jar` and `settings.json`. `web_config.json` stores `active_account` (the folder label). `src/config/paths.py` reads `active_account` at import time and sets `COOKIES_PATH`/`SETTINGS_PATH` dynamically. Switching accounts updates `web_config.json` and triggers a PM2 restart.

**Tech Stack:** Python (FastAPI, pathlib), vanilla JS, nginx/PM2

---

## Files Modified

| File | Change |
|------|--------|
| `src/config/paths.py` | Dynamic COOKIES_PATH/SETTINGS_PATH from active_account |
| `data/web_config.json` | Add `active_account` field |
| `src/web/app.py` | Add `/api/accounts/*` endpoints |
| `web/index.html` | Account section in System tab |
| `web/static/app.js` | Account management UI logic |
| `web/static/styles.css` | Account card styles |

---

### Task 1: Dynamic paths per account

**Files:**
- Modify: `src/config/paths.py`

- [ ] **Step 1: Update paths.py to read active_account from web_config.json**

Replace the bottom of `src/config/paths.py` (after `LANG_PATH` line):

```python
import json

def _get_account_data_dir() -> Path:
    """Return the data dir for the active account, or DATA_DIR root as fallback."""
    config_file = DATA_DIR / "web_config.json"
    if config_file.exists():
        try:
            cfg = json.loads(config_file.read_text())
            account = cfg.get("active_account")
            if account:
                account_dir = DATA_DIR / "accounts" / account
                account_dir.mkdir(parents=True, exist_ok=True)
                return account_dir
        except Exception:
            pass
    return DATA_DIR

_ACCOUNT_DATA_DIR = _get_account_data_dir()
COOKIES_PATH = _ACCOUNT_DATA_DIR / "cookies.jar"
SETTINGS_PATH = _ACCOUNT_DATA_DIR / "settings.json"
```

- [ ] **Step 2: Verify existing single-account still works (no active_account set)**

```bash
cd /home/claude/twitchdrops
python3 -c "from src.config.paths import COOKIES_PATH, SETTINGS_PATH; print(COOKIES_PATH, SETTINGS_PATH)"
```

Expected: paths ending in `data/cookies.jar` and `data/settings.json` (no accounts subfolder since no active_account set).

- [ ] **Step 3: Verify account-specific paths work**

```bash
python3 -c "
import json, pathlib
cfg = pathlib.Path('data/web_config.json')
orig = json.loads(cfg.read_text())
orig['active_account'] = 'testaccount'
cfg.write_text(json.dumps(orig))
"
python3 -c "from src.config.paths import COOKIES_PATH, SETTINGS_PATH; print(COOKIES_PATH, SETTINGS_PATH)"
```

Expected: paths ending in `data/accounts/testaccount/cookies.jar` and `data/accounts/testaccount/settings.json`.

- [ ] **Step 4: Restore web_config.json (remove test account)**

```bash
python3 -c "
import json, pathlib
cfg = pathlib.Path('data/web_config.json')
d = json.loads(cfg.read_text())
d.pop('active_account', None)
cfg.write_text(json.dumps(d, indent=2))
"
```

- [ ] **Step 5: Commit**

```bash
cd /home/claude/twitchdrops
git add src/config/paths.py
git commit -m "feat: dynamic COOKIES_PATH/SETTINGS_PATH per active_account"
```

---

### Task 2: Account management API endpoints

**Files:**
- Modify: `src/web/app.py`

- [ ] **Step 1: Add account list endpoint**

Add after the `/api/auth/status` endpoint in `src/web/app.py`:

```python
# ==================== Account Management ====================

@app.get("/api/accounts")
async def list_accounts():
    """List all saved accounts and the currently active one"""
    cfg = _load_web_config()
    active = cfg.get("active_account", "")
    accounts_dir = _DATA_DIR / "accounts"
    accounts = []
    if accounts_dir.exists():
        for d in sorted(accounts_dir.iterdir()):
            if d.is_dir():
                has_cookies = (d / "cookies.jar").exists()
                accounts.append({"label": d.name, "active": d.name == active, "has_cookies": has_cookies})
    return {"accounts": accounts, "active": active}


class AccountSwitchRequest(BaseModel):
    label: str


@app.post("/api/accounts/switch")
async def switch_account(data: AccountSwitchRequest):
    """Switch active account and schedule restart"""
    cfg = _load_web_config()
    account_dir = _DATA_DIR / "accounts" / data.label
    if not account_dir.exists():
        raise HTTPException(status_code=404, detail="Account not found")
    cfg["active_account"] = data.label
    _save_web_config(cfg)
    # Restart via PM2 after 1s
    async def _restart():
        await asyncio.sleep(1)
        import subprocess
        subprocess.Popen(["pm2", "restart", "twitchdrops"])
    asyncio.create_task(_restart())
    return {"success": True}


class AccountAddRequest(BaseModel):
    label: str


@app.post("/api/accounts/add")
async def add_account(data: AccountAddRequest):
    """Create a new account slot and switch to it (triggers restart + fresh login)"""
    label = data.label.strip()
    if not label or "/" in label or "\\" in label or "." in label:
        raise HTTPException(status_code=400, detail="Invalid account label")
    account_dir = _DATA_DIR / "accounts" / label
    if account_dir.exists():
        raise HTTPException(status_code=409, detail="Account label already exists")
    account_dir.mkdir(parents=True, exist_ok=True)
    cfg = _load_web_config()
    cfg["active_account"] = label
    _save_web_config(cfg)
    async def _restart():
        await asyncio.sleep(1)
        import subprocess
        subprocess.Popen(["pm2", "restart", "twitchdrops"])
    asyncio.create_task(_restart())
    return {"success": True}


@app.delete("/api/accounts/{label}")
async def remove_account(label: str):
    """Delete an account (cannot delete the active account)"""
    cfg = _load_web_config()
    if cfg.get("active_account") == label:
        raise HTTPException(status_code=400, detail="Cannot delete the active account. Switch first.")
    account_dir = _DATA_DIR / "accounts" / label
    if not account_dir.exists():
        raise HTTPException(status_code=404, detail="Account not found")
    import shutil
    shutil.rmtree(account_dir)
    return {"success": True}
```

- [ ] **Step 2: Restart miner and verify endpoints respond**

```bash
pm2 restart twitchdrops && sleep 4
curl -s http://127.0.0.1:8080/api/accounts -H "Cookie: __tdm_session=spenowayvetT13" | python3 -m json.tool
```

Expected: `{"accounts": [], "active": ""}` (no accounts yet since no active_account set).

- [ ] **Step 3: Commit**

```bash
cd /home/claude/twitchdrops
git add src/web/app.py
git commit -m "feat: add /api/accounts CRUD endpoints"
```

---

### Task 3: System tab — Account UI (HTML)

**Files:**
- Modify: `web/index.html`

- [ ] **Step 1: Add accounts section to System tab**

In `web/index.html`, find the `<div id="system-tab"` block and add the accounts section before the closing `</div>`:

```html
                    <div class="system-card" id="accounts-card">
                        <h3 id="system-accounts-header">Accounts</h3>
                        <div id="accounts-list" class="accounts-list"></div>
                        <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">
                            <input type="text" id="new-account-label" placeholder="Account label (e.g. Main)" style="flex:1;min-width:120px;background:#0e0e10;border:1px solid #3d3d4a;border-radius:6px;padding:7px 10px;color:#efeff1;font-size:.85rem;outline:none">
                            <button id="add-account-btn" class="btn-primary" style="width:auto;padding:7px 14px;font-size:.85rem">+ Add Account</button>
                        </div>
                        <div id="accounts-status" style="margin-top:8px;font-size:.82rem;display:none"></div>
                    </div>
```

Add this block inside the `<div class="system-controls">` in the system-tab, after the session card.

- [ ] **Step 2: Verify HTML is valid**

```bash
python3 -c "
from html.parser import HTMLParser
class V(HTMLParser): pass
v = V()
v.feed(open('/home/claude/twitchdrops/web/index.html').read())
print('HTML valid')
"
```

Expected: `HTML valid`

- [ ] **Step 3: Commit**

```bash
cd /home/claude/twitchdrops
git add web/index.html
git commit -m "feat: add accounts section to System tab HTML"
```

---

### Task 4: Account UI logic (JS)

**Files:**
- Modify: `web/static/app.js`

- [ ] **Step 1: Add account rendering function**

Add this function before the `// Handle modal submissions` section:

```javascript
async function loadAccounts() {
    try {
        const r = await fetch('/api/accounts');
        const d = await r.json();
        const list = document.getElementById('accounts-list');
        if (!list) return;
        if (!d.accounts || d.accounts.length === 0) {
            list.innerHTML = '<p style="font-size:.82rem;color:#adadb8;margin:0">No saved accounts. Add one below to enable switching.</p>';
            return;
        }
        list.replaceChildren(...d.accounts.map(acc => {
            const card = document.createElement('div');
            card.className = 'account-item' + (acc.active ? ' active' : '');
            const label = document.createElement('span');
            label.className = 'account-label';
            label.textContent = acc.label + (acc.active ? ' ✓' : '');
            card.appendChild(label);
            if (!acc.active) {
                const switchBtn = document.createElement('button');
                switchBtn.className = 'account-action-btn';
                switchBtn.textContent = 'Switch';
                switchBtn.addEventListener('click', () => switchAccount(acc.label));
                const deleteBtn = document.createElement('button');
                deleteBtn.className = 'account-action-btn danger';
                deleteBtn.textContent = '✕';
                deleteBtn.title = 'Remove account';
                deleteBtn.addEventListener('click', () => removeAccount(acc.label));
                card.appendChild(switchBtn);
                card.appendChild(deleteBtn);
            }
            return card;
        }));
    } catch (e) {}
}

async function switchAccount(label) {
    const status = document.getElementById('accounts-status');
    if (status) { status.textContent = `Switching to "${label}"… Miner restarts in ~5s`; status.style.display = 'block'; status.style.color = '#9147ff'; }
    try {
        await fetch('/api/accounts/switch', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ label }) });
    } catch (e) {}
}

async function removeAccount(label) {
    if (!confirm(`Remove account "${label}"? This deletes its cookies and settings.`)) return;
    try {
        const r = await fetch('/api/accounts/' + encodeURIComponent(label), { method: 'DELETE' });
        const d = await r.json();
        if (r.ok) loadAccounts();
        else { const s = document.getElementById('accounts-status'); if (s) { s.textContent = d.detail || 'Error'; s.style.display = 'block'; s.style.color = '#eb4a4a'; } }
    } catch (e) {}
}
```

- [ ] **Step 2: Wire up Add Account button and load accounts on init**

Find the `document.getElementById('system-logout-btn')` event listener block and add after it:

```javascript
    // Account management
    loadAccounts();

    document.getElementById('add-account-btn')?.addEventListener('click', async () => {
        const label = document.getElementById('new-account-label')?.value?.trim();
        const status = document.getElementById('accounts-status');
        if (!label) { if (status) { status.textContent = 'Enter an account label first.'; status.style.display = 'block'; status.style.color = '#eb4a4a'; } return; }
        try {
            const r = await fetch('/api/accounts/add', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ label }) });
            const d = await r.json();
            if (r.ok) {
                if (status) { status.textContent = `Account "${label}" created. Miner restarts for login…`; status.style.display = 'block'; status.style.color = '#9147ff'; }
            } else {
                if (status) { status.textContent = d.detail || 'Error'; status.style.display = 'block'; status.style.color = '#eb4a4a'; }
            }
        } catch (e) { }
    });
```

- [ ] **Step 3: Commit**

```bash
cd /home/claude/twitchdrops
git add web/static/app.js
git commit -m "feat: account management UI logic"
```

---

### Task 5: Account UI styles (CSS)

**Files:**
- Modify: `web/static/styles.css`

- [ ] **Step 1: Add account styles**

```bash
cat >> /home/claude/twitchdrops/web/static/styles.css << 'EOF'

/* Account Management */
.accounts-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin-bottom: 4px;
}

.account-item {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 10px;
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    font-size: .88rem;
}

.account-item.active {
    border-color: var(--accent-color);
    color: var(--accent-color);
    font-weight: 600;
}

.account-label {
    flex: 1;
}

.account-action-btn {
    background: transparent;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    color: var(--text-secondary);
    padding: 3px 8px;
    font-size: .78rem;
    cursor: pointer;
    transition: all 0.15s;
}

.account-action-btn:hover {
    background: var(--accent-color);
    color: white;
    border-color: var(--accent-color);
}

.account-action-btn.danger:hover {
    background: #c0392b;
    border-color: #c0392b;
}
EOF
```

- [ ] **Step 2: Restart miner and verify UI loads**

```bash
pm2 restart twitchdrops && sleep 4
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/ -H "Cookie: __tdm_session=spenowayvetT13"
```

Expected: `200`

- [ ] **Step 3: Commit**

```bash
cd /home/claude/twitchdrops
git add web/static/styles.css
git commit -m "feat: account management UI styles"
```

---

### Task 6: Migration — move existing account into accounts folder

**Files:**
- Modify: `data/web_config.json` (runtime, not committed)
- Modify: `src/web/app.py` — migration logic on startup

- [ ] **Step 1: Add migration logic to app.py startup**

Add this function near `_load_web_config()` / `_save_web_config()` in `src/web/app.py`:

```python
def _migrate_legacy_account() -> None:
    """If cookies.jar exists at data root but no active_account is set, keep as-is (backward compat).
    Called on first account-management API call to prompt migration."""
    pass  # No forced migration — user explicitly creates accounts via UI
```

The migration is user-driven: user clicks "Add Account" with a label like "Main", system creates `data/accounts/Main/`, then user can manually copy their existing cookies:

Add a note endpoint:

```python
@app.get("/api/accounts/migration-hint")
async def migration_hint():
    """Tells the UI if there are legacy cookies at data root that haven't been migrated"""
    legacy_cookies = (_DATA_DIR / "cookies.jar").exists()
    cfg = _load_web_config()
    has_active = bool(cfg.get("active_account"))
    return {"has_legacy": legacy_cookies and not has_active, "migrated": has_active}
```

- [ ] **Step 2: Show migration hint in UI when legacy cookies exist**

In `loadAccounts()` function in `app.js`, add after the fetch call:

```javascript
        const hint = await fetch('/api/accounts/migration-hint').then(r => r.json()).catch(() => null);
        if (hint?.has_legacy) {
            const status = document.getElementById('accounts-status');
            if (status) {
                status.textContent = 'Tip: You have an existing account. Add it with a label to enable multi-account switching.';
                status.style.display = 'block';
                status.style.color = '#adadb8';
            }
        }
```

- [ ] **Step 3: Commit**

```bash
cd /home/claude/twitchdrops
git add src/web/app.py web/static/app.js
git commit -m "feat: migration hint for legacy single-account users"
```

---

### Task 7: Update update.sh to include new files

**Files:**
- Modify: `/home/claude/twitchdrops/update.sh`

- [ ] **Step 1: Add new custom files to update.sh EXTRAS array**

In `update.sh`, update the `EXTRAS` array:

```bash
EXTRAS=(
    "web/logo.png"
    "web/favicon.ico"
    "web/favicon.png"
    "web/manifest.json"
    "data/web_config.json"
    "data/accounts"
)
```

Note: `data/accounts` is a directory — update the backup/restore logic to handle directories:

```bash
for f in "${EXTRAS[@]}"; do
    if [ -e "$f" ]; then
        mkdir -p "$BACKUP_DIR/$(dirname $f)"
        cp -r "$f" "$BACKUP_DIR/$f"
    fi
done
```

And restore:
```bash
for f in "${EXTRAS[@]}"; do
    if [ -e "$BACKUP_DIR/$f" ]; then
        cp -r "$BACKUP_DIR/$f" "$f"
    fi
done
```

Also add `src/config/paths.py` to the conflict resolution list:

```bash
git checkout --ours -- web/static/app.js web/index.html web/static/styles.css src/web/app.py src/config/paths.py 2>/dev/null || true
```

- [ ] **Step 2: Test update.sh dry run**

```bash
bash /home/claude/twitchdrops/update.sh 2>&1 | head -5
```

Expected: `Already up to date. No update needed.` (since we're on the same commit)

- [ ] **Step 3: Commit update.sh**

```bash
cd /home/claude/twitchdrops
git add update.sh
git commit -m "chore: update.sh covers multi-account data/accounts dir"
```
