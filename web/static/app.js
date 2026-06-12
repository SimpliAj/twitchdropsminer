// Twitch Drops Miner Web Client
// Socket.IO and API communication

const _accParam = new URLSearchParams(location.search).get('acc');
const ACC_NUM = _accParam === '2' ? 2 : 1;
const API_BASE = ACC_NUM === 2 ? '/acc2' : '';
const SOCKET_PATH = ACC_NUM === 2 ? '/acc2/socket.io' : '/socket.io';

function _todayStr() { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; }
let _dailyPtsTotal = 0;
let _dailyPtsDate = _todayStr();
function getDailyPoints() {
    if (_dailyPtsDate !== _todayStr()) { _dailyPtsTotal = 0; _dailyPtsDate = _todayStr(); }
    return _dailyPtsTotal;
}
function addDailyPoints(n) {
    if (_dailyPtsDate !== _todayStr()) { _dailyPtsTotal = 0; _dailyPtsDate = _todayStr(); }
    _dailyPtsTotal += n;
    fetch(API_BASE + '/api/daily-points', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ total: _dailyPtsTotal }) }).catch(() => {});
}

// Global state
const state = {
    connected: false,
    paused: false,
    channels: {},
    campaigns: {},
    settings: {},
    currentDrop: null,
    countdownTimer: null,  // Track the active countdown timer
    translations: {},  // Store current translations
    sessionPoints: {}  // channel_login -> { balance, claimed }
};

// ==================== Version Checking ====================

async function fetchAndDisplayVersion() {
    try {
        const response = await fetch(API_BASE + '/api/version');
        if (!response.ok) throw new Error('Failed to fetch version');

        const data = await response.json();
        const versionElement = document.getElementById('current-version');
        if (versionElement) {
            let versionText = data.current_version;
            // Add (latest) indicator if we know the latest version and it matches
            if (data.latest_version && data.current_version === data.latest_version) {
                versionText += ' (latest)';
            }
            versionElement.textContent = versionText;

            // Translate footer version text
            const footerVersionText = document.getElementById('footer-version-text');
            if (footerVersionText && state.translations.gui?.footer) {
                const versionLabel = state.translations.gui.footer.version || 'Version:';
                // Preserve the span inside
                const span = footerVersionText.querySelector('span');
                footerVersionText.textContent = versionLabel + ' ';
                footerVersionText.appendChild(span);
            }
        }

        // Display update notification if available
        if (data.update_available && data.latest_version) {
            const updateIndicator = document.getElementById('footer-update-indicator');
            const latestVersionSpan = document.getElementById('latest-version');
            const updateLink = document.getElementById('footer-update-link');

            if (updateIndicator && latestVersionSpan && updateLink) {
                latestVersionSpan.textContent = data.latest_version;
                updateLink.href = data.download_url;
                updateIndicator.style.display = 'inline-block';

                // Translate update message
                if (state.translations.gui?.footer) {
                    const updateLabel = state.translations.gui.footer.update_available || 'Update Available:';
                    const linkText = document.createTextNode(` ⚠ ${updateLabel} `);
                    // Clear existing text nodes but keep the span
                    const span = updateLink.querySelector('span'); // latest-version span
                    updateLink.textContent = '';
                    updateLink.appendChild(linkText);
                    updateLink.appendChild(span);
                }

                // Log to console
                console.log(`Update available: ${data.latest_version} (current: ${data.current_version})`);
            }
        }
    } catch (error) {
        console.warn('Could not fetch version information:', error);
        // Set placeholder text if fetch fails
        const versionElement = document.getElementById('current-version');
        const loadingText = state.translations.gui?.footer?.loading || 'Loading...';
        if (versionElement && versionElement.textContent === loadingText) {
            versionElement.textContent = 'Unknown';
        }
    }
}

// Initialize Socket.IO connection
const socket = io({ path: SOCKET_PATH,
    transports: ['websocket', 'polling'],
    reconnection: true,
    reconnectionDelay: 1000,
    reconnectionDelayMax: 5000,
    reconnectionAttempts: Infinity
});

// ==================== Socket.IO Event Handlers ====================

socket.on('connect', () => {
    console.log('Connected to server');
    state.connected = true;
    const connText = state.translations.gui?.websocket?.connected || 'Connected';
    const ci = document.getElementById('connection-indicator');
    ci.className = 'connected';
    const dot = ci.querySelector('.conn-dot');
    const txt = ci.querySelector('.conn-text');
    if (dot && txt) txt.textContent = ' ' + connText;
    else ci.textContent = '● ' + connText;
});

socket.on('disconnect', () => {
    console.log('Disconnected from server');
    state.connected = false;
    const disconnText = state.translations.gui?.websocket?.disconnected || 'Disconnected';
    const ci = document.getElementById('connection-indicator');
    ci.className = 'disconnected';
    const dot = ci.querySelector('.conn-dot');
    const txt = ci.querySelector('.conn-text');
    if (dot && txt) txt.textContent = ' ' + disconnText;
    else ci.textContent = '● ' + disconnText;
});

socket.on('initial_state', (data) => {
    console.log('Received initial state', data);
    if (data.status) updateStatus(data.status);

    // Batch update channels to prevent UI freezing
    if (data.channels) {
        data.channels.forEach(ch => {
            state.channels[ch.id] = ch;
        });
        renderChannels();
    }

    // Batch update campaigns to prevent UI freezing
    if (data.campaigns) {
        data.campaigns.forEach(camp => {
            state.campaigns[camp.id] = camp;
        });
        renderInventory();
    }

    // Batch update console logs
    if (data.console) {
        const consoleEl = document.getElementById('console-output');
        const fragment = document.createDocumentFragment();
        data.console.forEach(line => {
            const div = document.createElement('div');
            div.textContent = line;
            fragment.appendChild(div);
        });
        consoleEl.appendChild(fragment);
        consoleEl.scrollTop = consoleEl.scrollHeight;
        while (consoleEl.children.length > 1000) {
            consoleEl.removeChild(consoleEl.firstChild);
        }
    }

    if (data.settings) updateSettingsUI(data.settings);
    if (data.login) updateLoginStatus(data.login);
    if (data.manual_mode) updateManualModeUI(data.manual_mode);
    if (data.paused !== undefined) updatePauseState(data.paused);
    // Restore current drop progress if it exists
    if (data.current_drop) {
        updateDropProgress(data.current_drop);
    } else {
        clearDropProgress();
    }

    if (data.wanted_items) {
        renderWantedItems(data.wanted_items);
    }

    if (data.daily_points) {
        _dailyPtsTotal = data.daily_points.total || 0;
        updateStats();
    }

    if (data.channel_points_history) {
        Object.entries(data.channel_points_history).forEach(([login, balance]) => {
            if (!state.sessionPoints[login]) {
                state.sessionPoints[login] = { balance, claimed: 0 };
            }
        });
        renderPointsTracker();
    }

    if (data.watching_channel) {
        const login = data.watching_channel.login;
        const gameEl = document.getElementById('status-game');
        if (gameEl && data.watching_channel.game) {
            gameEl.textContent = 'Game: ' + data.watching_channel.game;
            gameEl.style.display = '';
        }
        updateChannelPointsDisplay(login, null);
        // Proactively check cp_enabled for current channel
        fetch(API_BASE + `/api/channel-points/${login}`).then(r => r.json()).then(d => {
            if (!state.sessionPoints[login]) state.sessionPoints[login] = { balance: 0, claimed: 0 };
            state.sessionPoints[login].cpEnabled = d.cp_enabled !== false;
            if (d.balance) state.sessionPoints[login].balance = d.balance;
            updateChannelPointsDisplay(login, null);
            renderPointsTracker();
        }).catch(() => {});
    }

    // Resume last mode after 3s if not already in the right state
    if (data.last_mode === 'idle_watch') {
        const currentStatus = (data.status || '').toLowerCase();
        const alreadyIdle = currentStatus.includes('idle') || currentStatus.includes('💤');
        if (!alreadyIdle && (data.settings?.idle_channels?.length > 0 || data.settings?.idle_use_followed)) {
            setTimeout(() => {
                fetch(API_BASE + '/api/idle-watch/resume', { method: 'POST' }).catch(() => {});
            }, 1000);
        }
    }
});

socket.on('channel_points_update', (data) => {
    const login = data.channel_login;
    const balance = data.balance || 0;
    const claimed = data.claimed_amount || 0;
    if (!state.sessionPoints[login]) {
        state.sessionPoints[login] = { balance: 0, claimed: 0 };
    }
    const prev = state.sessionPoints[login].balance;
    // Track all earned points via balance diff (covers watch points + chest bonuses)
    if (prev > 0 && balance > prev) addDailyPoints(balance - prev);
    state.sessionPoints[login].balance = balance;
    state.sessionPoints[login].claimed += claimed;
    state.sessionPoints[login].lastSeen = Date.now();
    if (data.cp_enabled !== undefined) state.sessionPoints[login].cpEnabled = data.cp_enabled;
    updateChannelPointsDisplay(login, claimed);
    renderPointsTracker();
    updateStats();
});

socket.on('status_update', (data) => {
    updateStatus(data.status);
});

socket.on('console_output', (data) => {
    addConsoleLine(data.message);
});

socket.on('channel_add', (data) => {
    updateChannel(data);
});

socket.on('channel_update', (data) => {
    updateChannel(data);
});

socket.on('channel_remove', (data) => {
    removeChannel(data.id);
});

socket.on('channels_clear', () => {
    clearChannels();
});

socket.on('channels_batch_update', (data) => {
    // Replace all channels atomically to prevent flickering
    state.channels = {};
    data.channels.forEach(ch => {
        state.channels[ch.id] = ch;
    });
    renderChannels();
});

socket.on('channel_watching', (data) => {
    setWatchingChannel(data.id);
    if (data.login) {
        updateChannelPointsDisplay(data.login, null);
        fetch(API_BASE + `/api/channel-points/${data.login}`).then(r => r.json()).then(d => {
            if (!state.sessionPoints[data.login]) state.sessionPoints[data.login] = { balance: 0, claimed: 0 };
            state.sessionPoints[data.login].cpEnabled = d.cp_enabled !== false;
            if (d.balance) state.sessionPoints[data.login].balance = d.balance;
            updateChannelPointsDisplay(data.login, null);
            renderPointsTracker();
        }).catch(() => {});
    }
});

socket.on('channel_watching_clear', () => {
    clearWatchingChannel();
});

socket.on('drop_progress', (data) => {
    updateDropProgress(data);
});

socket.on('drop_progress_stop', () => {
    clearDropProgress();
});

socket.on('campaign_add', (data) => {
    addCampaign(data);
});

socket.on('inventory_clear', () => {
    clearInventory();
});

socket.on('inventory_batch_update', (data) => {
    // Replace all campaigns atomically to prevent flickering
    state.campaigns = {};
    data.campaigns.forEach(camp => {
        state.campaigns[camp.id] = camp;
    });
    renderInventory();
});

socket.on('drop_update', (data) => {
    updateDrop(data.campaign_id, data.drop);
});

socket.on('login_required', () => {
    showLoginForm();
});

socket.on('oauth_code_required', (data) => {
    showOAuthCode(data.url, data.code);
});

socket.on('login_status', (data) => {
    updateLoginStatus(data);
});

socket.on('login_clear', (data) => {
    if (data.login) document.getElementById('username').value = '';
    if (data.password) document.getElementById('password').value = '';
    if (data.token) document.getElementById('2fa-token').value = '';
});

socket.on('settings_updated', (data) => {
    updateSettingsUI(data);
});

socket.on('games_available', (data) => {
    state.availableGames = data.games;
});

socket.on('theme_change', (data) => {
    if (data.dark_mode) {
        document.body.classList.add('dark-mode');
    } else {
        document.body.classList.remove('dark-mode');
    }
});

socket.on('notification', (data) => {
    const pushToggle = document.getElementById('push-enabled-toggle');
    if (!pushToggle?.checked) return;
    if (!('Notification' in window) || Notification.permission !== 'granted') return;

    const title = data.title || 'Drop Claimed!';
    const body = data.message || '';
    const icon = data.image_url || '/static/icon.png';
    new Notification(title, { body, icon });
});

socket.on('campaign_end_alert', (campaigns) => {
    const pushToggle = document.getElementById('push-enabled-toggle');
    if (!pushToggle?.checked) return;
    if (!('Notification' in window) || Notification.permission !== 'granted') return;

    for (const c of campaigns) {
        new Notification(`⏰ Campaign ending in ~${c.hours_left}h`, {
            body: `${c.name} — ${c.game} (${c.remaining_drops} drops left)`,
        });
    }
});

socket.on('attention_required', (data) => {
    if (data.sound) {
        // Play notification sound
        const audio = new Audio('/static/notification.mp3');
        audio.play().catch(() => { });
    }
    // Flash title
    flashTitle();
});

socket.on('manual_mode_update', (data) => {
    updateManualModeUI(data);
});

socket.on('language_changed', (data) => {
    console.log('Language changed to:', data.language);
    fetchAndApplyTranslations();
});

socket.on('wanted_items_update', (data) => {
    renderWantedItems(data);
});

socket.on('pause_state', function(data) {
    updatePauseState(data.paused);
});

// ==================== UI Update Functions ====================

function updateChannelPointsDisplay(login, claimedAmount) {
    const channelEl = document.getElementById('channel-points-channel');
    const balanceEl = document.getElementById('channel-points-balance');
    const claimedEl = document.getElementById('channel-points-claimed');
    if (!channelEl || !balanceEl) return;

    const pts = state.sessionPoints[login];
    channelEl.textContent = login;
    const cpDisabled = pts && pts.cpEnabled === false;
    balanceEl.textContent = cpDisabled ? 'No Points' : (pts ? `${pts.balance.toLocaleString()} pts` : '0 pts');
    balanceEl.style.color = cpDisabled ? '#adadb8' : '';

    if (claimedAmount && claimedEl) {
        claimedEl.textContent = `+${claimedAmount.toLocaleString()} pts`;
        claimedEl.style.opacity = '1';
        setTimeout(() => { claimedEl.style.opacity = '0'; }, 3000);
    }
}

function renderPointsTracker() {
    const section = document.getElementById('points-tracker-section');
    const list = document.getElementById('points-tracker-list');
    if (!section || !list) return;

    const entries = Object.entries(state.sessionPoints);
    if (entries.length === 0) {
        section.style.display = 'none';
        return;
    }

    section.style.display = 'block';
    list.replaceChildren();
    entries
        .filter(([, data]) => data.lastSeen)
        .sort((a, b) => b[1].lastSeen - a[1].lastSeen)
        .slice(0, 3)
        .forEach(([login, data]) => {
            const row = document.createElement('div');
            row.style.cssText = 'display:flex;justify-content:space-between;padding:2px 0;font-size:0.85rem;';
            const nameEl = document.createElement('span');
            nameEl.style.display = 'flex';nameEl.style.alignItems = 'center';nameEl.style.gap = '5px';
            const nameTxt = document.createElement('span');
            nameTxt.textContent = login;
            nameTxt.style.color = 'var(--accent-color,#9147ff)';
            nameEl.appendChild(nameTxt);
            if (data.cpEnabled === false) {
                const badge = document.createElement('span');
                badge.textContent = 'No Points';
                badge.style.cssText = 'font-size:0.7rem;background:#3d3d4a;color:#adadb8;padding:1px 5px;border-radius:4px;';
                nameEl.appendChild(badge);
            }
            const ptsEl = document.createElement('span');
            ptsEl.textContent = data.cpEnabled === false ? '—' : `${data.balance.toLocaleString()} pts`;
            row.appendChild(nameEl);
            row.appendChild(ptsEl);
            list.appendChild(row);
        });
    renderChannelPointsTab();
}


// ==================== Drop History ====================
async function loadStats() {
    try {
        const resp = await fetch(API_BASE + '/api/stats');
        const data = await resp.json();

        // Summary
        document.getElementById('stats-total').textContent = data.total_claims;
        document.getElementById('stats-games').textContent = data.by_game.length;
        const lastClaim = data.recent[0]?.timestamp;
        document.getElementById('stats-last').textContent = lastClaim
            ? new Date(lastClaim).toLocaleDateString()
            : '—';

        // By game bars
        const maxCount = data.by_game[0]?.count || 1;
        const gameContainer = document.getElementById('stats-by-game');
        gameContainer.textContent = '';
        for (const { game, count } of data.by_game) {
            const pct = Math.round((count / maxCount) * 100);
            const row = document.createElement('div');
            row.className = 'stats-game-bar';
            const label = document.createElement('span');
            label.className = 'stats-game-bar-label';
            label.title = game;
            label.textContent = game;
            const track = document.createElement('div');
            track.className = 'stats-game-bar-track';
            const fill = document.createElement('div');
            fill.className = 'stats-game-bar-fill';
            fill.style.width = pct + '%';
            track.appendChild(fill);
            const countEl = document.createElement('span');
            countEl.className = 'stats-game-bar-count';
            countEl.textContent = count;
            row.appendChild(label);
            row.appendChild(track);
            row.appendChild(countEl);
            gameContainer.appendChild(row);
        }

        // Contribution grid (GitHub-style)
        const gridEl = document.getElementById('stats-contribution-grid');
        if (gridEl && data.by_day.length > 0) {
            // Build date->count map
            const dayMap = {};
            for (const { date, count } of data.by_day) dayMap[date] = count;
            const maxCount = Math.max(...Object.values(dayMap), 1);

            // Generate last 52 weeks: current week on left, oldest on right
            const today = new Date();
            const startOfCurrentWeek = new Date(today);
            startOfCurrentWeek.setDate(today.getDate() - today.getDay()); // last Sunday
            const oldestSunday = new Date(startOfCurrentWeek);
            oldestSunday.setDate(startOfCurrentWeek.getDate() - 51 * 7);

            // Build week columns oldest→newest, then reverse so newest is left
            const weeks = [];
            let cur = new Date(oldestSunday);
            while (cur <= startOfCurrentWeek) {
                const week = [];
                for (let d = 0; d < 7; d++) {
                    const ds = cur.toISOString().slice(0, 10);
                    week.push({ date: ds, count: dayMap[ds] || 0 });
                    cur.setDate(cur.getDate() + 1);
                }
                weeks.push(week);
            }
            weeks.reverse(); // newest week first (leftmost)

            // Month labels
            const monthNames = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
            const labelsDiv = document.createElement('div');
            labelsDiv.className = 'contrib-labels';
            let lastMonth = -1;
            for (const week of weeks) {
                const m = new Date(week[0].date).getMonth();
                const span = document.createElement('span');
                span.className = 'contrib-month-label';
                span.style.minWidth = '15px';
                if (m !== lastMonth) { span.textContent = monthNames[m]; lastMonth = m; }
                labelsDiv.appendChild(span);
            }
            gridEl.innerHTML = '';
            gridEl.appendChild(labelsDiv);

            const grid = document.createElement('div');
            grid.className = 'contrib-grid';
            for (const week of weeks) {
                const col = document.createElement('div');
                col.className = 'contrib-col';
                for (const { date, count } of week) {
                    const cell = document.createElement('div');
                    cell.className = 'contrib-cell';
                    cell.setAttribute('data-count', count);
                    if (count > 0) {
                        const level = count >= 6 ? 4 : count >= 4 ? 3 : count >= 2 ? 2 : 1;
                        cell.setAttribute('data-level', level);
                    }
                    cell.title = count > 0 ? `${date}: ${count} claim${count > 1 ? 's' : ''}` : date;
                    col.appendChild(cell);
                }
                grid.appendChild(col);
            }
            gridEl.appendChild(grid);
        }

    } catch (e) {
        console.error('Failed to load stats:', e);
    }
}

