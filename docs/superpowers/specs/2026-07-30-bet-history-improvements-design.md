# Design: Bet History Improvements (Timestamp/Selection, Pagination, Stuck-Pending Fallback)

**Date:** 2026-07-30
**Project:** TwitchDropsMiner (SimpliAj fork)

---

## 1. Bet Table: Timestamp + Selection Display

### Goal
The Predictions tab's bet history table (`app.js::loadPredictions`, `#pred-tbody`) currently
shows only the bet *date* and never shows which outcome was picked, even though the data
(`outcome_chosen`) is already recorded per entry.

### Changes
- Timestamp column: `new Date(p.ts).toLocaleDateString()` → include time, e.g.
  `new Date(p.ts).toLocaleDateString("de-AT", {...}) + " " + new Date(p.ts).toLocaleTimeString("de-AT", {hour:"2-digit", minute:"2-digit"})`,
  matching the `de-AT` locale style already used in `renderDropHistory`.
- New "Selection" column between Title and Points Bet, rendering `p.outcome_chosen`.
- No backend changes — `outcome_chosen` is already persisted in `predictions_history.json`
  by `prediction_service.py::_save_pending_bet`.

---

## 2. Pagination (Bet History + Drop History)

### Goal
Both tables render their full list unpaginated (bet history: 100-row hard cap in JS with
nothing beyond it; drop history: full list in a scrollable box). Add page-number pagination
to both.

### Architecture
- Both source files are capped at 500 entries server-side already (`MAX_HISTORY` in
  `drop_history.py` and `prediction_service.py`) — small payloads (<100KB). No API changes;
  pagination is client-side slicing over the already-fetched full list.
- One shared pager helper in `app.js` (e.g. `renderPager(container, totalItems, pageSize, currentPage, onPageChange)`)
  used by both `renderDropHistory` and `loadPredictions`/its render step, instead of two
  copies — keeps DRY per `AGENTS.md`.
- Page size: 50 rows.
- Style: prev/next buttons + "Page N of M" label, no infinite scroll.
- Each table keeps its own current-page state (module-level `let` per table), reset to page 1
  on reload.

---

## 3. Stuck "PENDING" Status Fallback

### Goal
`prediction_service.py::_record_result` only updates a bet's `PENDING` entry when a
`RESOLVED` websocket event arrives on a still-subscribed channel. Because the miner
constantly channel-hops chasing drops, it can unsubscribe before that event lands, leaving
the entry `PENDING` forever with nothing to ever revisit it. Add a fallback so stuck entries
resolve to a neutral status instead of sitting wrong/unknown forever.

### Fallback status
`"UNKNOWN"` — rendered as a neutral gray row in the bet table (existing color logic:
`p.result === "WIN" ? green : p.result === "LOSE" ? red : "#adadb8"` already defaults
unrecognized results to gray, so no frontend color change needed — `UNKNOWN` falls through
the same branch `"PENDING"` currently does).

### Two mechanisms

**a) Stream-offline trigger (fast path)**
`message_handlers.py::on_channel_update`'s ONLINE→OFFLINE branch (~line 264) already fires
for every tracked channel transitioning offline, regardless of whether it's the one being
watched. Add a call there into a new `PredictionService` method,
e.g. `mark_channel_stale_pending(channel_name)`, which loads `predictions_history.json`,
flips any entry with `channel == channel_name.lower()` and `result == "PENDING"` to
`"UNKNOWN"`, and saves.

**b) 24h age sweep (catch-all/backstop)**
Covers cases the offline-hook can't reach (e.g. app restart loses in-memory `Channel`
tracking before an offline transition ever fires for that channel again). Runs inline in the
`/api/predictions` GET handler (`app.py`) before returning: any entry with
`result == "PENDING"` and `ts` older than 24h flips to `"UNKNOWN"` and the file is
re-saved. No new background task/loop.

**c) Late-result safety**
`_record_result`'s match condition changes from `entry.get("result") == "PENDING"` to
`entry.get("result") in ("PENDING", "UNKNOWN")`, so if Twitch's real resolution event
does arrive after an entry was already marked `UNKNOWN` by (a) or (b), it still gets
correctly overwritten to `WIN`/`LOSE` instead of being permanently stuck wrong.

### Testing
- Unit test for the 24h sweep: entry with `ts` 25h old and `PENDING` → becomes `UNKNOWN`;
  entry with `ts` 1h old and `PENDING` → stays `PENDING`.
- Unit test for `mark_channel_stale_pending`: only entries matching the given channel and
  still `PENDING` are flipped; other channels/statuses untouched.
- Unit test for `_record_result` late-override: an `UNKNOWN` entry for a matching
  `event_id` gets overwritten to `WIN`/`LOSE` when a resolution arrives.

---

## Out of Scope
- Server-side pagination (rejected — payload too small to justify).
- Precise "stream ended" semantics beyond the existing ONLINE→OFFLINE `Channel` transition
  already tracked by the miner (no new stream-state polling).
