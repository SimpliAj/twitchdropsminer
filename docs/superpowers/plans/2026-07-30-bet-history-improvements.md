# Bet History Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show HH:MM + selected outcome on the bet history table, paginate both the bet and drop history tables, and stop bets from getting stuck `PENDING` forever.

**Architecture:** Backend: two independent stale-`PENDING`→`UNKNOWN` triggers (an offline-channel hook and a 24h age sweep) sharing one predicate-based flip helper in `prediction_service.py`, plus relaxing `_record_result`'s match so a late real resolution can still override an `UNKNOWN` entry. Frontend: vanilla-JS DOM construction (no framework, no `innerHTML`, matching existing `app.js` style) for the new table column and a small shared pager component reused by both history tables, slicing an already-fetched (server-capped-at-500) list client-side.

**Tech Stack:** Python 3.12 / FastAPI backend, vanilla JS frontend (`src/web/app.js`, `src/web/index.html`), `unittest`/`pytest` for tests.

## Global Constraints

- DRY and OOP required for backend code (`AGENTS.md`).
- Frontend DOM construction must use `createElement`/`textContent`, never `innerHTML` with dynamic content (`AGENTS.md`).
- Always add unit tests for backend changes (`AGENTS.md`).
- No co-author trailer in commits (per user instruction this session).
- Don't touch unrelated pre-existing failing tests (10 known pre-existing failures on `main`, confirmed unrelated to this work).

---

### Task 1: Stale-pending core logic (`prediction_service.py`)

**Files:**
- Modify: `src/services/prediction_service.py:16-45` (add constant + helper functions after `_load_overrides`)
- Modify: `src/services/prediction_service.py:324-325` (relax `_record_result` match condition)
- Modify: `src/services/prediction_service.py:371-376` (add `mark_channel_stale_pending` method after `get_history`)
- Test: `tests/test_stale_pending.py`

**Interfaces:**
- Produces: `sweep_stale_pending_by_age(hist: list, max_age_hours: float = 24.0) -> bool` (module-level, mutates `hist` in place, returns whether anything changed) — consumed by Task 2's `/api/predictions` endpoint.
- Produces: `PredictionService.mark_channel_stale_pending(self, channel_name: str) -> None` — consumed by Task 2's `on_channel_update` hook.
- Produces: `_flip_pending_matching(hist: list, predicate) -> bool` (module-level, shared by the two above).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_stale_pending.py`:

```python
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.services.prediction_service import PredictionService, sweep_stale_pending_by_age


def _entry(channel="streamer_a", result="PENDING", hours_ago=1, event_id="evt-1"):
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    return {"event_id": event_id, "ts": ts, "channel": channel, "result": result}


class TestSweepStalePendingByAge(unittest.TestCase):
    def test_old_pending_becomes_unknown(self):
        hist = [_entry(hours_ago=25)]
        self.assertTrue(sweep_stale_pending_by_age(hist))
        self.assertEqual(hist[0]["result"], "UNKNOWN")

    def test_recent_pending_stays_pending(self):
        hist = [_entry(hours_ago=1)]
        self.assertFalse(sweep_stale_pending_by_age(hist))
        self.assertEqual(hist[0]["result"], "PENDING")

    def test_non_pending_entries_untouched(self):
        hist = [_entry(hours_ago=25, result="WIN")]
        self.assertFalse(sweep_stale_pending_by_age(hist))
        self.assertEqual(hist[0]["result"], "WIN")

    def test_malformed_timestamp_is_not_treated_as_stale(self):
        hist = [{"event_id": "e1", "ts": "not-a-timestamp", "channel": "a", "result": "PENDING"}]
        self.assertFalse(sweep_stale_pending_by_age(hist))
        self.assertEqual(hist[0]["result"], "PENDING")


class TestMarkChannelStalePending(unittest.TestCase):
    def test_flips_only_matching_channel_pending_entries(self):
        hist = [
            _entry(channel="streamer_a", result="PENDING", event_id="e1"),
            _entry(channel="streamer_b", result="PENDING", event_id="e2"),
            _entry(channel="streamer_a", result="WIN", event_id="e3"),
        ]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "predictions_history.json"
            p.write_text(json.dumps(hist))
            with patch("src.services.prediction_service._get_predictions_file", return_value=p):
                svc = PredictionService(MagicMock())
                svc.mark_channel_stale_pending("streamer_a")
            result = json.loads(p.read_text())

        by_id = {e["event_id"]: e for e in result}
        self.assertEqual(by_id["e1"]["result"], "UNKNOWN")
        self.assertEqual(by_id["e2"]["result"], "PENDING")
        self.assertEqual(by_id["e3"]["result"], "WIN")

    def test_channel_matching_is_case_insensitive(self):
        hist = [_entry(channel="streamer_a", result="PENDING", event_id="e1")]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "predictions_history.json"
            p.write_text(json.dumps(hist))
            with patch("src.services.prediction_service._get_predictions_file", return_value=p):
                svc = PredictionService(MagicMock())
                svc.mark_channel_stale_pending("Streamer_A")
            result = json.loads(p.read_text())

        self.assertEqual(result[0]["result"], "UNKNOWN")