async function loadDropHistory() {
    try {
        const resp = await fetch(API_BASE + "/api/drops-history");
        const data = await resp.json();
        renderDropHistory(data);
    } catch (e) { console.error("Failed to load drop history", e); }
}

function renderDropHistory(drops) {
    const emptyEl = document.getElementById("history-empty");
    const listEl = document.getElementById("history-list");
    const summaryEl = document.getElementById("history-summary");
    if (!emptyEl || !listEl) return;
    if (!drops || drops.length === 0) {
        emptyEl.style.display = "block";
        listEl.style.display = "none";
        if (summaryEl) summaryEl.textContent = "";
        return;
    }
    const today = new Date().toDateString();
    const todayCount = drops.filter(d => new Date(d.timestamp).toDateString() === today).length;
    emptyEl.style.display = "none";
    listEl.style.display = "block";
    if (summaryEl) summaryEl.textContent = `${drops.length} total · ${todayCount} today`;
    listEl.replaceChildren();

    listEl.style.cssText = 'max-height:520px;overflow-y:auto;scrollbar-width:thin;scrollbar-color:var(--border-color) transparent;';

    // Group by date
    const groups = new Map();
    drops.forEach(drop => {
        const ts = new Date(drop.timestamp);
        const dateKey = ts.toLocaleDateString("de-AT", { day:"2-digit", month:"2-digit", year:"numeric" });
        if (!groups.has(dateKey)) groups.set(dateKey, []);
        groups.get(dateKey).push({ ...drop, ts });
    });

    groups.forEach((dayDrops, dateKey) => {
        // Date header
        const dateHeader = document.createElement('div');
        dateHeader.style.cssText = 'font-size:0.7rem;font-weight:600;letter-spacing:0.07em;text-transform:uppercase;color:var(--text-secondary);padding:10px 4px 4px;border-bottom:1px solid var(--border-color);margin-bottom:2px;position:sticky;top:0;background:var(--bg-primary,#1a1a1a);z-index:1;';
        const isToday = new Date(dayDrops[0].ts).toDateString() === today;
        dateHeader.textContent = isToday ? `Today — ${dateKey}` : dateKey;
        listEl.appendChild(dateHeader);

        dayDrops.forEach(drop => {
            const row = document.createElement('div');
            row.style.cssText = 'display:flex;align-items:center;gap:8px;padding:5px 4px;border-radius:4px;transition:background 0.1s;min-width:0;';
            row.addEventListener('mouseover', () => row.style.background = 'rgba(255,255,255,0.04)');
            row.addEventListener('mouseout', () => row.style.background = '');

            // Item image (28×28)
            const imgWrap = document.createElement('div');
            imgWrap.style.cssText = 'width:28px;height:28px;border-radius:4px;overflow:hidden;flex-shrink:0;background:var(--bg-secondary);display:flex;align-items:center;justify-content:center;font-size:0.85rem;';
            if (drop.image_url) {
                const img = document.createElement('img');
                img.src = drop.image_url;
                img.style.cssText = 'width:100%;height:100%;object-fit:cover;';
                img.onerror = () => { imgWrap.textContent = '🎁'; };
                imgWrap.appendChild(img);
            } else {
                imgWrap.textContent = '🎁';
            }
            row.appendChild(imgWrap);

            // Time
            const timeEl = document.createElement('span');
            timeEl.textContent = drop.ts.toLocaleTimeString("de-AT", { hour:"2-digit", minute:"2-digit" });
            timeEl.style.cssText = 'font-size:0.75rem;color:var(--text-secondary);white-space:nowrap;flex-shrink:0;font-variant-numeric:tabular-nums;width:36px;';
            row.appendChild(timeEl);

            // Game tag
            const gameEl = document.createElement('span');
            gameEl.textContent = drop.game;
            gameEl.style.cssText = 'font-size:0.72rem;color:var(--accent-color);white-space:nowrap;flex-shrink:0;max-width:120px;overflow:hidden;text-overflow:ellipsis;';
            row.appendChild(gameEl);

            // Drop name (fills space)
            const dropNameEl = document.createElement('span');
            dropNameEl.textContent = drop.drop;
            dropNameEl.style.cssText = 'flex:1;font-size:0.82rem;color:var(--text-primary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;';
            row.appendChild(dropNameEl);

            // Reward badge
            const rewardEl = document.createElement('span');
            rewardEl.textContent = drop.reward;
            rewardEl.style.cssText = 'font-size:0.68rem;font-weight:600;color:#3ddc84;background:rgba(61,220,132,0.1);padding:2px 7px;border-radius:20px;white-space:nowrap;flex-shrink:0;max-width:160px;overflow:hidden;text-overflow:ellipsis;';
            row.appendChild(rewardEl);

            listEl.appendChild(row);
        });
    });
}

// ==================== Stats Widget ====================
async function updateStats() {
    const el = document.getElementById("stat-points-session");
    if (el) el.textContent = getDailyPoints().toLocaleString();
    try {
        const resp = await fetch(API_BASE + "/api/drops-history");
        const drops = await resp.json();
        const today = new Date().toDateString();
        const todayCount = drops.filter(d => new Date(d.timestamp).toDateString() === today).length;
        const todayEl = document.getElementById("stat-drops-today");
        const totalEl = document.getElementById("stat-drops-total");
        if (todayEl) todayEl.textContent = todayCount;
        if (totalEl) totalEl.textContent = drops.length;
    } catch(e) {}
}
function renderChannelPointsTab() {
    const emptyEl = document.getElementById('cp-tab-empty');
    const listEl = document.getElementById('cp-tab-list');
    const summaryEl = document.getElementById('cp-tab-summary');
    if (!emptyEl || !listEl) return;

    const entries = Object.entries(state.sessionPoints).sort((a, b) => b[1].balance - a[1].balance);
    if (entries.length === 0) {
        emptyEl.style.display = 'block';
        listEl.style.display = 'none';
        if (summaryEl) summaryEl.textContent = '';
        return;
    }

    emptyEl.style.display = 'none';
    listEl.style.display = 'block';

    const totalBalance = entries.reduce((s, [, d]) => s + (d.balance || 0), 0);
    const totalClaimed = entries.reduce((s, [, d]) => s + (d.claimed || 0), 0);
    if (summaryEl) {
        summaryEl.textContent = `${entries.length} channels · ${totalBalance.toLocaleString()} pts total${totalClaimed > 0 ? ` · +${totalClaimed.toLocaleString()} session` : ''}`;
    }

    listEl.replaceChildren();
    listEl.style.cssText = 'max-height:480px;overflow-y:auto;scrollbar-width:thin;scrollbar-color:var(--border-color) transparent;';

    entries.forEach(([login, data], idx) => {
        const isTop = idx === 0;
        const row = document.createElement('div');
        row.style.cssText = `display:flex;align-items:center;gap:8px;padding:7px 8px;border-radius:5px;cursor:pointer;transition:background 0.1s;${isTop ? 'background:rgba(145,71,255,0.07);' : ''}`;
        row.addEventListener('mouseover', () => row.style.background = 'rgba(145,71,255,0.1)');
        row.addEventListener('mouseout', () => row.style.background = isTop ? 'rgba(145,71,255,0.07)' : '');
        row.addEventListener('click', () => window.open(`https://www.twitch.tv/${login}`, '_blank'));

        const rankEl = document.createElement('span');
        rankEl.textContent = isTop ? '👑' : `${idx + 1}`;
        rankEl.style.cssText = `font-size:${isTop ? '0.85rem' : '0.72rem'};color:var(--text-secondary);width:22px;text-align:center;flex-shrink:0;`;
        row.appendChild(rankEl);

        const nameEl = document.createElement('span');
        nameEl.textContent = login;
        nameEl.style.cssText = `flex:1;font-size:0.88rem;font-weight:${isTop ? '600' : '400'};color:${isTop ? 'var(--accent-color)' : 'var(--text-primary)'};overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`;
        row.appendChild(nameEl);

        if (data.claimed > 0) {
            const badge = document.createElement('span');
            badge.textContent = `+${data.claimed.toLocaleString()}`;
            badge.style.cssText = 'font-size:0.72rem;font-weight:600;color:#3ddc84;background:rgba(61,220,132,0.1);padding:1px 6px;border-radius:20px;white-space:nowrap;flex-shrink:0;';
            row.appendChild(badge);
        }

        const ptsEl = document.createElement('span');
        ptsEl.textContent = `${(data.balance || 0).toLocaleString()} pts`;
        ptsEl.style.cssText = `font-size:0.85rem;font-weight:600;color:${isTop ? 'var(--accent-color)' : 'var(--text-primary)'};white-space:nowrap;flex-shrink:0;`;
        row.appendChild(ptsEl);

        listEl.appendChild(row);
    });
}

function updateStatus(status) {
    document.getElementById('status-text').textContent = status;
    if (!/watching/i.test(status)) {
        const gameEl = document.getElementById('status-game');
        if (gameEl) { gameEl.textContent = ''; gameEl.style.display = 'none'; }
    }
    updateQCButtons(status);
}

function updateQCButtons(status) {
    const isIdleWatching = /idle watching/i.test(status) || status.includes('💤');
    const isMining = state.currentDrop !== null;

    const checkBtn = document.getElementById('qc-check-drops-btn');
    const switchBtn = document.getElementById('qc-switch-btn');
    const skipBtn = document.getElementById('qc-skip-btn');

    // "Start Drop Mining" button
    if (checkBtn) {
        const strong = checkBtn.querySelector('strong');
        const small = checkBtn.querySelector('small');
        checkBtn.classList.remove('qc-btn--active', 'qc-btn--active-green');
        if (isMining) {
            // Actively mining a drop → green
            checkBtn.classList.add('qc-btn--active-green');
            if (strong) strong.textContent = 'Drop Mining Active';
            if (small) small.textContent = 'Currently farming drops';
        } else if (isIdleWatching) {
            // Idle-watching → purple, action is start mining
            checkBtn.classList.add('qc-btn--active');
            if (strong) strong.textContent = 'Start Drop Mining';
            if (small) small.textContent = 'Stop idle, search for drops now';
        } else {
            if (strong) strong.textContent = 'Start Drop Mining';
            if (small) small.textContent = 'Search for active drops';
        }
    }

    // "Switch Channel" — only visible when idle-watching
    if (switchBtn) {
        switchBtn.style.display = isIdleWatching ? '' : 'none';
        switchBtn.classList.toggle('qc-btn--active', isIdleWatching);
    }

    // "Start Idle Watch" — visible when NOT idle-watching and idle channels configured
    const idleBtn = document.getElementById('qc-idle-btn');
    const hasIdleChannels = (state.settings?.idle_channels?.length > 0) || state.settings?.idle_use_followed;
    if (idleBtn) {
        idleBtn.style.display = (!isIdleWatching && hasIdleChannels) ? '' : 'none';
    }

    // "Skip Current Game" — shown + highlighted yellow when actively mining
    if (skipBtn && skipBtn.style.display !== 'none') {
        skipBtn.classList.add('qc-btn--active-warn');
        skipBtn.classList.remove('qc-btn--active');
        const strong = skipBtn.querySelector('strong');
        const small = skipBtn.querySelector('small');
        if (strong) strong.textContent = 'Skip Game';
        if (small) small.textContent = 'End this drop, find next game';
    } else if (skipBtn) {
        skipBtn.classList.remove('qc-btn--active-warn', 'qc-btn--active');
    }
}

function addConsoleLine(message) {
    addConsoleLineRaw(message);
}

function addConsoleLineRaw(line) {
    const console = document.getElementById('console-output');
    const div = document.createElement('div');
    div.textContent = line;
    console.appendChild(div);
    // Auto-scroll to bottom
    console.scrollTop = console.scrollHeight;
    // Limit lines
    while (console.children.length > 1000) {
        console.removeChild(console.firstChild);
    }
}

function updateChannel(channelData) {
    state.channels[channelData.id] = channelData;
    renderChannels();
}

function removeChannel(channelId) {
    delete state.channels[channelId];
    renderChannels();
}

function clearChannels() {
    state.channels = {};
    renderChannels();
}

function setWatchingChannel(channelId) {
    Object.values(state.channels).forEach(ch => ch.watching = false);
    if (state.channels[channelId]) {
        state.channels[channelId].watching = true;
    }
    renderChannels();
}

function clearWatchingChannel() {
    Object.values(state.channels).forEach(ch => ch.watching = false);
    renderChannels();
}

function renderChannels() {
    const container = document.getElementById('channels-list');
    container.innerHTML = '';

    const t = state.translations;
    const channels = Object.values(state.channels);
    if (channels.length === 0) {
        const emptyMsg = t.gui?.channels?.no_channels || 'No channels tracked yet...';
        container.replaceChildren(
            makeElement('p', { class: 'empty-message' }, emptyMsg),
        );
        return;
    }

    // Get the games to watch list from settings
    const gamesToWatch = state.settings.games_to_watch || [];
    const gamesToWatchSet = new Set(gamesToWatch);

    // Filter channels to only include those playing games in the watch list
    const filteredChannels = channels.filter(channel => {
        const gameName = channel.game;
        // Include channels if: they have a game AND it's in the watch list
        // OR if the watch list is empty (show all)
        return gamesToWatch.length === 0 || (gameName && gamesToWatchSet.has(gameName));
    });

    if (filteredChannels.length === 0) {
        const emptyMsg = t.gui?.channels?.no_channels_for_games || 'No channels found for selected games...';
        container.replaceChildren(
            makeElement('p', { class: 'empty-message' }, emptyMsg),
        );
        return;
    }

    // Group channels by game
    const gameGroups = {};
    filteredChannels.forEach(channel => {
        const gameName = channel.game || 'No Game';
        const gameId = channel.game_id || 'no-game';
        const gameIcon = channel.game_icon;

        if (!gameGroups[gameId]) {
            gameGroups[gameId] = {
                name: gameName,
                icon: gameIcon,
                channels: []
            };
        }
        gameGroups[gameId].channels.push(channel);
    });

    // Sort games: prioritize games with watching channels, then by total viewers
    const sortedGames = Object.entries(gameGroups).sort(([idA, groupA], [idB, groupB]) => {
        const hasWatchingA = groupA.channels.some(ch => ch.watching);
        const hasWatchingB = groupB.channels.some(ch => ch.watching);

        if (hasWatchingA !== hasWatchingB) return hasWatchingB ? 1 : -1;

        // Sum total viewers for each game
        const totalViewersA = groupA.channels.reduce((sum, ch) => sum + (ch.viewers || 0), 0);
        const totalViewersB = groupB.channels.reduce((sum, ch) => sum + (ch.viewers || 0), 0);

        return totalViewersB - totalViewersA;
    });

    // Render each game group
    sortedGames.forEach(([gameId, group]) => {
        // Create game header
        const gameHeader = document.createElement('div');
        gameHeader.className = 'game-group-header';

        const channelCount = group.channels.length;
        const totalViewers = group.channels.reduce((sum, ch) => sum + (ch.viewers || 0), 0);

        const channelText = channelCount === 1
            ? (t.gui?.channels?.channel_count || 'channel')
            : (t.gui?.channels?.channel_count_plural || 'channels');
        const viewersText = t.gui?.channels?.viewers || 'viewers';

        if (group.icon) {
            gameHeader.appendChild(makeImageElement(group.icon.replace('{width}', '40').replace('{height}', '53'), group.name, 'game-icon'));
        }
        gameHeader.appendChild(makeElement('div', { class: 'game-group-info' }, null, el => {
            el.appendChild(makeElement('div', { class: 'game-group-name' }, group.name));
            el.appendChild(makeElement('div', { class: 'game-group-stats' }, `${channelCount} ${channelText} • ${totalViewers.toLocaleString()} ${viewersText}`));
        }));

        container.appendChild(gameHeader);

        // Sort channels within game: watching first, then online, then by viewers
        group.channels.sort((a, b) => {
            if (a.watching !== b.watching) return b.watching ? 1 : -1;
            if (a.online !== b.online) return b.online ? 1 : -1;
            return (b.viewers || 0) - (a.viewers || 0);
        });

        // Render channels in this game
        group.channels.forEach(channel => {
            const div = document.createElement('div');
            div.className = 'channel-item';
            if (channel.watching) div.classList.add('watching');
            if (channel.online) div.classList.add('online');
            else div.classList.add('offline');

            const nameDiv = makeElement('div', { class: 'channel-name' }, channel.name, el => {
                if (channel.drops_enabled) {
                    el.appendChild(document.createTextNode(' '));
                    el.appendChild(makeElement('span', { class: 'channel-badge drops' }, 'DROPS'));
                }
                if (channel.acl_based) {
                    el.appendChild(document.createTextNode(' '));
                    el.appendChild(makeElement('span', { class: 'channel-badge acl' }, 'ACL'));
                }
            });
            const infoDiv = makeElement('div', { class: 'channel-info' }, channel.viewers !== null ? channel.viewers.toLocaleString() + ' viewers' : 'Offline', el => {
                if (channel.watching) {
                    el.appendChild(document.createTextNode(' • '));
                    el.appendChild(makeElement('strong', {}, 'WATCHING'));
                }
            });
            div.replaceChildren(nameDiv, infoDiv);

            div.onclick = () => selectChannel(channel.id);
            container.appendChild(div);
        });
    });
}