class TestRecordResultOverridesUnknown(unittest.IsolatedAsyncioTestCase):
    async def test_late_result_overrides_unknown_entry(self):
        hist = [{
            "event_id": "evt-9",
            "ts": datetime.now(timezone.utc).isoformat(),
            "channel": "streamer_a",
            "result": "UNKNOWN",
            "outcome_id": "o1",
            "points_bet": 100,
            "points_won": 0,
        }]
        twitch = MagicMock()
        twitch.settings.discord_webhook_points = ""
        twitch.gui._broadcaster.emit = AsyncMock()
        svc = PredictionService(twitch)
        event = {"winning_outcome_id": "o1", "outcomes": [{"id": "o1", "total_points": 100}]}

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "predictions_history.json"
            p.write_text(json.dumps(hist))
            with patch("src.services.prediction_service._get_predictions_file", return_value=p):
                await svc._record_result("evt-9", event, "streamer_a")
            result = json.loads(p.read_text())

        self.assertEqual(result[0]["result"], "WIN")

    async def test_pending_entry_still_resolves_normally(self):
        hist = [{
            "event_id": "evt-10",
            "ts": datetime.now(timezone.utc).isoformat(),
            "channel": "streamer_a",
            "result": "PENDING",
            "outcome_id": "o2",
            "points_bet": 50,
            "points_won": 0,
        }]
        twitch = MagicMock()
        twitch.settings.discord_webhook_points = ""
        twitch.gui._broadcaster.emit = AsyncMock()
        svc = PredictionService(twitch)
        event = {"winning_outcome_id": "o1", "outcomes": [{"id": "o1", "total_points": 100}]}

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "predictions_history.json"
            p.write_text(json.dumps(hist))
            with patch("src.services.prediction_service._get_predictions_file", return_value=p):
                await svc._record_result("evt-10", event, "streamer_a")
            result = json.loads(p.read_text())

        self.assertEqual(result[0]["result"], "LOSE")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_stale_pending.py -v`
Expected: FAIL with `ImportError: cannot import name 'sweep_stale_pending_by_age'` (and `AttributeError` on `mark_channel_stale_pending` once the import error is fixed).

- [ ] **Step 3: Implement the core logic**

In `src/services/prediction_service.py`, after the existing `_load_overrides()` function (around line 45, right before `class PredictionService:`), add:

```python
STALE_PENDING_HOURS = 24


def _flip_pending_matching(hist: list, predicate) -> bool:
    """Flips PENDING entries matching predicate to UNKNOWN in place. Returns True if any changed."""
    changed = False
    for entry in hist:
        if entry.get("result") == "PENDING" and predicate(entry):
            entry["result"] = "UNKNOWN"
            changed = True
    return changed


def sweep_stale_pending_by_age(hist: list, max_age_hours: float = STALE_PENDING_HOURS) -> bool:
    """Flips PENDING entries older than max_age_hours to UNKNOWN in place. Returns True if any changed."""
    from datetime import datetime, timezone
    cutoff = datetime.now(timezone.utc).timestamp() - max_age_hours * 3600

    def _is_stale(entry: dict) -> bool:
        try:
            return datetime.fromisoformat(entry["ts"]).timestamp() < cutoff
        except Exception:
            return False

    return _flip_pending_matching(hist, _is_stale)
```

In `_record_result` (around line 324-325), change:

```python
            if entry.get("event_id") == event_id and entry.get("result") == "PENDING":
```

to:

```python
            if entry.get("event_id") == event_id and entry.get("result") in ("PENDING", "UNKNOWN"):