function updateDropProgress(data) {
    // Check if this is a new drop or if remaining seconds changed significantly
    const isNewDrop = !state.currentDrop || state.currentDrop.drop_id !== data.drop_id;

    // Store old remaining seconds before updating state
    const oldRemaining = state.currentDrop ? state.currentDrop.remaining_seconds : null;

    // Update state with new data
    state.currentDrop = data;

    document.getElementById('no-drop-message').style.display = 'none';
    document.getElementById('drop-info').style.display = 'block';
    const qcSkip = document.getElementById('qc-skip-btn');
    if (qcSkip) qcSkip.style.display = '';
    updateQCButtons(document.getElementById('status-text')?.textContent || '');

    document.getElementById('drop-name').textContent = data.drop_name;

    // Make campaign name clickable with link to Twitch
    const dropGameEl = document.getElementById('drop-game');
    if (data.campaign_id) {
        const campaignUrl = `https://www.twitch.tv/drops/campaigns?dropID=${data.campaign_id}`;
        dropGameEl.replaceChildren(
            makeElement('a', { href: campaignUrl, target: '_blank', rel: 'noopener noreferrer', class: 'drop-campaign-link' }, data.campaign_name),
            document.createTextNode(` (${data.game_name})`),
        );
    } else {
        dropGameEl.textContent = `${data.campaign_name} (${data.game_name})`;
    }

    const progress = data.progress * 100;
    const fill = document.getElementById('progress-fill');
    fill.style.width = `${progress}%`;
    fill.textContent = `${Math.round(progress)}%`;

    document.getElementById('progress-text').textContent =
        `${data.current_minutes} / ${data.required_minutes} minutes`;

    // Only reset the timer if it's a new drop or if backend time differs by more than 2 seconds
    // This prevents constant timer resets from periodic backend updates
    const shouldResetTimer = isNewDrop || oldRemaining === null || Math.abs(oldRemaining - data.remaining_seconds) > 2;

    if (shouldResetTimer) {
        // Cancel any existing countdown timer before starting a new one
        if (state.countdownTimer !== null) {
            clearTimeout(state.countdownTimer);
            state.countdownTimer = null;
        }

        // Start countdown with the new value from backend
        updateRemainingTime(data.remaining_seconds);
    }
    // Otherwise, let the existing timer continue counting down smoothly
}

function updateRemainingTime(seconds) {
    const minutes = Math.floor(seconds / 60);
    const secs = seconds % 60;
    document.getElementById('progress-time').textContent =
        `Time remaining: ${minutes}:${secs.toString().padStart(2, '0')}`;

    if (seconds > 0) {
        // Store the timer ID so we can cancel it if needed
        state.countdownTimer = setTimeout(() => updateRemainingTime(seconds - 1), 1000);
    } else {
        state.countdownTimer = null;
    }
}

function clearDropProgress() {
    state.currentDrop = null;

    // Cancel any active countdown timer
    if (state.countdownTimer !== null) {
        clearTimeout(state.countdownTimer);
        state.countdownTimer = null;
    }

    document.getElementById('no-drop-message').style.display = 'block';
    document.getElementById('drop-info').style.display = 'none';
    const qcSkip = document.getElementById('qc-skip-btn');
    if (qcSkip) qcSkip.style.display = 'none';
    updateQCButtons(document.getElementById('status-text')?.textContent || '');
}

function addCampaign(campaignData) {
    state.campaigns[campaignData.id] = campaignData;
    renderInventory();
}

function clearInventory() {
    state.campaigns = {};
    renderInventory();
}

function updateDrop(campaignId, dropData) {
    if (state.campaigns[campaignId]) {
        const drops = state.campaigns[campaignId].drops;
        const index = drops.findIndex(d => d.id === dropData.id);
        if (index !== -1) {
            drops[index] = dropData;
            renderInventory();
        }
    }
}

// ==================== Inventory Filtering ====================

function getInventoryFilters() {
    // Get filter state from UI checkboxes and selected games array
    return {
        show_active: document.getElementById('filter-active')?.checked || false,
        show_linked: document.getElementById('filter-linked')?.checked || false,
        show_not_linked: document.getElementById('filter-not-linked')?.checked || false,
        show_upcoming: document.getElementById('filter-upcoming')?.checked || false,
        show_expired: document.getElementById('filter-expired')?.checked || false,
        show_finished: document.getElementById('filter-finished')?.checked || false,
        game_name_search: [...selectedInventoryGames],  // Array of selected game names
        // Benefit type filters (default to true if checkbox doesn't exist)
        show_benefit_item: document.getElementById('filter-benefit-item')?.checked !== false,
        show_benefit_badge: document.getElementById('filter-benefit-badge')?.checked !== false,
        show_benefit_emote: document.getElementById('filter-benefit-emote')?.checked !== false,
        show_benefit_other: document.getElementById('filter-benefit-other')?.checked !== false,
        show_sub_drops: document.getElementById('filter-sub-drops')?.checked || false,
    };
}


function getCampaignStatus(campaign) {
    const now = new Date();
    const startsAt = new Date(campaign.starts_at);
    const endsAt = new Date(campaign.ends_at);
    // Use backend valid flag if present, otherwise derive from timestamps
    const valid = campaign.valid !== undefined ? campaign.valid : (campaign.active || campaign.upcoming);
    const expired = !valid || endsAt <= now;
    const upcoming = valid && now < startsAt;
    const active = valid && !expired && !upcoming;
    return { active, upcoming, expired };
}

function campaignMatchesFilters(campaign, filters) {
    // Calculate "finished" status: all drops claimed
    const isFinished = campaign.total_drops > 0 && campaign.claimed_drops === campaign.total_drops;

    // Always hide finished campaigns unless explicitly shown (fix #52)
    if (!filters.show_finished && isFinished) return false;

    // Link status: only filter when explicitly checked
    if (filters.show_linked && !filters.show_not_linked && !campaign.linked) return false;
    if (filters.show_not_linked && !filters.show_linked && campaign.linked) return false;

    // Check status filters (OR logic among: active, upcoming, expired, finished)
    const hasGameFilter = filters.game_name_search && filters.game_name_search.length > 0;
    const hasStatusFilters = filters.show_active || filters.show_upcoming ||
        filters.show_expired || filters.show_finished || hasGameFilter;

    if (!hasStatusFilters) return true;

    // Compute status live from timestamps to avoid stale backend cache
    const { active, upcoming, expired } = getCampaignStatus(campaign);

    let statusMatch = false;
    if (filters.show_active && active) statusMatch = true;
    if (filters.show_upcoming && upcoming) statusMatch = true;
    if (filters.show_expired && expired) statusMatch = true;
    if (filters.show_finished && isFinished) statusMatch = true;

    if (hasStatusFilters && !hasGameFilter && !statusMatch) return false;

    // Check game name filter (AND logic with status filters, OR logic among selected games)
    if (hasGameFilter) {
        const gameName = campaign.game_name;
        // Campaign must match at least ONE of the selected games
        const gameMatch = filters.game_name_search.includes(gameName);
        if (!gameMatch) {
            return false;
        }
    }

    // Hide subscription-required drops unless show_sub_drops is enabled
    if (!filters.show_sub_drops && campaign.subscription_required) return false;

    // Check benefit type filter - campaign must have at least one drop with a matching benefit type
    // Only filter if at least one benefit type is UNCHECKED (otherwise show all)
    const allBenefitsEnabled = filters.show_benefit_item && filters.show_benefit_badge &&
        filters.show_benefit_emote && filters.show_benefit_other;

    if (!allBenefitsEnabled && campaign.drops) {
        let benefitMatch = false;
        for (const drop of campaign.drops) {
            if (drop.benefits && drop.benefits.length > 0) {
                for (const benefit of drop.benefits) {
                    const benefitType = (benefit.type || '').toUpperCase();
                    // Map filter checkboxes to actual API benefit types
                    if (filters.show_benefit_item && benefitType === 'DIRECT_ENTITLEMENT') benefitMatch = true;
                    if (filters.show_benefit_badge && benefitType === 'BADGE') benefitMatch = true;
                    if (filters.show_benefit_emote && benefitType === 'EMOTE') benefitMatch = true;
                    if (filters.show_benefit_other && benefitType === 'UNKNOWN') benefitMatch = true;
                }
            }
        }
        if (!benefitMatch) {
            return false;
        }
    }


    return true;
}


function onInventoryFilterChange() {
    // Save filter state to settings and re-render inventory
    saveSettings();
    renderInventory();
}

function clearInventoryFilters() {
    // Uncheck all filter checkboxes
    document.getElementById('filter-active').checked = false;
    if (document.getElementById('filter-linked')) document.getElementById('filter-linked').checked = false;
    document.getElementById('filter-not-linked').checked = false;
    document.getElementById('filter-upcoming').checked = false;
    document.getElementById('filter-expired').checked = false;
    document.getElementById('filter-finished').checked = false;
    document.getElementById('inventory-game-search').value = '';

    // Reset benefit type filters to checked (show all)
    if (document.getElementById('filter-benefit-item')) document.getElementById('filter-benefit-item').checked = true;
    if (document.getElementById('filter-benefit-badge')) document.getElementById('filter-benefit-badge').checked = true;
    if (document.getElementById('filter-benefit-emote')) document.getElementById('filter-benefit-emote').checked = true;
    if (document.getElementById('filter-benefit-other')) document.getElementById('filter-benefit-other').checked = true;

    // Clear selected games
    selectedInventoryGames = [];
    updateGameTagsDisplay();

    // Save and re-render
    saveSettings();
    renderInventory();
}


// ==================== Game Dropdown & Tags ====================

// Track selected games for inventory filter
let selectedInventoryGames = [];
let gameDropdownFocusedIndex = -1;
let gameDropdownVisible = false;

function getAvailableGamesForDropdown() {
    // Combine games from campaigns and availableGames Set
    const gamesFromCampaigns = Object.values(state.campaigns).map(c => c.game_name);
    const gamesFromSettings = Array.from(availableGames || []);

    // Merge and deduplicate
    const allGames = [...new Set([...gamesFromCampaigns, ...gamesFromSettings])];

    // Sort alphabetically
    return allGames.sort((a, b) => a.localeCompare(b));
}

function renderGameDropdown(searchTerm = '') {
    const dropdown = document.getElementById('game-dropdown-list');
    const allGames = getAvailableGamesForDropdown();

    // Filter games by search term (case-insensitive)
    const searchLower = searchTerm.toLowerCase().trim();
    const filteredGames = searchLower
        ? allGames.filter(game => game.toLowerCase().includes(searchLower))
        : allGames;

    dropdown.innerHTML = '';

    if (filteredGames.length === 0) {
        dropdown.replaceChildren(makeElement('div', { class: 'dropdown-item no-results' }, 'No games found'));
        gameDropdownFocusedIndex = -1;
        return;
    }

    filteredGames.forEach((gameName, index) => {
        const isSelected = selectedInventoryGames.includes(gameName);
        const isFocused = index === gameDropdownFocusedIndex;

        const item = document.createElement('div');
        item.className = 'dropdown-item' + (isFocused ? ' focused' : '');
        item.dataset.gameName = gameName;
        item.dataset.index = index;

        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.checked = isSelected;
        checkbox.id = `game-dropdown-${index}`;

        const label = document.createElement('label');
        label.setAttribute('for', `game-dropdown-${index}`);
        label.textContent = gameName;

        item.appendChild(checkbox);
        item.appendChild(label);

        // Click handler for the entire item
        item.addEventListener('click', (e) => {
            e.stopPropagation();
            toggleGameSelection(gameName);
        });

        dropdown.appendChild(item);
    });
}

function toggleGameSelection(gameName) {
    const index = selectedInventoryGames.indexOf(gameName);
    if (index >= 0) {
        // Remove game
        selectedInventoryGames.splice(index, 1);
    } else {
        // Add game
        selectedInventoryGames.push(gameName);
    }

    updateGameTagsDisplay();
    renderGameDropdown(document.getElementById('inventory-game-search').value);
    saveSettings();
    renderInventory();
}

function removeGameTag(gameName) {
    const index = selectedInventoryGames.indexOf(gameName);
    if (index >= 0) {
        selectedInventoryGames.splice(index, 1);
        updateGameTagsDisplay();
        renderGameDropdown(document.getElementById('inventory-game-search').value);
        saveSettings();
        renderInventory();
    }
}

function updateGameTagsDisplay() {
    const container = document.getElementById('selected-game-tags');
    container.innerHTML = '';

    selectedInventoryGames.forEach(gameName => {
        const tag = document.createElement('div');
        tag.className = 'game-tag';

        const nameSpan = document.createElement('span');
        nameSpan.className = 'game-tag-name';
        nameSpan.textContent = gameName;

        const removeBtn = document.createElement('button');
        removeBtn.className = 'game-tag-remove';
        removeBtn.textContent = '×';
        removeBtn.setAttribute('aria-label', `Remove ${gameName}`);
        removeBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            removeGameTag(gameName);
        });

        tag.appendChild(nameSpan);
        tag.appendChild(removeBtn);
        container.appendChild(tag);
    });
}

function showGameDropdown() {
    const dropdown = document.getElementById('game-dropdown-list');
    dropdown.style.display = 'block';
    gameDropdownVisible = true;
    gameDropdownFocusedIndex = -1;
    renderGameDropdown(document.getElementById('inventory-game-search').value);
}

function closeGameDropdown() {
    const dropdown = document.getElementById('game-dropdown-list');
    dropdown.style.display = 'none';
    gameDropdownVisible = false;
    gameDropdownFocusedIndex = -1;
}

function handleGameSearchKeydown(event) {
    if (!gameDropdownVisible) {
        return;
    }

    const dropdown = document.getElementById('game-dropdown-list');
    const items = dropdown.querySelectorAll('.dropdown-item:not(.no-results)');
    const maxIndex = items.length - 1;

    if (event.key === 'ArrowDown') {
        event.preventDefault();
        gameDropdownFocusedIndex = Math.min(gameDropdownFocusedIndex + 1, maxIndex);
        renderGameDropdown(document.getElementById('inventory-game-search').value);

        // Scroll focused item into view
        const focusedItem = dropdown.querySelector('.dropdown-item.focused');
        if (focusedItem) {
            focusedItem.scrollIntoView({ block: 'nearest' });
        }
    } else if (event.key === 'ArrowUp') {
        event.preventDefault();
        gameDropdownFocusedIndex = Math.max(gameDropdownFocusedIndex - 1, 0);
        renderGameDropdown(document.getElementById('inventory-game-search').value);

        // Scroll focused item into view
        const focusedItem = dropdown.querySelector('.dropdown-item.focused');
        if (focusedItem) {
            focusedItem.scrollIntoView({ block: 'nearest' });
        }
    } else if (event.key === 'Enter') {
        event.preventDefault();
        if (gameDropdownFocusedIndex >= 0 && gameDropdownFocusedIndex <= maxIndex) {
            const focusedItem = items[gameDropdownFocusedIndex];
            const gameName = focusedItem.dataset.gameName;
            if (gameName) {
                toggleGameSelection(gameName);
            }
        }
    } else if (event.key === 'Escape') {
        event.preventDefault();
        closeGameDropdown();
        document.getElementById('inventory-game-search').blur();
    }
}