```

After the existing `get_history` method at the end of the class (around line 371-376), add:

```python
    def mark_channel_stale_pending(self, channel_name: str) -> None:
        """Flips this channel's still-PENDING bets to UNKNOWN. Called when the
        channel goes offline — Twitch predictions can resolve around stream end,
        and the miner constantly channel-hops chasing drops, so the RESOLVED
        websocket event that would normally flip PENDING -> WIN/LOSE is often
        missed once we're no longer subscribed to this channel's PubSub topic."""
        p = _get_predictions_file()
        try:
            hist = _json.loads(p.read_text()) if p.exists() else []
        except Exception:
            return
        changed = _flip_pending_matching(hist, lambda e: e.get("channel") == channel_name.lower())
        if changed:
            try:
                p.write_text(_json.dumps(hist, indent=2))
            except Exception:
                pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_stale_pending.py -v`
Expected: All PASS.

- [ ] **Step 5: Run the full suite to confirm no regressions**

Run: `python3 -m pytest tests/ -q`
Expected: Same 10 pre-existing unrelated failures as on `main` (verify with `git stash` if unsure which ones are pre-existing), all else passing, plus the new tests from this task.

- [ ] **Step 6: Commit**

```bash
git add src/services/prediction_service.py tests/test_stale_pending.py
git commit -m "feat: add stale-PENDING-bet sweep and late-result override safety"
```

---

### Task 2: Wire stale-pending triggers into the app

**Files:**
- Modify: `src/services/message_handlers.py:263-269` (call `mark_channel_stale_pending` on channel OFFLINE)
- Modify: `src/web/app.py:1581-1591` (`/api/predictions` applies `sweep_stale_pending_by_age` before returning)
- Test: `tests/test_stale_pending.py` (append two more test classes)

**Interfaces:**
- Consumes: `PredictionService.mark_channel_stale_pending(channel_name: str) -> None` and `sweep_stale_pending_by_age(hist: list, max_age_hours: float = 24.0) -> bool` from Task 1.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_stale_pending.py`:

```python
from src.services.message_handlers import MessageHandlerService


class TestOnChannelUpdateStalePendingHook(unittest.TestCase):
    def test_channel_going_offline_marks_stale_pending(self):
        twitch = MagicMock()
        twitch.watching_channel.get_with_default.return_value = None
        service = MessageHandlerService(twitch)

        channel = MagicMock()
        channel.name = "streamer_a"
        stream_before = MagicMock()

        service.on_channel_update(channel, stream_before, None)

        twitch._prediction_service.mark_channel_stale_pending.assert_called_once_with("streamer_a")

    def test_channel_staying_online_does_not_mark_stale_pending(self):
        twitch = MagicMock()
        twitch.watching_channel.get_with_default.return_value = None
        twitch.can_watch.return_value = False
        service = MessageHandlerService(twitch)

        channel = MagicMock()
        channel.name = "streamer_a"
        stream = MagicMock()

        service.on_channel_update(channel, stream, stream)

        twitch._prediction_service.mark_channel_stale_pending.assert_not_called()

    def test_channel_coming_online_does_not_mark_stale_pending(self):
        twitch = MagicMock()
        twitch.watching_channel.get_with_default.return_value = None
        twitch.can_watch.return_value = False
        service = MessageHandlerService(twitch)

        channel = MagicMock()
        channel.name = "streamer_a"

        service.on_channel_update(channel, None, MagicMock())

        twitch._prediction_service.mark_channel_stale_pending.assert_not_called()


class TestPredictionsEndpointSweep(unittest.IsolatedAsyncioTestCase):
    async def test_endpoint_sweeps_and_persists_stale_pending(self):
        import src.web.app as app_module

        hist = [{
            "event_id": "evt-1",
            "ts": (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(),
            "channel": "streamer_a",
            "result": "PENDING",
            "outcome_chosen": "Blue",
            "points_bet": 100,
            "points_won": 0,
        }]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "predictions_history.json"
            p.write_text(json.dumps(hist))
            with patch("src.services.prediction_service._get_predictions_file", return_value=p):
                result = await app_module.get_predictions()
            persisted = json.loads(p.read_text())

        self.assertEqual(result["predictions"][0]["result"], "UNKNOWN")
        self.assertEqual(persisted[0]["result"], "UNKNOWN")

    async def test_endpoint_leaves_fresh_pending_alone(self):
        import src.web.app as app_module

        hist = [{
            "event_id": "evt-2",
            "ts": datetime.now(timezone.utc).isoformat(),
            "channel": "streamer_a",
            "result": "PENDING",
            "outcome_chosen": "Red",
            "points_bet": 50,
            "points_won": 0,
        }]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "predictions_history.json"
            p.write_text(json.dumps(hist))
            with patch("src.services.prediction_service._get_predictions_file", return_value=p):
                result = await app_module.get_predictions()

        self.assertEqual(result["predictions"][0]["result"], "PENDING")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_stale_pending.py -v -k "OnChannelUpdate or EndpointSweep"`
Expected: FAIL — `mark_channel_stale_pending` never called (hook not wired), endpoint doesn't sweep (result stays `PENDING`).

- [ ] **Step 3: Wire the hook and the endpoint sweep**

In `src/services/message_handlers.py`, the "Channel going from ONLINE to OFFLINE" branch (around line 263-269) currently reads:

```python
        # Channel going from ONLINE to OFFLINE
        elif stream_before is not None and stream_after is None:
            if is_watching_this:
                self._twitch.print(_.t["status"]["goes_offline"].format(channel=channel.name))
                self._twitch.change_state(State.CHANNEL_SWITCH)
            else:
                logger.info(f"{channel.name} goes OFFLINE")
```

Change it to:

```python
        # Channel going from ONLINE to OFFLINE
        elif stream_before is not None and stream_after is None:
            if is_watching_this:
                self._twitch.print(_.t["status"]["goes_offline"].format(channel=channel.name))
                self._twitch.change_state(State.CHANNEL_SWITCH)
            else:
                logger.info(f"{channel.name} goes OFFLINE")
            self._twitch._prediction_service.mark_channel_stale_pending(channel.name)
```

In `src/web/app.py`, the `/api/predictions` endpoint (around line 1581-1591) currently reads:

```python
@app.get("/api/predictions")
async def get_predictions():
    """Return predictions history."""
    from src.services.prediction_service import _get_predictions_file
    import json as _j
    p = _get_predictions_file()
    try:
        hist = _j.loads(p.read_text()) if p.exists() else []
    except Exception:
        hist = []
    return {"predictions": list(reversed(hist[-200:]))}
```

Change it to:

```python
@app.get("/api/predictions")
async def get_predictions():
    """Return predictions history."""
    from src.services.prediction_service import _get_predictions_file, sweep_stale_pending_by_age
    import json as _j
    p = _get_predictions_file()
    try:
        hist = _j.loads(p.read_text()) if p.exists() else []
    except Exception:
        hist = []
    if sweep_stale_pending_by_age(hist):
        try:
            p.write_text(_j.dumps(hist, indent=2))
        except Exception:
            pass
    return {"predictions": list(reversed(hist[-200:]))}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_stale_pending.py -v`
Expected: All PASS (both this task's and Task 1's classes).

- [ ] **Step 5: Run the full suite to confirm no regressions**

Run: `python3 -m pytest tests/ -q`
Expected: Same pre-existing failures as Task 1's Step 5, nothing new broken.

- [ ] **Step 6: Commit**

```bash
git add src/services/message_handlers.py src/web/app.py tests/test_stale_pending.py
git commit -m "feat: wire stale-pending sweep into channel-offline hook and predictions endpoint"
```

---

### Task 3: Bet table — HH:MM timestamp + Selection column

**Files:**
- Modify: `src/web/index.html:727-737` (add "Selection" table header)
- Modify: `src/web/app.js:3466-3474` (render time-of-day + selection cell)

**Interfaces:**
- Consumes: `p.outcome_chosen` (already present in every `predictions_history.json` entry, written by `_save_pending_bet` in `prediction_service.py`) and `p.ts` (ISO timestamp).

- [ ] **Step 1: Add the table header**

In `src/web/index.html`, the Predictions History table header (around line 727-737) currently reads:

```html
                    <table style="width:100%;border-collapse:collapse;font-size:.85rem">
                        <thead>
                            <tr style="color:#adadb8;text-align:left">
                                <th style="padding:6px 8px">Time</th>
                                <th style="padding:6px 8px">Channel</th>
                                <th style="padding:6px 8px">Prediction</th>
                                <th style="padding:6px 8px">Bet</th>
                                <th style="padding:6px 8px">Result</th>
                                <th style="padding:6px 8px">Won</th>
                            </tr>
                        </thead>
                        <tbody id="pred-tbody"></tbody>
                    </table>
```

Change it to:

```html
                    <table style="width:100%;border-collapse:collapse;font-size:.85rem">
                        <thead>
                            <tr style="color:#adadb8;text-align:left">
                                <th style="padding:6px 8px">Time</th>
                                <th style="padding:6px 8px">Channel</th>
                                <th style="padding:6px 8px">Prediction</th>
                                <th style="padding:6px 8px">Selection</th>
                                <th style="padding:6px 8px">Bet</th>
                                <th style="padding:6px 8px">Result</th>
                                <th style="padding:6px 8px">Won</th>
                            </tr>
                        </thead>
                        <tbody id="pred-tbody"></tbody>
                    </table>
                    <div id="pred-pager"></div>
```