function renderInventory() {
    const container = document.getElementById('inventory-grid');
    container.innerHTML = '';

    const t = state.translations;
    const allCampaigns = Object.values(state.campaigns);

    // Apply filters
    const filters = getInventoryFilters();
    const campaigns = allCampaigns.filter(campaign => campaignMatchesFilters(campaign, filters));

    if (allCampaigns.length === 0) {
        const emptyMsg = t.gui?.inventory?.no_campaigns || 'No campaigns loaded yet...';
        container.replaceChildren(makeElement('p', { class: 'empty-message' }, emptyMsg));
        return;
    }

    if (campaigns.length === 0) {
        container.replaceChildren(makeElement('p', { class: 'empty-message' }, 'No campaigns match the current filters.'));
        return;
    }

    campaigns.forEach(campaign => {
        const card = document.createElement('div');
        card.className = 'campaign-card';

        let statusClass = '';
        let statusText = '';
        const liveStatus = getCampaignStatus(campaign);
        if (liveStatus.active) {
            statusClass = 'active';
            statusText = t.gui?.inventory?.status?.active || 'Active';
        } else if (liveStatus.upcoming) {
            statusClass = 'upcoming';
            statusText = t.gui?.inventory?.status?.upcoming || 'Upcoming';
        } else if (liveStatus.expired) {
            statusClass = 'expired';
            statusText = t.gui?.inventory?.status?.expired || 'Expired';
        }

        const claimedText = t.gui?.inventory?.status?.claimed || 'Claimed';
        const claimedCountText = t.gui?.inventory?.claimed_drops || 'claimed';

        // Build drops elements
        const dropsEl = makeElement('div', { class: 'campaign-drops' });
        campaign.drops.forEach(drop => {
            if (!filters.show_sub_drops && (drop.required_subs || 0) > 0) return;
            const dropItem = makeElement('div', { class: `drop-item${drop.is_claimed ? ' claimed' : ''}${drop.can_claim ? ' active' : ''}` });
            const subBadge = (drop.required_subs || 0) > 0
                ? makeElement('span', { class: 'sub-required-badge', title: `Requires ${drop.required_subs} subscription(s)` }, '★ Sub')
                : null;
            dropItem.appendChild(
                makeElement('div', { class: 'drop-item-header' }, '', el =>
                    el.appendChild(makeElement('div', { class: 'drop-item-info' }, '', el2 =>
                        el2.appendChild(makeElement('div', {}, '', el3 => {
                            el3.appendChild(makeElement('strong', {}, drop.name));
                            if (subBadge) el3.appendChild(subBadge);
                        }))
                    ))
                )
            );
            const benefitsList = makeElement('div', { class: 'benefits-list' });
            if (drop.benefits && drop.benefits.length > 0) {
                drop.benefits.forEach(benefit => {
                    benefitsList.appendChild(
                        makeElement('div', { class: 'benefit-item' }, '', el => {
                            el.appendChild(makeImageElement(benefit.image_url, benefit.name, 'benefit-icon'));
                            el.appendChild(makeElement('div', { class: 'benefit-info' }, '', el2 => {
                                el2.appendChild(makeElement('span', { class: 'benefit-name' }, benefit.name));
                                el2.appendChild(makeElement('span', { class: 'benefit-type' }, `(${benefit.type})`));
                            }));
                        })
                    );
                });
            }
            dropItem.appendChild(benefitsList);
            dropItem.appendChild(makeElement('div', {}, `${drop.current_minutes} / ${drop.required_minutes} minutes (${Math.round(drop.progress * 100)}%)`));
            if (drop.is_claimed) {
                dropItem.appendChild(makeElement('div', {}, `✓ ${claimedText}`));
            }
            dropsEl.appendChild(dropItem);
        });

        // Campaign name link
        const campaignNameLink = makeElement('a', { href: campaign.campaign_url, target: '_blank', rel: 'noopener noreferrer', class: 'campaign-name-link' }, campaign.name, el =>
            el.appendChild(makeElement('span', { class: 'external-link-icon' }, '🔗'))
        );

        // Linked/not linked badge
        const linkStatusBadge = campaign.linked
            ? makeElement('span', { class: 'campaign-badge linked', title: 'Account is linked' }, 'LINKED')
            : makeElement('span', { class: 'campaign-badge not-linked', title: 'Account not linked. Click to link on Twitch, then use "Check for Drops" to refresh status.' }, 'NOT LINKED', el => {
                el.addEventListener('click', () => window.open(campaign.link_url, '_blank'));
            });

        // Link account button
        const campaignGameDiv = makeElement('div', { class: 'campaign-game' }, '', el => {
            if (campaign.game_box_art_url) {
                const iconUrl = campaign.game_box_art_url.replace('{width}', '52').replace('{height}', '70');
                el.appendChild(makeImageElement(iconUrl, campaign.game_name, 'game-icon'));
            }
            el.appendChild(makeElement('span', { class: 'campaign-game-name' }, campaign.game_name));
            el.appendChild(linkStatusBadge);
        });

        const campaignHeader = makeElement('div', { class: 'campaign-header' }, '', el => {
            el.appendChild(campaignGameDiv);
            el.appendChild(campaignNameLink);
            if (!campaign.linked && campaign.link_url) {
                el.appendChild(makeElement('button', { class: 'link-account-btn' }, 'Link Account', btn => {
                    btn.addEventListener('click', () => window.open(campaign.link_url, '_blank'));
                }));
            }
        });

        // Toggle button
        const dropCount = campaign.drops.filter(d => !filters.show_sub_drops ? (d.required_subs || 0) === 0 : true).length;
        const toggleBtn = makeElement('button', { class: 'inv-toggle-btn' }, `▸ ${dropCount} drop${dropCount !== 1 ? 's' : ''}`);

        const campaignStatus = makeElement('div', { class: 'campaign-status' }, '', el => {
            el.appendChild(makeElement('span', {}, statusText));
            el.appendChild(makeElement('span', {}, `${campaign.claimed_drops} / ${campaign.total_drops} ${claimedCountText}`));
            el.appendChild(toggleBtn);
        });

        dropsEl.style.display = 'none';

        toggleBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            const open = dropsEl.style.display !== 'none';
            dropsEl.style.display = open ? 'none' : '';
            toggleBtn.textContent = open ? `▸ ${dropCount} drop${dropCount !== 1 ? 's' : ''}` : `▾ ${dropCount} drop${dropCount !== 1 ? 's' : ''}`;
        });

        card.replaceChildren(campaignHeader, campaignStatus);

        // Campaign timing
        if (liveStatus.active && campaign.ends_at) {
            const endsLabel = t.gui?.inventory?.ends || 'Ends: {time}';
            card.appendChild(makeElement('div', { class: 'campaign-timing' }, endsLabel.replace('{time}', new Date(campaign.ends_at).toLocaleString())));
        } else if (liveStatus.upcoming && campaign.starts_at) {
            const startsLabel = t.gui?.inventory?.starts || 'Starts: {time}';
            card.appendChild(makeElement('div', { class: 'campaign-timing' }, startsLabel.replace('{time}', new Date(campaign.starts_at).toLocaleString())));
        } else if (liveStatus.expired && campaign.ends_at) {
            const endsLabel = t.gui?.inventory?.ends || 'Ends: {time}';
            card.appendChild(makeElement('div', { class: 'campaign-timing' }, endsLabel.replace('{time}', new Date(campaign.ends_at).toLocaleString())));
        }

        card.appendChild(dropsEl);
        container.appendChild(card);
    });
}

function showLoginForm() {
    document.getElementById('login-form').style.display = 'block';
    document.getElementById('oauth-code-display').style.display = 'none';
}

function showOAuthCode(url, code) {
    document.getElementById('login-form').style.display = 'none';
    document.getElementById('oauth-code-display').style.display = 'block';
    document.getElementById('oauth-url').href = url;
    document.getElementById('oauth-code').textContent = code;
}

function updateLoginStatus(data) {
    const statusEl = document.getElementById('login-status');
    const t = state.translations;
    if (data.user_id) {
        const name = data.user_login || String(data.user_id);
        statusEl.textContent = `${data.status} (@${name})`;
        statusEl.removeAttribute('translation-key');
        statusEl.style.color = 'var(--success-color)';
        document.getElementById('login-form').style.display = 'none';
        document.getElementById('oauth-code-display').style.display = 'none';
    } else {
        const loggedOut = t.gui?.login?.logged_out || 'Not logged in';
        statusEl.textContent = data.status || loggedOut;
        statusEl.setAttribute('translation-key', 'logged_out');
        statusEl.style.color = 'var(--text-secondary)';
        // Check if OAuth is pending (for late-connecting clients)
        if (data.oauth_pending) {
            showOAuthCode(data.oauth_pending.url, data.oauth_pending.code);
        }
    }
}

function updateSettingsUI(settings) {
    state.settings = settings;
    document.getElementById('dark-mode').checked = settings.dark_mode || false;
    document.getElementById('connection-quality').value = settings.connection_quality || 1;
    document.getElementById('minimum-refresh-interval').value = settings.minimum_refresh_interval_minutes || 30;

    // Update proxy settings and indicator
    const proxyUrl = settings.proxy || '';
    const proxyInput = document.getElementById('proxy-url');
    if (proxyInput) proxyInput.value = proxyUrl;

    const proxyIndicator = document.getElementById('proxy-indicator');
    if (proxyIndicator) {
        proxyIndicator.style.display = proxyUrl ? 'inline-flex' : 'none';
        proxyIndicator.title = proxyUrl ? `Proxy active: ${proxyUrl}` : 'Proxy disabled';
    }

    // Update language dropdown if we have the current language
    if (settings.language) {
        const languageSelect = document.getElementById('language');
        if (languageSelect) {
            languageSelect.value = settings.language;
        }
    }

    if (settings.dark_mode) {
        document.body.classList.add('dark-mode');
    } else {
        document.body.classList.remove('dark-mode');
    }

    // Update available games if provided in settings
    if (settings.games_available) {
        availableGames = new Set(settings.games_available);
    }

    // Restore inventory filters from settings
    if (settings.inventory_filters) {
        document.getElementById('filter-active').checked = settings.inventory_filters.show_active || false;
        document.getElementById('filter-not-linked').checked = settings.inventory_filters.show_not_linked || false;
        document.getElementById('filter-upcoming').checked = settings.inventory_filters.show_upcoming || false;
        document.getElementById('filter-expired').checked = settings.inventory_filters.show_expired || false;
        document.getElementById('filter-finished').checked = settings.inventory_filters.show_finished || false;

        // Restore selected games array
        selectedInventoryGames = Array.isArray(settings.inventory_filters.game_name_search)
            ? [...settings.inventory_filters.game_name_search]
            : [];  // Handle old string format gracefully
        updateGameTagsDisplay();

        // Restore benefit type filters (default to true if not set)
        if (document.getElementById('filter-benefit-item')) document.getElementById('filter-benefit-item').checked = settings.inventory_filters.show_benefit_item !== false;
        if (document.getElementById('filter-benefit-badge')) document.getElementById('filter-benefit-badge').checked = settings.inventory_filters.show_benefit_badge !== false;
        if (document.getElementById('filter-benefit-emote')) document.getElementById('filter-benefit-emote').checked = settings.inventory_filters.show_benefit_emote !== false;
        if (document.getElementById('filter-benefit-other')) document.getElementById('filter-benefit-other').checked = settings.inventory_filters.show_benefit_other !== false;
        if (document.getElementById('filter-sub-drops')) document.getElementById('filter-sub-drops').checked = settings.inventory_filters.show_sub_drops || false;
    }

    // Restore mining benefit filters
    if (settings.mining_benefits) {
        if (document.getElementById('mining-benefit-item')) document.getElementById('mining-benefit-item').checked = settings.mining_benefits.DIRECT_ENTITLEMENT;
        if (document.getElementById('mining-benefit-badge')) document.getElementById('mining-benefit-badge').checked = settings.mining_benefits.BADGE;
        if (document.getElementById('mining-benefit-emote')) document.getElementById('mining-benefit-emote').checked = settings.mining_benefits.EMOTE;
        if (document.getElementById('mining-benefit-unknown')) document.getElementById('mining-benefit-unknown').checked = settings.mining_benefits.UNKNOWN;
    }


    // Update games to watch lists
    renderGamesToWatch();

    // Re-render channels list to apply filter based on updated games to watch
    renderChannels();

    // Restore discord webhook fields
    const webhookDropsEl = document.getElementById('discord-webhook-drops');
    if (webhookDropsEl) webhookDropsEl.value = settings.discord_webhook_drops || '';
    const webhookPointsEl = document.getElementById('discord-webhook-points');
    if (webhookPointsEl) webhookPointsEl.value = settings.discord_webhook_points || '';
    const blacklistEl = document.getElementById('drop-blacklist-input');
    if (blacklistEl) blacklistEl.value = (settings.drop_name_blacklist || []).join(', ');

    const claimCpEl = document.getElementById('claim-channel-points');
    if (claimCpEl) claimCpEl.checked = settings.claim_channel_points !== false;

    renderIdleChannels(settings.idle_channels || []);

    const idleFollowedEl = document.getElementById('idle-use-followed');
    if (idleFollowedEl) idleFollowedEl.checked = settings.idle_use_followed === true;

    const schedulerEnabled = document.getElementById('scheduler-enabled');
    if (schedulerEnabled) schedulerEnabled.checked = settings.scheduler_enabled || false;
    const schedulerStart = document.getElementById('scheduler-start');
    if (schedulerStart) schedulerStart.value = settings.scheduler_start || '22:00';
    const schedulerStop = document.getElementById('scheduler-stop');
    if (schedulerStop) schedulerStop.value = settings.scheduler_stop || '08:00';

    // Re-render inventory to apply filters
    renderInventory();

    // Update Discord bot pairing status (fetch live since WebSocket data lacks bot_paired)
    fetch(API_BASE + '/api/pair/status').then(r => r.json()).then(d => updateBotPairedUI(d.paired)).catch(() => updateBotPairedUI(settings.bot_paired || false));
}

function updateBotPairedUI(paired) {
    const badge = document.getElementById('bot-status-badge');
    const revokeBtn = document.getElementById('bot-revoke-btn');
    if (badge) {
        badge.replaceChildren();
        const span = document.createElement('span');
        span.style.color = paired ? '#57d75b' : '#adadb8';
        const db = state.translations.gui?.settings?.discord_bot;
        span.textContent = paired ? (db?.connected || '✅ Connected') : (db?.not_connected || 'Not connected');
        badge.appendChild(span);
    }
    if (revokeBtn) revokeBtn.style.display = paired ? 'inline-block' : 'none';
    const configBox = document.getElementById('bot-channel-config');
    if (configBox) {
        if (paired) {
            configBox.style.display = 'block';
            loadBotChannelConfig();
        } else {
            configBox.style.display = 'none';
        }
    }
}

async function loadBotChannelConfig() {
    try {
        const res = await fetch(API_BASE + '/api/discord-bot/config');
        if (!res.ok) return;
        const data = await res.json();
        const channels = data.channels || {};
        const dropsEl = document.getElementById('bot-drops-channel');
        const pointsEl = document.getElementById('bot-points-channel');
        const dropsClear = document.getElementById('bot-drops-clear-btn');
        const pointsClear = document.getElementById('bot-points-clear-btn');
        const fmtChannels = arr => arr && arr.length ? arr.map(c => `#${c.name || c.id}`).join(', ') : null;
        const dropsStr = fmtChannels(channels.drops);
        const pointsStr = fmtChannels(channels.points);
        if (dropsEl) dropsEl.textContent = dropsStr || '—';
        if (pointsEl) pointsEl.textContent = pointsStr || '—';
        if (dropsClear) dropsClear.style.display = dropsStr ? 'inline-block' : 'none';
        if (pointsClear) pointsClear.style.display = pointsStr ? 'inline-block' : 'none';
    } catch (e) {}
}

async function botClearChannel(type) {
    if (!confirm(`Clear the ${type} notification channel?`)) return;
    try {
        const res = await fetch(API_BASE + `/api/discord-bot/config/channel/${type}`, { method: 'DELETE' });
        if (!res.ok) throw new Error(await res.text());
        loadBotChannelConfig();
    } catch (e) {
        alert('Error: ' + e.message);
    }
}

async function botGenerateCode() {
    const box = document.getElementById('bot-pair-code-box');
    const codeText = document.getElementById('bot-pair-code-text');
    const btn = document.getElementById('bot-generate-btn');
    if (btn) { btn.disabled = true; btn.textContent = state.translations.gui?.settings?.discord_bot?.generating || 'Generating...'; }
    try {
        const res = await fetch(API_BASE + '/api/pair/generate', { method: 'POST' });
        if (!res.ok) throw new Error(await res.text());
        const data = await res.json();
        const serverUrl = window.location.origin;
        if (codeText) {
            codeText.replaceChildren();
            const urlLine = document.createElement('div');
            urlLine.append('URL: ');
            const urlB = document.createElement('b');
            urlB.textContent = serverUrl;
            urlLine.appendChild(urlB);
            const codeLine = document.createElement('div');
            codeLine.append('Code: ');
            const codeB = document.createElement('b');
            codeB.textContent = data.code;
            codeLine.appendChild(codeB);
            codeText.append(urlLine, codeLine);
        }
        if (box) box.style.display = 'block';
    } catch (e) {
        alert('Fehler: ' + e.message);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = state.translations.gui?.settings?.discord_bot?.generate_code || 'Generate code'; }
    }
}

async function botRevoke() {
    if (!confirm(state.translations.gui?.settings?.discord_bot?.disconnect_confirm || 'Disconnect the Discord bot?')) return;
    try {
        const res = await fetch(API_BASE + '/api/pair/revoke', { method: 'DELETE' });
        if (!res.ok) throw new Error(await res.text());
        updateBotPairedUI(false);
        const box = document.getElementById('bot-pair-code-box');
        if (box) box.style.display = 'none';
    } catch (e) {
        alert('Fehler: ' + e.message);
    }
}

function updateManualModeUI(manualModeInfo) {
    // Manual mode UI removed — backend still sends events, ignore them
}

// ==================== Games to Watch Management ====================

let availableGames = new Set(); // All games from campaigns
let draggedElement = null;

socket.on('games_available', (data) => {
    availableGames = new Set(data.games || []);
    renderGamesToWatch();
});

function renderIdleChannels(channels) {
    state.settings.idle_channels = channels;
    const container = document.getElementById('idle-channels-list');
    if (!container) return;
    container.replaceChildren();
    channels.forEach((ch, idx) => {
        const item = document.createElement('div');
        item.className = 'sortable-item';
        const label = document.createElement('span');
        label.textContent = ch;
        const btn = document.createElement('button');
        btn.className = 'remove-btn';
        btn.textContent = '✕';
        btn.addEventListener('click', () => {
            state.settings.idle_channels.splice(idx, 1);
            renderIdleChannels([...state.settings.idle_channels]);
            saveSettings();
        });
        item.appendChild(label);
        item.appendChild(btn);
        container.appendChild(item);
    });
}

function renderGamesToWatch() {
    const selectedGames = state.settings.games_to_watch || [];
    const filterText = document.getElementById('games-filter')?.value.toLowerCase() || '';

    // Render selected games (sortable)
    renderSelectedGames(selectedGames);

    // Render available games (checkboxes for unselected games)
    const unselectedGames = Array.from(availableGames)
        .filter(game => !selectedGames.includes(game))
        .filter(game => game.toLowerCase().includes(filterText))
        .sort();

    renderAvailableGames(unselectedGames, filterText);
}