(The `#pred-pager` div is unused until Task 4, but adding it now avoids touching this block twice.)

- [ ] **Step 2: Render the new time format and Selection cell**

In `src/web/app.js`, inside `loadPredictions` (around line 3466-3474), the row-building code currently reads:

```javascript
        preds.slice(0, 100).forEach(p => {
            const color = p.result === "WIN" ? "#00b368" : p.result === "LOSE" ? "#eb4a4a" : "#adadb8";
            const tr = document.createElement("tr");
            tr.style.borderTop = "1px solid #2d2d35";
            const netWon = p.result === "WIN" ? (p.points_won || 0) - (p.points_bet || 0) : 0;
            const wonText = p.result === "WIN" ? `+${netWon.toLocaleString()}` : p.result === "LOSE" ? `−${(p.points_bet || 0).toLocaleString()}` : "—";
            [{ text: p.ts ? new Date(p.ts).toLocaleDateString() : "—", style: "color:#adadb8" }, { text: p.channel || "—" }, { text: p.title ? p.title.slice(0, 40) : "—" }, { text: (p.points_bet || 0).toLocaleString() }, { text: p.result || "PENDING", style: `color:${color};font-weight:600` }, { text: wonText, style: `color:${color}` }]
                .forEach(c => { const td = document.createElement("td"); td.style.padding = "5px 8px"; if (c.style) td.style.cssText += c.style; td.textContent = c.text; tr.appendChild(td); });
            tbody.appendChild(tr);
        });
```

Change the row-cell array to add the time-of-day to the first cell and a new Selection cell:

```javascript
        preds.slice(0, 100).forEach(p => {
            const color = p.result === "WIN" ? "#00b368" : p.result === "LOSE" ? "#eb4a4a" : "#adadb8";
            const tr = document.createElement("tr");
            tr.style.borderTop = "1px solid #2d2d35";
            const netWon = p.result === "WIN" ? (p.points_won || 0) - (p.points_bet || 0) : 0;
            const wonText = p.result === "WIN" ? `+${netWon.toLocaleString()}` : p.result === "LOSE" ? `−${(p.points_bet || 0).toLocaleString()}` : "—";
            const tsText = p.ts
                ? `${new Date(p.ts).toLocaleDateString("de-AT")} ${new Date(p.ts).toLocaleTimeString("de-AT", { hour: "2-digit", minute: "2-digit" })}`
                : "—";
            [{ text: tsText, style: "color:#adadb8;white-space:nowrap" }, { text: p.channel || "—" }, { text: p.title ? p.title.slice(0, 40) : "—" }, { text: p.outcome_chosen || "—" }, { text: (p.points_bet || 0).toLocaleString() }, { text: p.result || "PENDING", style: `color:${color};font-weight:600` }, { text: wonText, style: `color:${color}` }]
                .forEach(c => { const td = document.createElement("td"); td.style.padding = "5px 8px"; if (c.style) td.style.cssText += c.style; td.textContent = c.text; tr.appendChild(td); });
            tbody.appendChild(tr);
        });
```

(This step will be revisited in Task 4 to switch `preds.slice(0, 100)` to page-based slicing — leave the `100` cap as-is for now, it's still correct behavior on its own.)

- [ ] **Step 3: Manually verify in the browser**

The miner is running locally via PM2 (`twitchdrops`, port 8080; `twitchdrops2`, port 8082). After editing, restart the process serving the account with existing prediction history (check which port has `predictions_history.json` entries) and use the Playwright browser tool to navigate to it, open the Analytics tab, and confirm:
- Time column shows date + HH:MM.
- New Selection column shows the picked outcome (e.g. a team/color name), not blank, for existing entries.
- No console errors.

- [ ] **Step 4: Commit**

```bash
git add src/web/index.html src/web/app.js
git commit -m "feat: show bet timestamp-with-time and selected outcome in bet history"
```

---

### Task 4: Shared pagination for bet history and drop history

**Files:**
- Modify: `src/web/index.html:686-687` (add `#history-pager` div after `#history-list`)
- Modify: `src/web/app.js:557-563` (`loadDropHistory`: fetch once, reset page, render)
- Modify: `src/web/app.js:565-649` (`renderDropHistory`: slice to current page, render pager)
- Modify: `src/web/app.js:3448-3477` (split `loadPredictions` into `loadPredictions` (fetch, reset page) + new `renderPredictions` (slice to current page, render pager))
- Modify: `src/web/app.js` (new shared `renderPager` helper, placed after `renderDropHistory`)

**Interfaces:**
- Produces: `renderPager(containerEl: HTMLElement, totalItems: number, pageSize: number, currentPage: number, onPageChange: (page: number) => void) -> void` — pure DOM-rendering helper, no fetch, used by both tables.
- Consumes: Task 3's modified `loadPredictions` (the version with the `tsText`/Selection-cell row code) — this task replaces that function's body wholesale, splitting it into `loadPredictions` (fetch) + a new `renderPredictions` (render), carrying Task 3's row-cell code into the new `renderPredictions`.

- [ ] **Step 1: Add the drop-history pager container**

In `src/web/index.html`, around line 686-687:

```html
                    <div id="history-empty" style="color:#adadb8;font-size:0.9rem;padding:12px 0;">No drops claimed yet.</div>
                    <div id="history-list" style="display:none;"></div>
```

Change to:

```html
                    <div id="history-empty" style="color:#adadb8;font-size:0.9rem;padding:12px 0;">No drops claimed yet.</div>
                    <div id="history-list" style="display:none;"></div>
                    <div id="history-pager"></div>
```

(`#pred-pager` was already added in Task 3.)

- [ ] **Step 2: Add the shared pager helper**

In `src/web/app.js`, immediately after the closing brace of `renderDropHistory` (around line 649, right before the `// ==================== Stats Widget ====================` comment), add:

```javascript
const HISTORY_PAGE_SIZE = 50;

function renderPager(containerEl, totalItems, pageSize, currentPage, onPageChange) {
    if (!containerEl) return;
    containerEl.replaceChildren();
    const totalPages = Math.max(1, Math.ceil(totalItems / pageSize));
    if (totalPages <= 1) return;
    containerEl.style.cssText = 'display:flex;align-items:center;justify-content:center;gap:10px;padding:10px 0;font-size:0.8rem;color:var(--text-secondary,#adadb8);';

    const prevBtn = document.createElement('button');
    prevBtn.textContent = '‹ Prev';
    prevBtn.className = 'secondary-btn';
    prevBtn.disabled = currentPage <= 1;
    prevBtn.addEventListener('click', () => onPageChange(currentPage - 1));

    const label = document.createElement('span');
    label.textContent = `Page ${currentPage} of ${totalPages}`;

    const nextBtn = document.createElement('button');
    nextBtn.textContent = 'Next ›';
    nextBtn.className = 'secondary-btn';
    nextBtn.disabled = currentPage >= totalPages;
    nextBtn.addEventListener('click', () => onPageChange(currentPage + 1));

    containerEl.appendChild(prevBtn);
    containerEl.appendChild(label);
    containerEl.appendChild(nextBtn);
}
```

- [ ] **Step 3: Paginate drop history**

In `src/web/app.js`, `loadDropHistory` (around line 557-563) currently reads:

```javascript
async function loadDropHistory() {
    try {
        const resp = await fetch(API_BASE + "/api/drops-history");
        const data = await resp.json();
        renderDropHistory(data);
    } catch (e) { console.error("Failed to load drop history", e); }
}
```

Change to:

```javascript
let dropHistoryFullList = [];
let dropHistoryPage = 1;

async function loadDropHistory() {
    try {
        const resp = await fetch(API_BASE + "/api/drops-history");
        const data = await resp.json();
        dropHistoryFullList = data || [];
        dropHistoryPage = 1;
        renderDropHistory(dropHistoryFullList);
    } catch (e) { console.error("Failed to load drop history", e); }
}
```

Then in `renderDropHistory` (around line 565-592), the function currently groups the *entire* `drops` array by date. Change it to group only the current page's slice, and render the pager against the full total. The function signature and empty-state handling stay the same; only the part after the summary line changes. Currently:

```javascript
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
```

Change the `// Group by date` line onward to operate on a page slice, and add the pager call at the end of the function:

```javascript
    // Group by date (current page only)
    const pageStart = (dropHistoryPage - 1) * HISTORY_PAGE_SIZE;
    const pageDrops = drops.slice(pageStart, pageStart + HISTORY_PAGE_SIZE);
    const groups = new Map();
    pageDrops.forEach(drop => {
```

(The rest of the function body — the `groups.forEach(...)` rendering loop — stays exactly as-is, since it already iterates whatever's in `groups`.)

At the very end of `renderDropHistory`, right before its closing `}` (after the `groups.forEach(...)` block), add:

```javascript

    renderPager(document.getElementById("history-pager"), drops.length, HISTORY_PAGE_SIZE, dropHistoryPage, (page) => {
        dropHistoryPage = page;
        renderDropHistory(dropHistoryFullList);
    });
```

Also add, right after the early-return empty-state block (so the pager clears when there's no history):

```javascript
    if (!drops || drops.length === 0) {
        emptyEl.style.display = "block";
        listEl.style.display = "none";
        if (summaryEl) summaryEl.textContent = "";
        renderPager(document.getElementById("history-pager"), 0, HISTORY_PAGE_SIZE, 1, () => {});
        return;
    }
```

(replacing the existing 4-line empty-state block, which is otherwise unchanged.)

- [ ] **Step 4: Paginate bet history**

`renderDropHistory` and `loadDropHistory` are already two separate functions (fetch+reset-page vs. render-a-given-list), so the pager's `onPageChange` can call the render function directly without re-fetching or resetting the page. `loadPredictions` (as modified in Task 3) is a single function that both fetches *and* renders — calling it again from `onPageChange` would re-fetch **and** reset `predHistoryPage` back to 1, undoing the click. So this step splits it into `loadPredictions` (fetch, reset page, delegate to render) and a new `renderPredictions` (render a given list at the current page), mirroring the drop-history split.

`loadPredictions` (modified in Task 3, around line 3448-3477) currently reads in full:

```javascript
async function loadPredictions() {
    try {
        const resp = await fetch(API_BASE + "/api/predictions");
        const data = await resp.json();
        const preds = data.predictions || [];
        const wins = preds.filter(p => p.result === "WIN").length;
        const losses = preds.filter(p => p.result === "LOSE").length;
        const net = preds.filter(p => ["WIN", "LOSE"].includes(p.result)).reduce((s, p) => s + (p.points_won || 0) - (p.points_bet || 0), 0);
        const winRate = wins + losses > 0 ? Math.round(wins / (wins + losses) * 100) : 0;
        const summaryEl = document.getElementById("pred-summary");
        if (summaryEl) {
            summaryEl.replaceChildren();
            [{ label: "Total", value: preds.length }, { label: "Win Rate", value: `${winRate}%` }, { label: "Net", value: `${net >= 0 ? "+" : ""}${net.toLocaleString()} pts`, color: net >= 0 ? "#00b368" : "#eb4a4a" }]
                .forEach(c => { const div = document.createElement("div"); div.className = "stat-card"; if (c.color) div.style.color = c.color; div.textContent = `${c.label}: ${c.value}`; summaryEl.appendChild(div); });
        }
        const tbody = document.getElementById("pred-tbody");
        if (!tbody) return;
        tbody.replaceChildren();
        preds.slice(0, 100).forEach(p => {
            const color = p.result === "WIN" ? "#00b368" : p.result === "LOSE" ? "#eb4a4a" : "#adadb8";
            const tr = document.createElement("tr");
            tr.style.borderTop = "1px solid #2d2d35";
            const netWon = p.result === "WIN" ? (p.points_won || 0) - (p.points_bet || 0) : 0;
            const wonText = p.result === "WIN" ? `+${netWon.toLocaleString()}` : p.result === "LOSE" ? `−${(p.points_bet || 0).toLocaleString()}` : "—";
            const tsText = p.ts
                ? `${new Date(p.ts).toLocaleDateString("de-AT")} ${new Date(p.ts).toLocaleTimeString("de-AT", { hour: "2-digit", minute: "2-digit" })}`
                : "—";
            [{ text: tsText, style: "color:#adadb8;white-space:nowrap" }, { text: p.channel || "—" }, { text: p.title ? p.title.slice(0, 40) : "—" }, { text: p.outcome_chosen || "—" }, { text: (p.points_bet || 0).toLocaleString() }, { text: p.result || "PENDING", style: `color:${color};font-weight:600` }, { text: wonText, style: `color:${color}` }]
                .forEach(c => { const td = document.createElement("td"); td.style.padding = "5px 8px"; if (c.style) td.style.cssText += c.style; td.textContent = c.text; tr.appendChild(td); });
            tbody.appendChild(tr);
        });
    } catch(e) {}
}
```

Replace the whole function with:

```javascript
let predHistoryFullList = [];
let predHistoryPage = 1;

async function loadPredictions() {
    try {
        const resp = await fetch(API_BASE + "/api/predictions");
        const data = await resp.json();
        predHistoryFullList = data.predictions || [];
        predHistoryPage = 1;
        renderPredictions(predHistoryFullList);
    } catch(e) {}
}

function renderPredictions(preds) {
    const wins = preds.filter(p => p.result === "WIN").length;
    const losses = preds.filter(p => p.result === "LOSE").length;
    const net = preds.filter(p => ["WIN", "LOSE"].includes(p.result)).reduce((s, p) => s + (p.points_won || 0) - (p.points_bet || 0), 0);
    const winRate = wins + losses > 0 ? Math.round(wins / (wins + losses) * 100) : 0;
    const summaryEl = document.getElementById("pred-summary");
    if (summaryEl) {
        summaryEl.replaceChildren();
        [{ label: "Total", value: preds.length }, { label: "Win Rate", value: `${winRate}%` }, { label: "Net", value: `${net >= 0 ? "+" : ""}${net.toLocaleString()} pts`, color: net >= 0 ? "#00b368" : "#eb4a4a" }]
            .forEach(c => { const div = document.createElement("div"); div.className = "stat-card"; if (c.color) div.style.color = c.color; div.textContent = `${c.label}: ${c.value}`; summaryEl.appendChild(div); });
    }
    const tbody = document.getElementById("pred-tbody");
    if (!tbody) return;
    tbody.replaceChildren();
    const pageStart = (predHistoryPage - 1) * HISTORY_PAGE_SIZE;
    preds.slice(pageStart, pageStart + HISTORY_PAGE_SIZE).forEach(p => {
        const color = p.result === "WIN" ? "#00b368" : p.result === "LOSE" ? "#eb4a4a" : "#adadb8";
        const tr = document.createElement("tr");
        tr.style.borderTop = "1px solid #2d2d35";
        const netWon = p.result === "WIN" ? (p.points_won || 0) - (p.points_bet || 0) : 0;
        const wonText = p.result === "WIN" ? `+${netWon.toLocaleString()}` : p.result === "LOSE" ? `−${(p.points_bet || 0).toLocaleString()}` : "—";
        const tsText = p.ts
            ? `${new Date(p.ts).toLocaleDateString("de-AT")} ${new Date(p.ts).toLocaleTimeString("de-AT", { hour: "2-digit", minute: "2-digit" })}`
            : "—";
        [{ text: tsText, style: "color:#adadb8;white-space:nowrap" }, { text: p.channel || "—" }, { text: p.title ? p.title.slice(0, 40) : "—" }, { text: p.outcome_chosen || "—" }, { text: (p.points_bet || 0).toLocaleString() }, { text: p.result || "PENDING", style: `color:${color};font-weight:600` }, { text: wonText, style: `color:${color}` }]
            .forEach(c => { const td = document.createElement("td"); td.style.padding = "5px 8px"; if (c.style) td.style.cssText += c.style; td.textContent = c.text; tr.appendChild(td); });
        tbody.appendChild(tr);
    });

    renderPager(document.getElementById("pred-pager"), preds.length, HISTORY_PAGE_SIZE, predHistoryPage, (page) => {
        predHistoryPage = page;
        renderPredictions(predHistoryFullList);
    });
}
```

(Note this also drops the old `preds.slice(0, 100)` cap entirely — pagination replaces it, so all entries are reachable via paging instead of the top 100 being a hard ceiling.)

- [ ] **Step 5: Manually verify in the browser**

Using the Playwright browser tool against the running local instance (whichever account/port has 50+ drop history entries and, if possible, 50+ prediction entries — check `data/accounts/*/drops_history.json` / `predictions_history.json` lengths first):
- Drop History: confirm pager appears only when >50 entries, Prev disabled on page 1, clicking Next advances and re-groups by date correctly, clicking Prev returns to page 1.
- Predictions: confirm pager appears only when >50 entries (may not have enough locally — if not, verify the pager math and the "totalPages <= 1 renders nothing" case still holds by testing with `HISTORY_PAGE_SIZE` temporarily lowered in devtools, then revert).
- No console errors on any page transition.

- [ ] **Step 6: Commit**

```bash
git add src/web/index.html src/web/app.js
git commit -m "feat: add page-number pagination to drop history and bet history tables"
```

---

## Post-Implementation

After all 4 tasks are committed:
- Bump `src/version.py` and `pyproject.toml` (`chore: bump version to X.Y.Z`), matching this repo's release convention (a plain `main` push triggers the Docker build; the GitHub Release itself — tag + release notes — is created separately with `git tag vX.Y.Z <bump-commit>` + `git push origin vX.Y.Z` + `gh release create vX.Y.Z --title vX.Y.Z --notes "..."`, since there's no automatic release-branch pipeline in practice here).
- Restart the local PM2 `twitchdrops` / `twitchdrops2` processes so the running instances pick up the change.