function renderSelectedGames(games) {
    const container = document.getElementById('selected-games-list');
    if (!container) return;

    const t = state.translations;
    container.innerHTML = '';

    if (games.length === 0) {
        const emptyMsg = t.gui?.settings?.no_games_selected || 'No games selected. Check games below to add them.';
        container.replaceChildren(makeElement('p', { class: 'empty-message' }, emptyMsg));
        return;
    }

    games.forEach((game, index) => {
        const div = document.createElement('div');
        div.className = 'sortable-item';
        div.draggable = true;
        div.dataset.game = game;
        div.replaceChildren(
            makeElement('span', { class: 'drag-handle' }, '☰'),
            makeElement('span', { class: 'priority-number' }, String(index + 1)),
            makeElement('span', { class: 'game-name' }, game),
            makeElement('button', { class: 'remove-btn' }, '✕'),
        );

        // Event listener for the delete button
        const removeBtn = div.querySelector('.remove-btn');
        removeBtn.addEventListener('click', () => removeGameFromWatch(game));

        // Drag event handlers
        div.addEventListener('dragstart', handleDragStart);
        div.addEventListener('dragover', handleDragOver);
        div.addEventListener('drop', handleDrop);
        div.addEventListener('dragend', handleDragEnd);

        container.appendChild(div);
    });
}

function renderAvailableGames(games, filterText) {
    const container = document.getElementById('available-games-list');
    if (!container) return;

    const t = state.translations;
    container.innerHTML = '';

    if (games.length === 0) {
        if (filterText) {
            const emptyMsg = t.gui?.settings?.no_games_match || 'No games match your search.';
            const addHint = t.gui?.settings?.add_game_hint || ' Click "Add Game" to add it manually.';
            container.replaceChildren(makeElement('p', { class: 'empty-message' }, `${emptyMsg}${addHint}`));
        } else {
            const emptyMsg = t.gui?.settings?.all_games_selected || 'All games are selected or no games available.';
            container.replaceChildren(makeElement('p', { class: 'empty-message' }, emptyMsg));
        }
        return;
    }

    games.forEach(game => {
        const label = document.createElement('label');
        label.className = 'game-checkbox';
        label.replaceChildren(
            makeElement('input', { type: 'checkbox', value: game }),
            makeElement('span', {}, game),
        );

        const checkbox = label.querySelector('input[type="checkbox"]');
        checkbox.addEventListener('change', (e) => toggleGameWatch(game, e.target.checked));

        container.appendChild(label);
    });
}

// Drag and drop handlers
function handleDragStart(e) {
    draggedElement = e.target;
    e.target.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/html', e.target.innerHTML);
}

function handleDragOver(e) {
    if (e.preventDefault) {
        e.preventDefault();
    }
    e.dataTransfer.dropEffect = 'move';

    const target = e.target.closest('.sortable-item');
    if (target && target !== draggedElement) {
        const container = target.parentNode;
        const allItems = [...container.querySelectorAll('.sortable-item')];
        const draggedIndex = allItems.indexOf(draggedElement);
        const targetIndex = allItems.indexOf(target);

        if (draggedIndex < targetIndex) {
            target.parentNode.insertBefore(draggedElement, target.nextSibling);
        } else {
            target.parentNode.insertBefore(draggedElement, target);
        }
    }
    return false;
}

function handleDrop(e) {
    if (e.stopPropagation) {
        e.stopPropagation();
    }
    return false;
}

function handleDragEnd(e) {
    e.target.classList.remove('dragging');

    // Update the order in state
    const container = document.getElementById('selected-games-list');
    const items = container.querySelectorAll('.sortable-item');
    const newOrder = Array.from(items).map(item => item.dataset.game);

    state.settings.games_to_watch = newOrder;

    // Re-render to update priority numbers
    renderSelectedGames(newOrder);

    // Re-render channels list to apply updated filter
    renderChannels();

    // Save settings
    saveSettings();
}

function toggleGameWatch(gameName, checked) {
    const games = state.settings.games_to_watch || [];

    if (checked && !games.includes(gameName)) {
        games.push(gameName);
    } else if (!checked) {
        const index = games.indexOf(gameName);
        if (index > -1) {
            games.splice(index, 1);
        }
    }

    state.settings.games_to_watch = games;
    renderGamesToWatch();
    renderChannels();
    saveSettings();
}

function removeGameFromWatch(gameName) {
    const games = state.settings.games_to_watch || [];
    const index = games.indexOf(gameName);
    if (index > -1) {
        games.splice(index, 1);
        state.settings.games_to_watch = games;
        renderGamesToWatch();
        renderChannels();
        saveSettings();
    }
}

function selectAllGames() {
    state.settings.games_to_watch = Array.from(availableGames).sort();
    renderGamesToWatch();
    renderChannels();
    saveSettings();
}

function deselectAllGames() {
    state.settings.games_to_watch = [];
    renderGamesToWatch();
    renderChannels();
    saveSettings();
}

function selectLinkedGames() {
    const linked = new Set(
        Object.values(state.campaigns)
            .filter(c => c.linked && c.game_name)
            .map(c => c.game_name)
    );
    const current = new Set(state.settings.games_to_watch || []);
    linked.forEach(g => current.add(g));
    state.settings.games_to_watch = Array.from(current).sort();
    renderGamesToWatch();
    renderChannels();
    saveSettings();
}

function selectBadgeEmoteGames() {
    const badgeEmoteGames = new Set();
    for (const camp of Object.values(state.campaigns)) {
        if (!camp.game_name || !camp.drops) continue;
        let hasFreeBadgeOrEmote = false;
        for (const drop of camp.drops) {
            if ((drop.required_subs || 0) > 0) continue;
            for (const benefit of (drop.benefits || [])) {
                const t = (benefit.type || '').toUpperCase();
                if (t === 'BADGE' || t === 'EMOTE') { hasFreeBadgeOrEmote = true; }
            }
        }
        if (hasFreeBadgeOrEmote) badgeEmoteGames.add(camp.game_name);
    }
    const current = new Set(state.settings.games_to_watch || []);
    badgeEmoteGames.forEach(g => current.add(g));
    state.settings.games_to_watch = Array.from(current).sort();
    renderGamesToWatch();
    renderChannels();
    saveSettings();
}

function addGameFromSearch() {
    const searchInput = document.getElementById('games-filter');
    const gameName = searchInput.value.trim();

    if (!gameName) {
        return;
    }

    const games = state.settings.games_to_watch || [];
    
    // Check if already selected
    if (games.includes(gameName)) {
        searchInput.value = ''; // Clear input if already added
        renderGamesToWatch(); // Just re-render to clear any filtering state if needed
        return;
    }

    // Add to selected games
    games.push(gameName);
    state.settings.games_to_watch = games;

    // Add to available games set so it shows up in lists
    availableGames.add(gameName);

    // Clear search and update UI
    searchInput.value = '';
    renderGamesToWatch();
    renderChannels();
    saveSettings();
}

function flashTitle() {
    const originalTitle = document.title;
    let count = 0;
    const interval = setInterval(() => {
        document.title = count % 2 === 0 ? '🔔 Attention!' : originalTitle;
        count++;
        if (count >= 10) {
            document.title = originalTitle;
            clearInterval(interval);
        }
    }, 1000);
}

// ==================== API Functions ====================

async function selectChannel(channelId) {
    try {
        const response = await fetch(API_BASE + '/api/channels/select', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ channel_id: channelId })
        });

        if (!response.ok) {
            const errorData = await response.json();
            console.error('Failed to select channel:', errorData.detail || 'Unknown error');
            addConsoleLine(`Error selecting channel: ${errorData.detail || 'Unknown error'}`);
        }
    } catch (error) {
        console.error('Failed to select channel:', error);
        addConsoleLine(`Error selecting channel: ${error.message}`);
    }
}

async function exitManualMode() {
    try {
        const response = await fetch(API_BASE + '/api/mode/exit-manual', {
            method: 'POST'
        });

        const result = await response.json();
        if (!result.success) {
            console.log('Exit manual mode:', result.message || 'Already in automatic mode');
        }
    } catch (error) {
        console.error('Failed to exit manual mode:', error);
        addConsoleLine(`Error exiting manual mode: ${error.message}`);
    }
}

async function submitLogin() {
    const username = document.getElementById('username').value;
    const password = document.getElementById('password').value;
    const token = document.getElementById('2fa-token').value;

    try {
        await fetch(API_BASE + '/api/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password, token })
        });
    } catch (error) {
        console.error('Failed to submit login:', error);
    }
}

async function confirmOAuth() {
    // Signal that OAuth code has been entered
    try {
        await fetch(API_BASE + '/api/oauth/confirm', {
            method: 'POST'
        });
        // Hide the OAuth form and show waiting message
        document.getElementById('oauth-code-display').style.display = 'none';
        const t = state.translations;
        const waitingAuth = t.gui?.login?.waiting_auth || 'Waiting for authentication...';
        const loginStatus = document.getElementById('login-status');
        loginStatus.textContent = waitingAuth;
        loginStatus.setAttribute('translation-key', 'waiting_auth');
    } catch (error) {
        console.error('Failed to confirm OAuth:', error);
    }
}

async function verifyProxy() {
    const proxyInput = document.getElementById('proxy-url');
    const proxyUrl = proxyInput ? proxyInput.value.trim() : '';
    const resultDiv = document.getElementById('proxy-verify-result');

    if (!resultDiv) return;

    // Reset display
    resultDiv.style.display = 'block';
    resultDiv.className = 'verify-result loading';
    resultDiv.textContent = 'Verifying connection...';

    if (!proxyUrl) {
        resultDiv.className = 'verify-result error';
        resultDiv.textContent = 'Please enter a proxy URL first.';
        return;
    }

    try {
        const response = await fetch(API_BASE + '/api/settings/verify-proxy', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ proxy: proxyUrl })
        });

        const data = await response.json();

        if (data.success) {
            resultDiv.className = 'verify-result success';
            resultDiv.textContent = `✓ ${data.message}`;
        } else {
            resultDiv.className = 'verify-result error';
            resultDiv.textContent = `✗ ${data.message}`;
        }
    } catch (error) {
        resultDiv.className = 'verify-result error';
        resultDiv.textContent = `Error: ${error.message}`;
    }
}

async function saveSettings() {
    const settings = {
        dark_mode: document.getElementById('dark-mode').checked,
        language: document.getElementById('language').value,
        connection_quality: parseInt(document.getElementById('connection-quality').value),
        minimum_refresh_interval_minutes: parseInt(document.getElementById('minimum-refresh-interval').value),
        proxy: state.settings.proxy || '',
        games_to_watch: state.settings.games_to_watch || [],
        inventory_filters: getInventoryFilters(),
        mining_benefits: {
            "DIRECT_ENTITLEMENT": document.getElementById('mining-benefit-item')?.checked,
            "BADGE": document.getElementById('mining-benefit-badge')?.checked,
            "EMOTE": document.getElementById('mining-benefit-emote')?.checked,
            "UNKNOWN": document.getElementById('mining-benefit-unknown')?.checked
        },
        discord_webhook_drops: document.getElementById('discord-webhook-drops')?.value || '',
        discord_webhook_points: document.getElementById('discord-webhook-points')?.value || '',
        claim_channel_points: document.getElementById('claim-channel-points')?.checked ?? true,
        idle_channels: state.settings.idle_channels || [],
        idle_use_followed: document.getElementById('idle-use-followed')?.checked ?? false,
        drop_name_blacklist: (document.getElementById('drop-blacklist-input')?.value || '')
            .split(',').map(s => s.trim()).filter(Boolean),
        scheduler_enabled: document.getElementById('scheduler-enabled')?.checked || false,
        scheduler_start: document.getElementById('scheduler-start')?.value || '22:00',
        scheduler_stop: document.getElementById('scheduler-stop')?.value || '08:00',
    };

    try {
        await fetch(API_BASE + '/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(settings)
        });
        console.log('Settings saved automatically');
    } catch (error) {
        console.error('Failed to save settings:', error);
    }
}

async function loadPushConfig() {
    try {
        const resp = await fetch(API_BASE + '/api/push-config');
        const cfg = await resp.json();
        const pushToggle = document.getElementById('push-enabled-toggle');
        const soundToggle = document.getElementById('push-sound-toggle');
        const alertsToggle = document.getElementById('campaign-alerts-toggle');
        if (pushToggle) pushToggle.checked = !!cfg.push_notifications_enabled;
        if (soundToggle) soundToggle.checked = cfg.push_sound_enabled !== false;
        if (alertsToggle) alertsToggle.checked = cfg.campaign_end_alerts_enabled !== false;
    } catch (e) {
        console.error('Failed to load push config:', e);
    }
}

async function savePushConfig() {
    const pushToggle = document.getElementById('push-enabled-toggle');
    const soundToggle = document.getElementById('push-sound-toggle');
    const alertsToggle = document.getElementById('campaign-alerts-toggle');
    const payload = {
        push_notifications_enabled: pushToggle?.checked || false,
        push_sound_enabled: soundToggle?.checked !== false,
        campaign_end_alerts_enabled: alertsToggle?.checked !== false,
    };
    try {
        await fetch(API_BASE + '/api/push-config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
    } catch (e) {
        console.error('Failed to save push config:', e);
    }
}

function updatePauseState(paused) {
    state.paused = paused;
    const btn = document.getElementById('pause-resume-btn');
    if (!btn) return;
    const strong = btn.querySelector('strong');
    const small = btn.querySelector('small');
    const icon = btn.querySelector('.qc-icon');
    if (paused) {
        if (icon) icon.textContent = '▶';
        if (strong) strong.textContent = 'Resume';
        if (small) small.textContent = 'Click to resume';
        btn.classList.add('qc-btn--active');
    } else {
        if (icon) icon.textContent = '⏸';
        if (strong) strong.textContent = 'Pause';
        if (small) small.textContent = 'Pause / resume the miner';
        btn.classList.remove('qc-btn--active');
    }
}

async function togglePause() {
    const endpoint = state.paused ? '/api/resume' : '/api/pause';
    try {
        const resp = await fetch(endpoint, { method: 'POST' });
        const data = await resp.json();
        updatePauseState(data.paused);
    } catch (e) {
        console.error('Failed to toggle pause:', e);
    }
}

async function testWebhook(type) {
    const id = type === 'drops' ? 'discord-webhook-drops' : 'discord-webhook-points';
    const url = document.getElementById(id)?.value.trim();
    if (!url) { alert('No webhook URL set.'); return; }
    try {
        const resp = await fetch(API_BASE + '/api/settings/test-webhook', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url })
        });
        const data = await resp.json();
        alert(data.success ? '✅ ' + data.message : '❌ ' + data.message);
    } catch (e) {
        alert('❌ Request failed: ' + e.message);
    }
}

async function fetchAndPopulateLanguages() {
    try {
        const response = await fetch(API_BASE + '/api/languages');
        const data = await response.json();

        const languageSelect = document.getElementById('language');
        if (!languageSelect) {
            console.warn('Language select element not found');
            return;
        }

        // Clear existing options
        languageSelect.innerHTML = '';

        // Populate with available languages
        data.available.forEach(lang => {
            const option = document.createElement('option');
            option.value = lang;
            option.textContent = lang;
            languageSelect.appendChild(option);
        });

        // Set current language
        if (data.current) {
            languageSelect.value = data.current;
        }
    } catch (error) {
        console.error('Failed to fetch languages:', error);
        const languageSelect = document.getElementById('language');
        if (languageSelect) {
            languageSelect.replaceChildren(makeElement('option', { value: '' }, 'Failed to load languages'));
        }
        addConsoleLine('Error: Unable to fetch available languages. Please check your connection or try again later.');
    }
}

async function fetchAndApplyTranslations() {
    try {
        const response = await fetch(API_BASE + '/api/translations');
        const data = await response.json();

        state.translations = data;
        applyTranslations(data);
        console.log('Translations applied for language:', data.language_name);
    } catch (error) {
        console.error('Failed to fetch translations:', error);
    }
}

function applyTranslations(t) {
    // Update tab buttons
    const tabButtons = {
        'main': document.querySelector('[data-tab="main"]'),
        'inventory': document.querySelector('[data-tab="inventory"]'),
        'settings': document.querySelector('[data-tab="settings"]'),
        'help': document.querySelector('[data-tab="help"]'),
        'analytics': document.querySelector('[data-tab="analytics"]'),
    };

    if (tabButtons.main && t.gui?.tabs) tabButtons.main.textContent = t.gui.tabs.main;
    if (tabButtons.inventory && t.gui?.tabs) tabButtons.inventory.textContent = t.gui.tabs.inventory;
    if (tabButtons.settings && t.gui?.tabs) tabButtons.settings.textContent = t.gui.tabs.settings;
    if (tabButtons.help && t.gui?.tabs) tabButtons.help.textContent = t.gui.tabs.help;
    if (tabButtons.analytics && t.gui?.tabs) tabButtons.analytics.textContent = t.gui.tabs.analytics ?? 'Analytics';

    // Update Main tab - Login section
    const mainTab = document.getElementById('main-tab');
    if (mainTab && t.gui?.login) {
        const loginHeader = mainTab.querySelector('.login-panel h2');
        if (loginHeader) loginHeader.textContent = t.gui.login.name;

        const loginStatus = document.getElementById('login-status');
        if (loginStatus?.hasAttribute('translation-key')) loginStatus.textContent = t.login?.status?.[loginStatus.getAttribute('translation-key')];

        // Update login form placeholders
        const usernameInput = document.getElementById('username');
        if (usernameInput) usernameInput.placeholder = t.gui.login.username;

        const passwordInput = document.getElementById('password');
        if (passwordInput) passwordInput.placeholder = t.gui.login.password;

        const twofaInput = document.getElementById('2fa-token');
        if (twofaInput) twofaInput.placeholder = t.gui.login.twofa_code;

        const loginButton = document.getElementById('login-button');
        if (loginButton) loginButton.textContent = t.gui.login.button;

        // Update OAuth display text
        const oauthDisplay = document.getElementById('oauth-code-display');
        if (oauthDisplay) {
            const oauthP = oauthDisplay.querySelector('p');
            if (oauthP) {
                const link = oauthP.querySelector('a');
                if (link) {
                    oauthP.textContent = t.gui.login.oauth_prompt + ' ';
                    link.textContent = t.gui.login.oauth_activate;
                    oauthP.appendChild(link);
                }
            }

            const oauthConfirmBtn = document.getElementById('oauth-confirm');
            if (oauthConfirmBtn) oauthConfirmBtn.textContent = t.gui.login.oauth_confirm;
        }
    }

    // Update Progress section
    if (mainTab && t.gui?.progress) {
        // ID: progress-header
        const progressHeader = document.getElementById('progress-header');
        if (progressHeader) progressHeader.textContent = t.gui.progress.name;

        const noDropMsg = document.getElementById('no-drop-message');
        if (noDropMsg) noDropMsg.textContent = t.gui.progress.no_drop;

    }

    // Update Console section
    if (mainTab && t.gui) {
        // ID: console-header
        const consoleHeader = document.getElementById('console-header');
        if (consoleHeader) consoleHeader.textContent = t.gui.output;
    }

    // Update Channels section
    if (mainTab && t.gui?.channels) {
        // ID: channels-header
        const channelsHeader = document.getElementById('channels-header');
        if (channelsHeader) channelsHeader.textContent = t.gui.channels.name;
        // Channel list will re-render with translated empty messages
        renderChannels();
    }

    // Update Inventory tab
    const inventoryTab = document.getElementById('inventory-tab');
    if (inventoryTab && t.gui?.inventory) {
        // Inventory will re-render with translated status and empty messages
        renderInventory();
    }

    // Update Settings tab
    const settingsTab = document.getElementById('settings-tab');
    if (settingsTab && t.gui?.settings) {
        // Use IDs for robust selection
        const generalHeader = document.getElementById('settings-general-header');
        if (generalHeader) generalHeader.textContent = t.gui.settings.general.name;

        const benefitsHeader = document.getElementById('settings-benefits-header');
        if (benefitsHeader && t.gui.settings.mining_benefits) benefitsHeader.textContent = t.gui.settings.mining_benefits;

        const gamesHeader = document.getElementById('settings-games-header');
        if (gamesHeader) gamesHeader.textContent = t.gui.settings.games_to_watch;

        const actionsHeader = document.getElementById('settings-actions-header');
        if (actionsHeader) actionsHeader.textContent = t.gui.settings.actions;

        const darkModeLabel = settingsTab.querySelector('label:has(#dark-mode)');
        if (darkModeLabel) {
            const checkbox = darkModeLabel.querySelector('input');
            darkModeLabel.textContent = '';
            darkModeLabel.appendChild(checkbox);
            darkModeLabel.appendChild(document.createTextNode(' ' + t.gui.settings.general.dark_mode));
        }

        const connQualityLabel = settingsTab.querySelector('label:has(#connection-quality)');
        if (connQualityLabel) {
            const input = connQualityLabel.querySelector('input');
            connQualityLabel.textContent = t.gui.settings.connection_quality + ' ';
            connQualityLabel.appendChild(input);
        }

        const refreshLabel = settingsTab.querySelector('label:has(#minimum-refresh-interval)');
        if (refreshLabel) {
            const input = refreshLabel.querySelector('input');
            refreshLabel.textContent = t.gui.settings.minimum_refresh + ' ';
            refreshLabel.appendChild(input);
        }

        const benefitsHelp = document.getElementById('settings-benefits-help');
        if (benefitsHelp && t.gui.settings.mining_benefits_help) benefitsHelp.textContent = t.gui.settings.mining_benefits_help;

        const notifHeader = document.getElementById('settings-notifications-header');
        if (notifHeader && t.gui.settings.notifications_header) notifHeader.textContent = t.gui.settings.notifications_header;
        const pushLabel = document.getElementById('settings-push-enabled-label');
        if (pushLabel && t.gui.settings.push_enabled) pushLabel.textContent = t.gui.settings.push_enabled;
        const soundLabel = document.getElementById('settings-push-sound-label');
        if (soundLabel && t.gui.settings.push_sound) soundLabel.textContent = t.gui.settings.push_sound;
        const alertLabel = document.getElementById('settings-campaign-alerts-label');
        if (alertLabel && t.gui.settings.campaign_end_alerts_enabled) alertLabel.textContent = t.gui.settings.campaign_end_alerts_enabled;

        const gamesHelp = document.getElementById('settings-games-help');
        if (gamesHelp) gamesHelp.textContent = t.gui.settings.games_help;

        const searchInput = document.getElementById('games-filter');
        if (searchInput) searchInput.placeholder = t.gui.settings.search_games;

        const selectAllBtn = document.getElementById('select-all-btn');
        if (selectAllBtn) selectAllBtn.textContent = t.gui.settings.select_all;

        const deselectAllBtn = document.getElementById('deselect-all-btn');
        if (deselectAllBtn) deselectAllBtn.textContent = t.gui.settings.deselect_all;

        const addGameBtn = document.getElementById('add-game-btn');
        if (addGameBtn && t.gui.settings.add_game) addGameBtn.textContent = t.gui.settings.add_game;

        const selectedGamesHeader = settingsTab.querySelector('.selected-games h3');
        if (selectedGamesHeader) selectedGamesHeader.textContent = t.gui.settings.selected_games;

        const availableGamesHeader = settingsTab.querySelector('.available-games h3');
        if (availableGamesHeader) availableGamesHeader.textContent = t.gui.settings.available_games;

        const reloadBtn = document.getElementById('reload-btn');
        if (reloadBtn) reloadBtn.textContent = t.gui.settings.reload_campaigns;

        // Re-render games to watch with translated empty messages
        renderGamesToWatch();

        // Discord Bot section
        if (t.gui?.settings?.discord_bot) {
            const db = t.gui.settings.discord_bot;
            const dbHeader = document.getElementById('settings-discord-bot-header');
            if (dbHeader) dbHeader.textContent = db.header;
            const dbDesc = document.getElementById('settings-discord-bot-desc');
            if (dbDesc) dbDesc.textContent = db.description;
            const dbExpires = document.getElementById('settings-discord-bot-expires');
            if (dbExpires) dbExpires.textContent = db.expires;
            const dbGenBtn = document.getElementById('bot-generate-btn');
            if (dbGenBtn && !dbGenBtn.disabled) dbGenBtn.textContent = db.generate_code;
            const dbRevokeBtn = document.getElementById('bot-revoke-btn');
            if (dbRevokeBtn) dbRevokeBtn.textContent = db.disconnect;
            const dbInviteText = document.getElementById('settings-discord-bot-invite-text');
            if (dbInviteText) dbInviteText.textContent = db.invite_bot;
        }

        const s = t.gui.settings;

        // Channel Points section
        const cpHeader = document.getElementById('settings-cp-header');
        if (cpHeader && s.channel_points_section) cpHeader.textContent = s.channel_points_section;
        const cpAutoLabel = document.getElementById('settings-cp-autoclaim-label');
        if (cpAutoLabel && s.channel_points_auto_claim) cpAutoLabel.textContent = s.channel_points_auto_claim;
        const cpAutoHelp = document.getElementById('settings-cp-autoclaim-help');
        if (cpAutoHelp && s.channel_points_auto_claim_help) cpAutoHelp.textContent = s.channel_points_auto_claim_help;

        // Discord Notifications section
        const discordNotifHeader = document.getElementById('settings-discord-notif-header');
        if (discordNotifHeader && s.discord_notifications) discordNotifHeader.textContent = s.discord_notifications;
        const dropsWebhookLabel = document.getElementById('settings-drops-webhook-label');
        if (dropsWebhookLabel && s.discord_drops_webhook_label) dropsWebhookLabel.textContent = s.discord_drops_webhook_label;
        const dropsWebhookHelp = document.getElementById('settings-drops-webhook-help');
        if (dropsWebhookHelp && s.discord_drops_webhook_help) dropsWebhookHelp.textContent = s.discord_drops_webhook_help;
        const pointsWebhookLabel = document.getElementById('settings-points-webhook-label');
        if (pointsWebhookLabel && s.discord_points_webhook_label) pointsWebhookLabel.textContent = s.discord_points_webhook_label;
        const pointsWebhookHelp = document.getElementById('settings-points-webhook-help');
        if (pointsWebhookHelp && s.discord_points_webhook_help) pointsWebhookHelp.textContent = s.discord_points_webhook_help;
        const testDropsBtn = document.getElementById('test-drops-webhook-btn');
        if (testDropsBtn && s.test_webhook) testDropsBtn.textContent = s.test_webhook;
        const testPointsBtn = document.getElementById('test-points-webhook-btn');
        if (testPointsBtn && s.test_webhook) testPointsBtn.textContent = s.test_webhook;

        // Proxy section
        const proxyLabel = document.getElementById('settings-proxy-label');
        if (proxyLabel && s.proxy_url_label) proxyLabel.textContent = s.proxy_url_label;
        const setProxyBtn = document.getElementById('set-proxy-btn');
        if (setProxyBtn && s.set_proxy) setProxyBtn.textContent = s.set_proxy;
        const verifyProxyBtn = document.getElementById('verify-proxy-btn');
        if (verifyProxyBtn && s.verify_proxy) verifyProxyBtn.textContent = s.verify_proxy;
        const proxyHelp = document.getElementById('settings-proxy-help');
        if (proxyHelp && s.proxy_url_help) proxyHelp.textContent = s.proxy_url_help;

        // Idle Watch section
        const idleHeader = document.getElementById('settings-idle-header');
        if (idleHeader && s.idle_watch) idleHeader.textContent = s.idle_watch;
        const idleHelp = document.getElementById('settings-idle-help');
        if (idleHelp && s.idle_watch_help) idleHelp.textContent = s.idle_watch_help;
        const idleAutoLabel = document.getElementById('settings-idle-auto-label');
        if (idleAutoLabel && s.idle_auto_followed) idleAutoLabel.textContent = s.idle_auto_followed;
        const idleAutoHelp = document.getElementById('settings-idle-auto-help');
        if (idleAutoHelp && s.idle_auto_followed_help) idleAutoHelp.textContent = s.idle_auto_followed_help;
        const idleChannelInput = document.getElementById('idle-channel-input');
        if (idleChannelInput && s.idle_channel_placeholder) idleChannelInput.placeholder = s.idle_channel_placeholder;
        const idleAddBtn = document.getElementById('idle-channel-add-btn');
        if (idleAddBtn && s.idle_channel_add) idleAddBtn.textContent = s.idle_channel_add;

        // Drop Name Blacklist section
        const blacklistHeader = document.getElementById('settings-blacklist-header');
        if (blacklistHeader && s.blacklist) blacklistHeader.textContent = s.blacklist;
        const blacklistHelp = document.getElementById('settings-blacklist-help');
        if (blacklistHelp && s.blacklist_help) blacklistHelp.textContent = s.blacklist_help;

        // Scheduler section
        const schedulerHeader = document.getElementById('settings-scheduler-header');
        if (schedulerHeader && s.scheduler) schedulerHeader.textContent = s.scheduler;
        const schedulerHelp = document.getElementById('settings-scheduler-help');
        if (schedulerHelp && s.scheduler_help) schedulerHelp.textContent = s.scheduler_help;
        const schedulerEnableLabel = document.getElementById('settings-scheduler-enable-label');
        if (schedulerEnableLabel && s.scheduler_enable) schedulerEnableLabel.textContent = s.scheduler_enable;
        const schedulerFrom = document.getElementById('settings-scheduler-from');
        if (schedulerFrom && s.scheduler_active_from) schedulerFrom.textContent = s.scheduler_active_from;
        const schedulerUntil = document.getElementById('settings-scheduler-until');
        if (schedulerUntil && s.scheduler_active_until) schedulerUntil.textContent = s.scheduler_active_until;
        const schedulerTimesHelp = document.getElementById('settings-scheduler-times-help');
        if (schedulerTimesHelp && s.scheduler_times_help) schedulerTimesHelp.textContent = s.scheduler_times_help;

        // Discord bot channel config texts
        const botNotifChannelsLabel = document.getElementById('settings-bot-notif-channels-label');
        if (botNotifChannelsLabel && s.bot_notification_channels) botNotifChannelsLabel.textContent = s.bot_notification_channels;
        const botSetchannelHint = document.getElementById('settings-bot-setchannel-hint');
        if (botSetchannelHint && s.bot_setchannel_hint) {
            botSetchannelHint.textContent = '';
            const code = document.createElement('code');
            code.textContent = '/setchannel';
            botSetchannelHint.appendChild(document.createTextNode('Use '));
            botSetchannelHint.appendChild(code);
            botSetchannelHint.appendChild(document.createTextNode(' in Discord to set channels.'));
            // If translation doesn't match default, override with plain text
            if (s.bot_setchannel_hint !== 'Use /setchannel in Discord to set channels.') {
                botSetchannelHint.textContent = s.bot_setchannel_hint;
            }
        }

        // Add Account button and placeholder
        const addAccountBtn = document.getElementById('add-account-btn');
        if (addAccountBtn && s.add_account) addAccountBtn.textContent = s.add_account;
        const newAccountLabel = document.getElementById('new-account-label');
        if (newAccountLabel && s.account_label_placeholder) newAccountLabel.placeholder = s.account_label_placeholder;
    }

    // Update System section (now inside settings tab)
    if (t.gui?.system) {
        const sys = t.gui.system;
        const systemHeader = document.getElementById('system-header');
        if (systemHeader) systemHeader.textContent = sys.header;
        const systemAccountsHeader = document.getElementById('system-accounts-header');
        if (systemAccountsHeader && sys.accounts_header) systemAccountsHeader.textContent = sys.accounts_header;
        const systemMinerHeader = document.getElementById('system-miner-header');
        if (systemMinerHeader) systemMinerHeader.textContent = sys.miner_header;
        const systemMinerDesc = document.getElementById('system-miner-desc');
        if (systemMinerDesc) systemMinerDesc.textContent = sys.miner_desc;
        const systemReloadBtn = document.getElementById('system-reload-btn');
        if (systemReloadBtn) systemReloadBtn.textContent = sys.reload_btn;
        const systemRestartHeader = document.getElementById('system-restart-header');
        if (systemRestartHeader) systemRestartHeader.textContent = sys.restart_header;
        const systemRestartDesc = document.getElementById('system-restart-desc');
        if (systemRestartDesc) systemRestartDesc.textContent = sys.restart_desc;
        const systemRestartBtn = document.getElementById('system-restart-btn');
        if (systemRestartBtn) systemRestartBtn.textContent = sys.restart_btn;
        const systemSessionHeader = document.getElementById('system-session-header');
        if (systemSessionHeader) systemSessionHeader.textContent = sys.session_header;
        const systemSessionDesc = document.getElementById('system-session-desc');
        if (systemSessionDesc) systemSessionDesc.textContent = sys.session_desc;
        const systemLogoutBtn = document.getElementById('system-logout-btn');
        if (systemLogoutBtn) systemLogoutBtn.textContent = sys.logout_btn;
    }

    // Update Analytics tab
    if (t.gui?.analytics) {
        const a = t.gui.analytics;
        const statsHeader = document.getElementById('analytics-stats-header');
        if (statsHeader) statsHeader.textContent = a.stats_header;
        const totalLabel = document.getElementById('analytics-total-label');
        if (totalLabel) totalLabel.textContent = a.total_claims;
        const gamesLabel = document.getElementById('analytics-games-label');
        if (gamesLabel) gamesLabel.textContent = a.games_label;
        const lastLabel = document.getElementById('analytics-last-label');
        if (lastLabel) lastLabel.textContent = a.last_claim;
        const byGameHeader = document.getElementById('analytics-by-game-header');
        if (byGameHeader) byGameHeader.textContent = a.claims_by_game;
        const activityHeader = document.getElementById('analytics-activity-header');
        if (activityHeader) activityHeader.textContent = a.claims_activity;
        const cpHeader = document.getElementById('analytics-cp-header');
        if (cpHeader) cpHeader.textContent = a.channel_points;
        const cpRefreshBtn = document.getElementById('cp-tab-refresh-btn');
        if (cpRefreshBtn) cpRefreshBtn.textContent = a.refresh;
        const cpEmpty = document.getElementById('cp-tab-empty');
        if (cpEmpty) cpEmpty.textContent = a.no_channel_points;
        const historyHeader = document.getElementById('analytics-history-header');
        if (historyHeader) historyHeader.textContent = a.drop_history;
        const historyRefreshBtn = document.getElementById('history-refresh-btn');
        if (historyRefreshBtn) historyRefreshBtn.textContent = a.refresh;
        const historyEmpty = document.getElementById('history-empty');
        if (historyEmpty) historyEmpty.textContent = a.no_history;
    }

    // Update Help tab
    const helpTab = document.getElementById('help-tab');
    if (helpTab && t.gui?.help) {
        // Robust ID selection for Help tab headers
        const aboutHeader = document.getElementById('help-about-header');
        if (aboutHeader) aboutHeader.textContent = t.gui.help.about || 'About Twitch Drops Miner';

        const howtoHeader = document.getElementById('help-howto-header');
        if (howtoHeader) howtoHeader.textContent = t.gui.help.how_to_use || 'How to Use';

        const featuresHeader = document.getElementById('help-features-header');
        if (featuresHeader) featuresHeader.textContent = t.gui.help.features || 'Features';

        const notesHeader = document.getElementById('help-notes-header');
        if (notesHeader) notesHeader.textContent = t.gui.help.important_notes || 'Important Notes';

        // Update list items and links (keeping innerHTML approach for lists as they are dynamic content blocks)
        const helpContent = helpTab.querySelector('.help-content');
        if (helpContent) {
            const howToItems = t.gui.help.how_to_use_items || [
                'Login using your Twitch account (OAuth device code flow)',
                'Link your accounts at <a href="https://www.twitch.tv/drops/campaigns" target="_blank">twitch.tv/drops/campaigns</a>',
                'The miner will automatically discover campaigns and start mining',
                'Configure priority games in Settings to focus on what you want',
                'Monitor progress in the Main and Inventory tabs'
            ];
            const featuresItems = t.gui.help.features_items || [
                'Stream-less drop mining - saves bandwidth',
                'Game priority and exclusion lists',
                'Tracks up to 199 channels simultaneously',
                'Automatic channel switching',
                'Real-time progress tracking'
            ];
            const notesItems = t.gui.help.important_notes_items || [
                'Do not watch streams on the same account while mining',
                'Keep your cookies.jar file secure',
                'Requires linked game accounts for drops'
            ];

            const webhookItems = [
                'Go to your Discord server → Channel Settings → Integrations → Webhooks → New Webhook',
                'Copy the webhook URL',
                'Paste it into <strong>Settings → Discord Notifications</strong> (separate URLs for drops and points)',
                'Use <strong>Test Webhook</strong> to verify it works',
            ];
            const botSetupItems = [
                'Go to <strong>Settings → Discord Bot</strong> and click <strong>Generate code</strong>',
                'Note your dashboard URL (e.g. <code>http://your-server:8081</code>) and the code (e.g. <code>DROPS-A1B2C3D4</code>)',
                'In Discord, run: <code>/link http://your-server:8081 DROPS-A1B2C3D4</code>',
                'The bot confirms: ✅ Connected',
            ];
            const botCommands = [
                '<code>/link [url] [code]</code> — connect to your dashboard',
                '<code>/dashboard</code> — post a live-updating embed with control buttons (pause, campaigns, drops)',
                '<code>/setchannel drops</code> — send drop notifications to this channel',
                '<code>/setchannel points</code> — send channel points notifications to this channel',
                '<code>/unlink</code> — disconnect the bot',
            ];
            helpContent.replaceChildren(
                makeElement('h2', { id: 'help-about-header' }, t.gui.help.about || 'About Twitch Drops Miner'),
                makeElement('p', {}, t.gui.help.about_text || 'This application automatically mines timed Twitch drops without downloading stream data.'),
                makeElement('h3', { id: 'help-howto-header' }, t.gui.help.how_to_use || 'How to Use'),
                makeHelpList('ol', howToItems),
                makeElement('h3', { id: 'help-features-header' }, t.gui.help.features || 'Features'),
                makeHelpList('ul', featuresItems),
                makeElement('h3', { id: 'help-notes-header' }, t.gui.help.important_notes || 'Important Notes'),
                makeHelpList('ul', notesItems),
                makeElement('h3', { id: 'help-discord-webhook-header' }, 'Discord Webhook'),
                makeElement('p', {}, 'Get notified in Discord when drops are claimed or channel points are earned.'),
                makeHelpList('ol', webhookItems),
                makeElement('h3', { id: 'help-discord-bot-header' }, 'Discord Bot'),
                makeElement('p', {}, 'Control your miner directly from Discord using slash commands.'),
                makeElement('h4', {}, 'Setup'),
                makeHelpList('ol', botSetupItems),
                makeElement('h4', {}, 'Commands'),
                makeHelpList('ul', botCommands),
                makeElement('p', {}, null, el => appendTrustedHelpContent(el, '<strong>Note:</strong> The pairing code expires in 10 minutes. To rename your profile (shown in bot footer), go to <strong>System → Accounts</strong> and use the ✏️ button.')),
                makeElement('div', { class: 'help-links' }, '', el =>
                    el.appendChild(makeElement('a', { href: 'https://github.com/SimpliAj/twitchdropsminer', target: '_blank', rel: 'noopener noreferrer' }, t.gui.help.github_repo || 'GitHub Repository'))
                ),
            );
        }
    }

    // Update Footer
    if (t.gui?.footer) {
        const loadingText = t.gui.footer.loading || 'Loading...';
        const currentVersionEl = document.getElementById('current-version');
        // Only update if it's the specific "Loading..." text to avoid overwriting the fetched version
        if (currentVersionEl && currentVersionEl.textContent === 'Loading...') {
            currentVersionEl.textContent = loadingText;
        }

        const footerVersionText = document.getElementById('footer-version-text');
        if (footerVersionText) {
            const versionLabel = t.gui.footer.version || 'Version:';
            const span = document.getElementById('current-version'); // Need to re-fetch or preserve
            footerVersionText.textContent = versionLabel + ' ';
            // Re-finding the span because textContent wiped it from parent
            if (span) footerVersionText.appendChild(span);
        }
    }

    // Update Badges tooltips
    if (t.gui?.badges) {
        const manualBadge = document.getElementById('manual-mode-badge');
        if (manualBadge && t.gui.badges.manual) manualBadge.title = t.gui.badges.manual.title;

        const autoBadge = document.getElementById('auto-mode-badge');
        if (autoBadge && t.gui.badges.auto) autoBadge.title = t.gui.badges.auto.title;

        const proxyBadge = document.getElementById('proxy-indicator');
        if (proxyBadge && t.gui.badges.proxy) proxyBadge.title = t.gui.badges.proxy.title; // Note: append logic in updateSettingsUI overrides this
    }

    // Update Wanted Drops Panel
    if (mainTab && t.gui?.wanted) {
        // ID: wanted-header
        const wantedHeader = document.getElementById('wanted-header');
        if (wantedHeader) wantedHeader.textContent = t.gui.wanted.name;
        // Re-render wanted items to update empty message
        // Since we don't store wanted items in state globally (only receives them), we rely on updateWantedItems triggering render
    }

    // Update Inventory Filters (re-using existing inventoryTab variable if available, or just querying)
    // Note: inventoryTab was declared above in "Update Inventory Status" section
    // But since that might be in a different block or not, let's be safe and just query element directly without const redeclaration if it conflicts.
    // However, looking at the code, the previous declaration was likely in the same function scope.
    // Simplest fix: use the existing element or re-query without 'const' if needed, but best to just use the one we have.
    // Actually, looking at the view_file, there was 'const inventoryTab' around line 1639.
    // So I should just reuse that variable or use a different name.

    if (inventoryTab && t.gui?.inventory?.filters) {
        const f = t.gui.inventory.filters;
        const updateLabel = (id, text) => {
            const el = document.getElementById(id)?.parentElement.querySelector('span');
            if (el) el.textContent = text;
        };
        updateLabel('filter-active', f.active);
        updateLabel('filter-not-linked', f.not_linked);
        // filter-upcoming is hidden (bare input, no label wrapper) — skip to avoid corrupting filter-active label
        updateLabel('filter-expired', f.expired);
        updateLabel('filter-finished', f.finished);
        updateLabel('filter-benefit-item', f.item);
        updateLabel('filter-benefit-badge', f.badge);
        updateLabel('filter-benefit-emote', f.emote);
        updateLabel('filter-benefit-other', f.other);

        const clearBtn = document.getElementById('clear-filters-btn');
        if (clearBtn) clearBtn.textContent = f.clear;

        const searchInput = document.getElementById('games-filter');
        if (searchInput) searchInput.placeholder = f.search_placeholder;

        // Update Mining Benefit Labels in Settings (re-using inventory filter keys)
        // IDs: mining-benefit-item, mining-benefit-badge, mining-benefit-emote, mining-benefit-unknown
        updateLabel('mining-benefit-item', f.item);
        updateLabel('mining-benefit-badge', f.badge);
        updateLabel('mining-benefit-emote', f.emote);
        updateLabel('mining-benefit-unknown', f.other);
    }

    // Update header elements
    if (t.gui?.header) {
        const languageLabel = document.querySelector('.language-selector span');
        if (languageLabel) languageLabel.textContent = t.gui.header.language;

        const statusText = document.getElementById('status-text');
        if (statusText && statusText.textContent === 'Initializing...') {
            statusText.textContent = t.gui.header.initializing;
        }

        // Update connection indicator
        const connIndicator = document.getElementById('connection-indicator');
        if (connIndicator) {
            const txt = connIndicator.querySelector('.conn-text');
            const label = state.connected
                ? (t.gui.websocket.connected || 'Connected')
                : (t.gui.websocket.disconnected || 'Disconnected');
            if (txt) txt.textContent = ' ' + label;
            else connIndicator.textContent = '● ' + label;
        }
    }
}

async function reloadCampaigns() {
    try {
        await fetch(API_BASE + '/api/reload', { method: 'POST' });
        // Status will update via Socket.IO when backend starts operation
    } catch (error) {
        console.error('Failed to reload:', error);
    }
}


// ==================== Tab Management ====================

function switchTab(tabName) {
    // Hide all tabs
    document.querySelectorAll('.tab-content').forEach(tab => {
        tab.classList.remove('active');
    });
    document.querySelectorAll('.tab-button').forEach(btn => {
        btn.classList.remove('active');
    });

    // Show selected tab
    document.getElementById(`${tabName}-tab`).classList.add('active');
    document.querySelector(`[data-tab="${tabName}"]`).classList.add('active');
    if (tabName === 'analytics') { loadStats(); loadDropHistory(); }
    if (tabName === 'settings') {
        fetch(API_BASE + '/api/pair/status').then(r => r.json()).then(d => updateBotPairedUI(d.paired)).catch(() => {});
        loadPushConfig();
        loadAccounts();
    }
}

// ==================== Event Listeners ====================

function switchAccount(num) {
    if (num === ACC_NUM) return;
    const url = new URL(location.href);
    if (num === 2) url.searchParams.set('acc', '2');
    else url.searchParams.delete('acc');
    location.href = url.toString();
}

function initAccountTabs() {
    const btn1 = document.getElementById('acc-btn-1');
    const btn2 = document.getElementById('acc-btn-2');
    if (!btn1 || !btn2) return;
    if (ACC_NUM === 2) {
        btn1.classList.remove('active-acc');
        btn2.classList.add('active-acc');
    }
    const acc1Base = '';
    const acc2Base = '/acc2';
    const updateBtn = (btn, data) => {
        if (data.login) btn.textContent = data.login;
        else if (data.label && !data.label.startsWith('Instance')) btn.textContent = data.label;
    };
    fetch(acc1Base + '/api/instance').then(r => r.json()).then(d => updateBtn(btn1, d)).catch(() => {});
    fetch(acc2Base + '/api/instance').then(r => r.json()).then(d => updateBtn(btn2, d)).catch(() => {});
}

document.addEventListener('DOMContentLoaded', () => {
    // Fetch and display version information
    fetchAndDisplayVersion();
    updateStats();
    initAccountTabs();
    document.getElementById("history-refresh-btn")?.addEventListener("click", loadDropHistory);

    // Tab switching
    document.querySelectorAll('.tab-button').forEach(button => {
        button.addEventListener('click', () => {
            switchTab(button.dataset.tab);
        });
    });

    // Login form
    document.getElementById('login-button').addEventListener('click', submitLogin);
    document.getElementById('oauth-confirm').addEventListener('click', confirmOAuth);

    // Settings - auto-save on change
    document.getElementById('dark-mode').addEventListener('change', (e) => {
        // Apply dark mode immediately for instant feedback
        if (e.target.checked) {
            document.body.classList.add('dark-mode');
        } else {
            document.body.classList.remove('dark-mode');
        }
        // Then save settings
        saveSettings();
    });
    document.getElementById('language').addEventListener('change', saveSettings);
    document.getElementById('connection-quality').addEventListener('change', saveSettings);
    document.getElementById('minimum-refresh-interval').addEventListener('change', saveSettings);
    // Proxy uses a manual "Set Proxy" button instead of auto-save
    document.getElementById('set-proxy-btn').addEventListener('click', () => {
        const proxyInput = document.getElementById('proxy-url');
        const newValue = proxyInput ? proxyInput.value : '';

        // Only save if changed
        if (newValue !== (state.settings.proxy || '')) {
            state.settings.proxy = newValue;
            saveSettings();
        }
    });
    document.getElementById('verify-proxy-btn').addEventListener('click', verifyProxy);
    document.getElementById('reload-btn').addEventListener('click', reloadCampaigns);


    // Games to watch management
    document.getElementById('select-all-btn').addEventListener('click', selectAllGames);
    document.getElementById('deselect-all-btn').addEventListener('click', deselectAllGames);
    document.getElementById('select-linked-btn')?.addEventListener('click', selectLinkedGames);
    document.getElementById('select-badge-emote-btn')?.addEventListener('click', selectBadgeEmoteGames);
    document.getElementById('add-game-btn').addEventListener('click', addGameFromSearch);
    document.getElementById('games-filter').addEventListener('input', renderGamesToWatch);

    // Inventory filters
    document.getElementById('filter-active').addEventListener('change', onInventoryFilterChange);
    document.getElementById('filter-linked')?.addEventListener('change', onInventoryFilterChange);
    document.getElementById('filter-not-linked').addEventListener('change', onInventoryFilterChange);
    document.getElementById('filter-upcoming').addEventListener('change', onInventoryFilterChange);
    document.getElementById('filter-expired').addEventListener('change', onInventoryFilterChange);
    document.getElementById('filter-finished').addEventListener('change', onInventoryFilterChange);
    // Benefit type filters
    document.getElementById('filter-benefit-item').addEventListener('change', onInventoryFilterChange);
    document.getElementById('filter-benefit-badge').addEventListener('change', onInventoryFilterChange);
    document.getElementById('filter-benefit-emote').addEventListener('change', onInventoryFilterChange);
    document.getElementById('filter-benefit-other').addEventListener('change', onInventoryFilterChange);
    document.getElementById('filter-sub-drops')?.addEventListener('change', onInventoryFilterChange);
    document.getElementById('clear-filters-btn').addEventListener('click', clearInventoryFilters);

    // Mining benefit settings
    document.getElementById('mining-benefit-item').addEventListener('change', saveSettings);
    document.getElementById('mining-benefit-badge').addEventListener('change', saveSettings);
    document.getElementById('mining-benefit-emote').addEventListener('change', saveSettings);
    document.getElementById('mining-benefit-unknown').addEventListener('change', saveSettings);
    document.getElementById('discord-webhook-drops')?.addEventListener('blur', saveSettings);
    document.getElementById('drop-blacklist-input')?.addEventListener('change', saveSettings);
    document.getElementById('discord-webhook-points')?.addEventListener('blur', saveSettings);
    document.getElementById('claim-channel-points')?.addEventListener('change', saveSettings);

    document.getElementById('push-enabled-toggle')?.addEventListener('change', async function() {
        if (this.checked && 'Notification' in window && Notification.permission !== 'granted') {
            await Notification.requestPermission();
        }
        savePushConfig();
    });
    document.getElementById('push-sound-toggle')?.addEventListener('change', savePushConfig);
    document.getElementById('campaign-alerts-toggle')?.addEventListener('change', savePushConfig);

    document.getElementById('cp-tab-refresh-btn')?.addEventListener('click', async () => {
        const btn = document.getElementById('cp-tab-refresh-btn');
        if (btn) btn.textContent = '↻ Loading...';
        const logins = Object.keys(state.sessionPoints);
        await Promise.all(logins.map(async login => {
            try {
                const resp = await fetch(API_BASE + `/api/channel-points/${login}`);
                const data = await resp.json();
                if (data.balance !== undefined) {
                    if (!state.sessionPoints[login]) state.sessionPoints[login] = { balance: 0, claimed: 0 };
                    state.sessionPoints[login].balance = data.balance;
                }
            } catch(e) {}
        }));
        renderChannelPointsTab();
        renderPointsTracker();
        if (btn) btn.textContent = '↻ Refresh';
    });
    document.getElementById("qc-check-drops-btn")?.addEventListener("click", async () => {
        const btn = document.getElementById("qc-check-drops-btn");
        if (btn) { btn.disabled = true; btn.style.opacity = "0.6"; }
        try {
            await fetch(API_BASE + "/api/reload", { method: "POST" });
        } catch (e) { addConsoleLine("Error: " + e.message); }
        setTimeout(() => { if (btn) { btn.disabled = false; btn.style.opacity = ""; } }, 3000);
    });

    document.getElementById("qc-skip-btn")?.addEventListener("click", async () => {
        const btn = document.getElementById("qc-skip-btn");
        if (btn) { btn.style.opacity = "0.5"; btn.style.pointerEvents = "none"; }
        try {
            const r = await fetch(API_BASE + "/api/skip-game", { method: "POST" });
            if (!r.ok) {
                const d = await r.json().catch(() => ({}));
                alert(d.detail || `Skip failed (${r.status})`);
            }
        } catch (e) {
            alert("Error: " + e.message);
        } finally {
            setTimeout(() => { if (btn) { btn.style.opacity = ""; btn.style.pointerEvents = ""; } }, 2000);
        }
    });

    document.getElementById('idle-channel-add-btn')?.addEventListener('click', () => {
        const input = document.getElementById('idle-channel-input');
        const val = input.value.trim().toLowerCase();
        if (!val) return;
        const channels = state.settings.idle_channels || [];
        if (!channels.includes(val)) {
            channels.push(val);
            renderIdleChannels([...channels]);
            saveSettings();
        }
        input.value = '';
    });
    document.getElementById('idle-channel-input')?.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') document.getElementById('idle-channel-add-btn').click();
    });

    // Inventory game search dropdown
    const gameSearchInput = document.getElementById('inventory-game-search');
    gameSearchInput.addEventListener('focus', () => {
        showGameDropdown();
    });
    gameSearchInput.addEventListener('input', (e) => {
        renderGameDropdown(e.target.value);
    });
    gameSearchInput.addEventListener('keydown', handleGameSearchKeydown);

    // Click outside to close dropdown
    document.addEventListener('click', (e) => {
        const container = document.querySelector('.game-dropdown-container');
        if (container && !container.contains(e.target) && gameDropdownVisible) {
            closeGameDropdown();
        }
    });

    // System tab buttons
    document.getElementById('system-reload-btn')?.addEventListener('click', async () => {
        const status = document.getElementById('system-status');
        try {
            await fetch(API_BASE + '/api/reload', { method: 'POST' });
            if (status) { status.textContent = 'Campaigns reload triggered.'; status.className = 'system-status success'; }
        } catch (e) {
            if (status) { status.textContent = 'Error: ' + e.message; status.className = 'system-status error'; }
        }
    });

    document.getElementById('system-restart-btn')?.addEventListener('click', async () => {
        const status = document.getElementById('system-status');
        if (!confirm('Restart the miner? PM2 will restart it automatically.')) return;
        try {
            await fetch(API_BASE + '/api/restart', { method: 'POST' });
            if (status) { status.textContent = 'Miner restarting via PM2...'; status.className = 'system-status success'; }
        } catch (e) {
            if (status) { status.textContent = 'Error: ' + e.message; status.className = 'system-status error'; }
        }
    });

    document.getElementById('system-logout-btn')?.addEventListener('click', () => {
        window.location.href = '/__auth_logout';
    });

    document.getElementById('qc-switch-btn')?.addEventListener('click', async () => {
        try {
            const r = await fetch(API_BASE + '/api/idle-watch/switch', { method: 'POST' });
            if (!r.ok) {
                const d = await r.json().catch(() => ({}));
                alert(d.detail || 'Switch failed');
            }
        } catch (e) {
            alert('Error: ' + e.message);
        }
    });

    document.getElementById('qc-idle-btn')?.addEventListener('click', async () => {
        try {
            const r = await fetch(API_BASE + '/api/idle-watch/switch', { method: 'POST' });
            if (!r.ok) {
                const d = await r.json().catch(() => ({}));
                alert(d.detail || 'No idle channels online');
            }
        } catch (e) {
            alert('Error: ' + e.message);
        }
    });

    // Fetch and populate available languages
    fetchAndPopulateLanguages();

    // Fetch and apply translations for the current language
    fetchAndApplyTranslations();

    // Request notification permission
    if ('Notification' in window && Notification.permission === 'default') {
        Notification.requestPermission();
    }

    // Account management
    async function loadAccounts() {
        const listEl = document.getElementById('accounts-list');
        const statusEl = document.getElementById('accounts-status');
        if (!listEl) return;
        try {
            const r = await fetch(API_BASE + '/api/accounts');
            const data = await r.json();
            listEl.replaceChildren();
            if (data.accounts.length === 0) {
                const msg = document.createElement('div');
                msg.textContent = 'No accounts saved yet.';
                msg.style.cssText = 'font-size:0.82rem;color:var(--text-secondary);padding:4px 0;';
                listEl.appendChild(msg);
                return;
            }
            data.accounts.forEach(acc => {
                const row = document.createElement('div');
                row.style.cssText = 'display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border-color);';

                // Name display / inline edit
                const nameEl = document.createElement('span');
                nameEl.textContent = acc.label;
                nameEl.style.cssText = 'flex:1;font-size:0.88rem;font-weight:500;';
                row.appendChild(nameEl);

                // Edit pencil button
                const editBtn = document.createElement('button');
                editBtn.textContent = '✏️';
                editBtn.title = 'Rename';
                editBtn.style.cssText = 'font-size:0.75rem;padding:2px 6px;border-radius:4px;border:1px solid var(--border-color);background:transparent;color:var(--text-secondary);cursor:pointer;';
                editBtn.addEventListener('click', () => {
                    // Replace nameEl with inline input
                    const input = document.createElement('input');
                    input.type = 'text';
                    input.value = acc.label;
                    input.maxLength = 40;
                    input.style.cssText = 'flex:1;background:#18181b;border:1px solid var(--accent-color);border-radius:4px;padding:2px 8px;color:var(--text-primary);font-size:0.85rem;outline:none;';
                    row.replaceChild(input, nameEl);
                    editBtn.style.display = 'none';
                    input.focus();
                    input.select();

                    const save = async () => {
                        const newLabel = input.value.trim();
                        if (!newLabel || newLabel === acc.label) { loadAccounts(); return; }
                        try {
                            const r = await fetch(API_BASE + `/api/accounts/${encodeURIComponent(acc.label)}`, {
                                method: 'PATCH',
                                headers: {'Content-Type':'application/json'},
                                body: JSON.stringify({new_label: newLabel}),
                            });
                            if (!r.ok) throw new Error(await r.text());
                            loadAccounts();
                        } catch (e) {
                            if (statusEl) { statusEl.textContent = 'Error: ' + e.message; statusEl.style.display = 'block'; statusEl.style.color = '#f55'; }
                            loadAccounts();
                        }
                    };
                    input.addEventListener('blur', save);
                    input.addEventListener('keydown', e => { if (e.key === 'Enter') input.blur(); if (e.key === 'Escape') loadAccounts(); });
                });
                row.appendChild(editBtn);

                if (acc.active) {
                    const badge = document.createElement('span');
                    badge.textContent = 'Active';
                    badge.style.cssText = 'font-size:0.72rem;font-weight:600;color:var(--accent-color);background:rgba(145,71,255,0.12);padding:2px 8px;border-radius:20px;';
                    row.appendChild(badge);
                }

                if (!acc.has_cookies) {
                    const warn = document.createElement('span');
                    warn.textContent = 'Not logged in';
                    warn.style.cssText = 'font-size:0.72rem;color:#f90;';
                    row.appendChild(warn);
                }

                if (!acc.active) {
                    const switchBtn = document.createElement('button');
                    switchBtn.textContent = 'Switch';
                    switchBtn.style.cssText = 'font-size:0.78rem;padding:3px 10px;border-radius:4px;border:1px solid var(--border-color);background:transparent;color:var(--text-primary);cursor:pointer;';
                    switchBtn.addEventListener('click', async () => {
                        if (!confirm(`Switch to account "${acc.label}"? The miner will restart.`)) return;
                        try {
                            await fetch(API_BASE + '/api/accounts/switch', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({label: acc.label}) });
                            if (statusEl) { statusEl.textContent = `Switched to ${acc.label}, restarting...`; statusEl.style.display = 'block'; statusEl.style.color = '#3ddc84'; }
                        } catch (e) {
                            if (statusEl) { statusEl.textContent = 'Error: ' + e.message; statusEl.style.display = 'block'; statusEl.style.color = '#f55'; }
                        }
                    });
                    row.appendChild(switchBtn);

                    const delBtn = document.createElement('button');
                    delBtn.textContent = '✕';
                    delBtn.style.cssText = 'font-size:0.78rem;padding:3px 8px;border-radius:4px;border:1px solid var(--border-color);background:transparent;color:#f55;cursor:pointer;';
                    delBtn.addEventListener('click', async () => {
                        if (!confirm(`Delete account "${acc.label}"?`)) return;
                        try {
                            await fetch(API_BASE + `/api/accounts/${encodeURIComponent(acc.label)}`, { method: 'DELETE' });
                            loadAccounts();
                        } catch (e) {
                            if (statusEl) { statusEl.textContent = 'Error: ' + e.message; statusEl.style.display = 'block'; statusEl.style.color = '#f55'; }
                        }
                    });
                    row.appendChild(delBtn);
                }

                listEl.appendChild(row);
            });
        } catch (e) {
            if (statusEl) { statusEl.textContent = 'Error loading accounts.'; statusEl.style.display = 'block'; }
        }
    }

    document.getElementById('add-account-btn')?.addEventListener('click', async () => {
        const labelInput = document.getElementById('new-account-label');
        const statusEl = document.getElementById('accounts-status');
        const label = labelInput?.value.trim();
        if (!label) { alert('Enter a label first.'); return; }
        try {
            const r = await fetch(API_BASE + '/api/accounts/add', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({label}) });
            if (!r.ok) { const d = await r.json().catch(() => ({})); alert(d.detail || 'Error'); return; }
            if (labelInput) labelInput.value = '';
            if (statusEl) { statusEl.textContent = `Account "${label}" added, miner restarting for login...`; statusEl.style.display = 'block'; statusEl.style.color = '#3ddc84'; }
            setTimeout(loadAccounts, 1500);
        } catch (e) {
            alert('Error: ' + e.message);
        }
    });

    // Load accounts when Settings tab is opened (system section now part of settings)
    document.querySelectorAll('.tab-button').forEach(btn => {
        btn.addEventListener('click', () => {
            if (btn.dataset.tab === 'settings') loadAccounts();
        });
    });
});


// ==================== Wanted Items Rendering ====================

// Store last known tree for reorder/remove operations
let _wantedTree = [];

function renderWantedItems(tree) {
    _wantedTree = tree || [];
    const container = document.getElementById('wanted-items-list');
    if (!container) return;
    container.innerHTML = '';

    if (!tree || tree.length === 0) {
        const emptyMsg = state.translations.gui?.wanted?.none || 'No wanted drops queued...';
        container.replaceChildren(makeElement('p', { class: 'empty-message-small' }, emptyMsg));
        return;
    }

    tree.forEach((gameGroup, index) => {
        const totalDrops = gameGroup.campaigns.reduce((n, c) => n + c.drops.length, 0);
        let iconUrl = gameGroup.game_icon
            ? gameGroup.game_icon.replace('{width}', '30').replace('{height}', '40')
            : null;

        const row = document.createElement('div');
        row.className = 'wq-row sortable-item';
        row.draggable = true;
        row.dataset.game = gameGroup.game_name;

        // Drag handle
        const handle = makeElement('span', { class: 'wq-drag-handle drag-handle' }, '⠿');

        // Priority badge
        const badge = makeElement('span', { class: 'wq-badge' }, `#${index + 1}`);

        // Icon
        const iconEl = iconUrl ? makeImageElement(iconUrl, gameGroup.game_name, 'wq-icon') : makeElement('span', { class: 'wq-icon-placeholder' }, '🎮');

        // Name
        const nameEl = makeElement('span', { class: 'wq-name' }, gameGroup.game_name);

        // Drop count
        const countEl = makeElement('span', { class: 'wq-count' }, `${totalDrops} drop${totalDrops !== 1 ? 's' : ''}`);

        // Toggle
        const toggleEl = makeElement('span', { class: 'wq-toggle' }, '▾');

        // Up/Down move buttons (mobile-friendly)
        const moveUpEl = makeElement('button', { class: 'wq-move', title: 'Move up' }, '↑');
        const moveDownEl = makeElement('button', { class: 'wq-move', title: 'Move down' }, '↓');
        if (index === 0) moveUpEl.disabled = true;
        if (index === tree.length - 1) moveDownEl.disabled = true;

        const moveGame = (dir) => {
            const games = state.settings.games_to_watch || [];
            const idx = games.indexOf(gameGroup.game_name);
            if (idx < 0) return;
            const swapIdx = idx + dir;
            if (swapIdx < 0 || swapIdx >= games.length) return;
            const nameA = games[idx], nameB = games[swapIdx];
            [games[idx], games[swapIdx]] = [games[swapIdx], games[idx]];
            state.settings.games_to_watch = [...games];
            saveSettings();
            renderGamesToWatch();
            // Re-sort and re-render the wanted queue immediately
            if (_wantedTree && _wantedTree.length > 0) {
                const order = state.settings.games_to_watch;
                _wantedTree.sort((a, b) => order.indexOf(a.game_name) - order.indexOf(b.game_name));
                renderWantedItems([..._wantedTree]);
            }
        };
        moveUpEl.addEventListener('click', (e) => { e.stopPropagation(); moveGame(-1); });
        moveDownEl.addEventListener('click', (e) => { e.stopPropagation(); moveGame(1); });

        // Remove button
        const removeEl = makeElement('button', { class: 'wq-remove', title: 'Remove from watch list' }, '×');
        removeEl.addEventListener('click', (e) => {
            e.stopPropagation();
            const games = state.settings.games_to_watch || [];
            state.settings.games_to_watch = games.filter(g => g !== gameGroup.game_name);
            saveSettings();
            renderGamesToWatch();
        });

        // Header row click → toggle expand
        const headerEl = makeElement('div', { class: 'wq-header' }, '', el => {
            [handle, badge, iconEl, nameEl, countEl, moveUpEl, moveDownEl, toggleEl, removeEl].forEach(c => el.appendChild(c));
        });

        // Expanded content
        const bodyEl = makeElement('div', { class: 'wq-body' }, '');
        gameGroup.campaigns.forEach(campaign => {
            const campEl = makeElement('div', { class: 'wq-campaign' }, '', el => {
                el.appendChild(makeElement('span', { class: 'wq-campaign-link' }, campaign.name));
                campaign.drops.forEach(drop => {
                    const dropEl = makeElement('div', { class: 'wq-drop' }, '', d => {
                        d.appendChild(makeElement('span', { class: 'wq-drop-name' }, drop.name));
                        (drop.benefits || []).forEach(b => {
                            const benefitEl = document.createElement('span');
                            benefitEl.className = 'wq-benefit';
                            const bName = typeof b === 'string' ? b : b.name;
                            const bImg = typeof b === 'object' && b.image_url ? b.image_url : null;
                            if (bImg) {
                                const img = document.createElement('img');
                                img.src = bImg; img.alt = bName;
                                benefitEl.appendChild(img);
                            }
                            benefitEl.appendChild(document.createTextNode(bName));
                            d.appendChild(benefitEl);
                        });
                    });
                    dropEl.addEventListener('click', (e) => { e.stopPropagation(); showRewardModal(drop); });
                    el.appendChild(dropEl);
                });
            });
            bodyEl.appendChild(campEl);
        });

        // Collapse by default unless first item
        if (index !== 0) {
            bodyEl.style.display = 'none';
            toggleEl.textContent = '▸';
        }

        headerEl.addEventListener('click', (e) => {
            if (e.target === removeEl || e.target === handle) return;
            const open = bodyEl.style.display !== 'none';
            bodyEl.style.display = open ? 'none' : '';
            toggleEl.textContent = open ? '▸' : '▾';
        });

        row.appendChild(headerEl);
        row.appendChild(bodyEl);

        // Drag handlers
        row.addEventListener('dragstart', handleDragStart);
        row.addEventListener('dragover', handleDragOver);
        row.addEventListener('dragend', handleWantedDragEnd);

        container.appendChild(row);
    });
}

function showRewardModal(drop) {
    document.getElementById('wq-reward-modal')?.remove();
    const overlay = document.createElement('div');
    overlay.id = 'wq-reward-modal';
    overlay.className = 'wq-modal-overlay';
    overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });

    const modal = document.createElement('div');
    modal.className = 'wq-modal';

    const closeBtn = document.createElement('button');
    closeBtn.className = 'wq-modal-close';
    closeBtn.textContent = '×';
    closeBtn.addEventListener('click', () => overlay.remove());
    modal.appendChild(closeBtn);

    const title = document.createElement('div');
    title.className = 'wq-modal-title';
    title.textContent = drop.name;
    modal.appendChild(title);

    const benefitsList = document.createElement('div');
    benefitsList.className = 'wq-modal-benefits';
    (drop.benefits || []).forEach(b => {
        const bName = typeof b === 'string' ? b : b.name;
        const bImg = typeof b === 'object' && b.image_url ? b.image_url : null;
        const item = document.createElement('div');
        item.className = 'wq-modal-benefit';
        if (bImg) {
            const img = document.createElement('img');
            img.src = bImg; img.alt = bName;
            item.appendChild(img);
        }
        const nameEl = document.createElement('span');
        nameEl.className = 'wq-modal-benefit-name';
        nameEl.textContent = bName;
        item.appendChild(nameEl);
        benefitsList.appendChild(item);
    });
    modal.appendChild(benefitsList);
    overlay.appendChild(modal);
    document.body.appendChild(overlay);
}

function handleWantedDragEnd(e) {
    e.target.classList.remove('dragging');
    const container = document.getElementById('wanted-items-list');
    if (!container) return;
    const items = container.querySelectorAll('.sortable-item');
    const newOrder = Array.from(items).map(item => item.dataset.game);
    // Update full games_to_watch preserving any games not in wanted list
    const current = state.settings.games_to_watch || [];
    const wantedSet = new Set(newOrder);
    const extras = current.filter(g => !wantedSet.has(g));
    state.settings.games_to_watch = [...newOrder, ...extras];
    saveSettings();
    renderGamesToWatch();
}

// ==================== DOM Utilities ====================

const TRUSTED_HELP_LINKS = new Set(['https://www.twitch.tv/drops/campaigns']);

/**
 * @param {string} tag
 * @param {Record<string, string|number|boolean>} attrs
 * @param {string|number|null} text
 * @param {(el: HTMLElement) => void|null} callback
 */
function makeElement(tag, attrs = {}, text = null, callback = null) {
    const el = document.createElement(tag);
    Object.entries(attrs).forEach(([key, value]) => el.setAttribute(key, String(value)));
    if (text !== null && text !== undefined) {
        el.textContent = String(text);
    }
    if (callback) {
        callback(el);
    }
    return el;
}

function makeImageElement(src, alt, className) {
    const image = makeElement('img', { src, alt, class: className });
    image.onerror = () => {
        image.style.display = 'none';
    };
    return image;
}

function makeHelpList(tag, items) {
    return makeElement(tag, {}, null, list => {
        items.forEach(item => {
            list.appendChild(makeElement('li', {}, null, li => appendTrustedHelpContent(li, item)));
        });
    });
}

function appendTrustedHelpContent(parent, text) {
    const source = String(text);
    const tagPattern = /<(code|strong|a)\b([^>]*)>(.*?)<\/\1>/gi;
    let lastIndex = 0;
    let match;
    let matched = false;

    while ((match = tagPattern.exec(source)) !== null) {
        matched = true;
        if (match.index > lastIndex) {
            parent.appendChild(document.createTextNode(source.slice(lastIndex, match.index)));
        }
        const tagName = match[1].toLowerCase();
        const inner = match[3];
        if (tagName === 'a') {
            const hrefMatch = match[2].match(/href=(["'])(https?:\/\/[^"']+)\1/i);
            if (hrefMatch && TRUSTED_HELP_LINKS.has(hrefMatch[2])) {
                parent.appendChild(makeElement('a', { href: hrefMatch[2], target: '_blank', rel: 'noopener noreferrer' }, inner));
            } else {
                parent.appendChild(document.createTextNode(inner));
            }
        } else {
            parent.appendChild(makeElement(tagName, {}, inner));
        }
        lastIndex = tagPattern.lastIndex;
    }

    if (!matched) {
        parent.textContent = source;
        return;
    }

    if (lastIndex < source.length) {
        parent.appendChild(document.createTextNode(source.slice(lastIndex)));
    }
}
