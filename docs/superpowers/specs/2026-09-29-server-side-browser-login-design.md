# Server-Side Browser Login (replaces device-code auth)

## Problem

Twitch shut down the `ANDROID_APP` client's device-code endpoint around
2026-09-18 (durable `invalid client`, not transient). The only device-code
client IDs that still work at all (`MOBILE_WEB`, `SMARTBOX`) mint tokens
that are either hard-rejected by the GQL gateway (`MOBILE_WEB`) or scoped
by Twitch to a degraded campaign-visibility tier (`SMARTBOX`: ~46 eligible
campaigns drop to ~5-10). This was proven exhaustively across five releases
(v1.5.3-v1.5.7, see issue #15) to be intrinsic to how the token was minted
(the grant type / client identity used against `id.twitch.tv/oauth2/device`),
not fixable by any header/identity consistency fix this app controls — the
same failure shape as the historical upstream `DevilXD/TwitchDropsMiner#264`.

Upstream (`rangermix/TwitchDropsMiner`) solved this in v2.0.0 by dropping
device-code auth entirely in favor of a real browser session, captured via
a desktop helper program the user runs on their own machine. This fork's
`AGENTS.md` documents "OAuth device code flow — Works great for web-based
deployment" as a deliberate design decision and lists "Desktop GUI" under
explicitly **not supported** scope — a literal port of upstream's desktop
helper would contradict that. This spec instead runs the real-browser login
server-side, inside the existing container, on demand.

## Goals

- Replace device-code login entirely with a real `www.twitch.tv/login`
  session, captured via a server-side browser, yielding a normally-scoped
  `auth-token` cookie (same shape/consumption as today's cookie, so the
  rest of `_AuthState`/`gql_client.py`/`client_info.py` is unaffected once
  the cookie exists).
- Preserve the fork's headless/web-GUI/single-container deployment model —
  no separate program for the user to install or run.
- Let the user complete 2FA/CAPTCHA/any interactive step live, since a
  server-automated fill would itself look bot-like to Twitch and doesn't
  solve CAPTCHA anyway.
- Only consume resources (virtual display, browser, VNC bridge) while a
  login is actually in progress; idle steady-state is unchanged.

## Non-goals

- No credential storage or auto-fill. The server never sees or stores a
  Twitch password; only the resulting session cookie, exactly as today.
- No fallback to device-code login. `SMARTBOX`/`MOBILE_WEB`/`ANDROID_APP`
  login-client code paths are deleted, not kept dormant.
- No multi-container/sidecar split (rejected approach C) — everything runs
  in the existing single container/process.
- No automated end-to-end test of the real login flow (interactive by
  nature); covered by a manual test checklist instead.

## Architecture

New module `src/auth/browser_login.py`, exporting `BrowserLoginManager`:

- Owns the lifecycle of one login attempt: start an `Xvfb` virtual display,
  launch a non-headless Playwright Chromium against it, navigate to
  `https://www.twitch.tv/login`, start `x11vnc` against the same display,
  start `websockify` bridging a local TCP port to a WebSocket noVNC can
  consume.
- Async API: `start() -> LoginSessionHandle`, `wait_for_cookie(timeout) ->
  dict[str, str]` (resolves with the captured `auth-token` + `unique_id`
  cookies or raises on timeout/cancel), `cancel()`, `stop()`.
- All subprocesses (`Xvfb`, `x11vnc`, `websockify`) and the Playwright
  browser are tracked on the handle and torn down together, from one place
  (`stop()`), called from a `try/finally` around every code path that calls
  `start()`.

`src/auth/auth_state.py` changes:

- `_oauth_login()` (device-code polling loop) is deleted.
- `_validate()`'s "missing access_token" branch calls a new
  `_browser_login()` method instead, which drives a `BrowserLoginManager`
  instance, awaits its cookie, and populates `self.access_token` /
  `jar` exactly like the old flow did after a successful device-code poll.
  Everything after that point in `_validate()` (token validation via
  `id.twitch.tv/oauth2/validate`, `cookies.jar` persistence) is unchanged.
- `LOGIN_CLIENT` constant and its long historical comment are removed;
  `headers(gql=True)` stops overriding `Client-Id`/`Origin`/`Referer`/
  `User-Agent`/`User-Agent` from a separate login client, since login and
  browsing now share one real identity (`ClientType.WEB`).

`src/config/client_info.py`: `SMARTBOX` and `MOBILE_WEB` entries are
removed (no longer referenced anywhere). `_twitch._client_type` becomes
`ClientType.WEB` everywhere (replacing `ANDROID_APP`) — the captured
cookie is inherently a `WEB`-domain (`www.twitch.tv`) cookie, so browsing
and GQL headers now match the same real identity the login happened
under, the same "one consistent identity" principle the old
`headers(gql=True)` override existed to approximate. `ANDROID_APP` is
removed as dead code once nothing references it.

`src/web/app.py`: new endpoints —

- `POST /api/login/browser/start` — begins a login attempt (rejects if one
  is already in progress for this instance), returns the noVNC WebSocket
  path.
- `WS /api/login/browser/ws` — a FastAPI WebSocket endpoint that pipes
  bytes bidirectionally to the session's local `websockify` TCP port.
  Keeps this feature within the single listening port the container
  already exposes — no new Docker port mapping or docker-compose change.
- `POST /api/login/browser/cancel` — cancels the in-progress attempt and
  tears it down.
- Existing `/api/login` / `/api/oauth/confirm` (device-code specific)
  endpoints are removed.

`src/web/gui_manager.py` / `LoginFormManager`: replace the device-code
"enter this code at twitch.tv/activate" UI state with a new state that
renders the embedded noVNC panel and status text
(waiting/verifying/success/error), driven by new Socket.IO events
(replacing `login_required`'s device-code-specific payload with one
carrying the browser-login start signal).

`web/` frontend: vendor a noVNC JS client (`static/vendor/novnc/`), add a
login panel component that mounts the `RFB` client against the WebSocket
URL from `/api/login/browser/start`, with a visible Cancel button and
status line.

Dockerfile / `pyproject.toml`: the current `Dockerfile` base is
`python:3-alpine` (musl libc) — Playwright's bundled Chromium needs glibc
and cannot run there at all (already documented in `pyproject.toml`'s
comment on the existing optional Playwright dependency used by
`src/services/campaign_discovery.py`). Base image changes to
`python:3-slim` (Debian, glibc). Add `playwright` (Python) as a base
(non-optional) dependency plus `playwright install chromium --with-deps`
in the image build step; add `xvfb`, `x11vnc`, and `websockify` (or
`novnc`'s bundled websockify) as system packages via `apt-get` (replacing
the current `apk add`). Expect a meaningfully larger image — document
this in the README's Docker section per AGENTS.md's "update README" rule.

Feasibility spike (2026-09-29, run on this project's own Hetzner VPS
host, same hosting class as `campaign_discovery.py`'s "VPS #1" that
previously hung): headful Playwright Chromium under Xvfb launched in
0.5s and loaded `https://www.twitch.tv/login` in 0.6s (HTTP 200, real
login form rendered, no CAPTCHA/integrity block). This does not reproduce
`campaign_discovery.py`'s documented VPS failure modes (Chromium hanging
before opening its CDP port; Twitch's `/integrity` endpoint 429ing
datacenter IPs) — that module's integrity check is a stricter,
separate gate tied to the drops-dashboard GQL query specifically, not
the plain login page this design depends on. Still genuinely possible
some hosts behave like `campaign_discovery.py`'s "VPS #1" (hung
Chromium) — Task 1 of the implementation plan includes an explicit,
scriptable version of this same check so a deploy can self-diagnose
that specific failure mode with a clear error rather than hanging
silently.

## Data flow

1. `_validate()` finds no usable cookie → calls `_browser_login()`.
2. `BrowserLoginManager.start()`: launches `Xvfb :N -screen 0 1280x800x24`
   on a free display number, launches Playwright Chromium with
   `DISPLAY=:N`, navigates to `https://www.twitch.tv/login`, starts
   `x11vnc -display :N -nopw -forever -shared`, starts `websockify`
   bridging a local port to that VNC port.
3. `WebGUIManager` emits a Socket.IO event carrying the WebSocket path;
   the dashboard's login panel mounts the noVNC `RFB` client against it.
4. The user completes login live inside the embedded view — credentials,
   2FA, CAPTCHA, all exactly as a normal Twitch login would show them.
5. `BrowserLoginManager` polls `context.cookies()` for the `www.twitch.tv`
   `auth-token` cookie (and `unique_id` for device_id) on an interval
   (e.g. every 1-2s) as the success signal.
6. On success: cookies are extracted, `stop()` tears down Chromium, Xvfb,
   `x11vnc`, and `websockify`, and the cookies are handed back to
   `_AuthState`, which stores them exactly as today (`jar.update_cookies`,
   `jar.save(COOKIES_PATH)`), then continues normal token validation.
7. Dashboard receives a success event, closes the noVNC panel, shows the
   logged-in state.
8. On timeout (default 10 minutes of no successful cookie) or an explicit
   `POST /api/login/browser/cancel`: `stop()` runs, an error/cancelled
   status is surfaced to the dashboard, `_browser_login()` raises so
   `_validate()`'s existing retry/error handling takes over (mirrors how a
   device-code expiry used to be handled).

## Error handling

- Missing system deps (`Xvfb`/`x11vnc`/`websockify` binaries, or
  Playwright's Chromium not installed) at `start()` time: fail fast with a
  clear, specific log message and a dashboard-visible error (no silent
  hang, no fallback — device-code is fully removed).
- User cancels or closes the dashboard tab mid-login: `cancel()` triggers
  the same `stop()` teardown as a timeout; no orphaned processes.
- Process-group cleanup: launch `Xvfb`/`x11vnc`/`websockify` each in their
  own process group so `stop()` can kill the whole group (handles a
  subprocess that spawns children), wrapped in `try/finally` around every
  `start()` call site.
- App restart while a login was mid-flight (crash, deploy): on startup,
  sweep for and kill any leftover `Xvfb`/`x11vnc`/`websockify` processes
  matching this app's known display-number/port range from a previous run
  (e.g. a PID file or a recognizable process name/arg pattern), so a crash
  never leaves a zombie virtual display running.
- Concurrent login attempts: serialized through `_AuthState`'s existing
  `self._lock`; `POST /api/login/browser/start` also checks for and
  rejects a second concurrent attempt at the API layer with a clear error.
- noVNC WebSocket disconnect (network blip): the frontend's `RFB` client
  reconnects to the same `websockify` bridge without restarting Chromium,
  as long as the overall login attempt hasn't timed out.

## Testing

- Unit tests (mocking Playwright's API and `asyncio.create_subprocess_exec`)
  for: `BrowserLoginManager` lifecycle (`start`/`stop`/`cancel` transitions,
  process-group teardown called correctly), cookie-extraction logic
  (correct cookie picked out of `context.cookies()`), timeout handling,
  and the "second concurrent attempt rejected" API behavior.
- No automated end-to-end test of an actual Twitch login (inherently
  interactive/CAPTCHA-gated) — add a manual test checklist to
  `docs/` covering: fresh login from an empty data dir, cancel mid-login,
  timeout with no user action, network blip during noVNC session,
  container restart recovery (no zombie processes).
- Review `tests/test_login_rate_limit.py` for any assumption tied to the
  device-code polling loop and update/remove as needed.

## Documentation

Per `AGENTS.md`'s rule 5, update in the same change:

- `README.md` and every per-agent instruction file's shared sections
  (Architecture → Authentication, Key Design Decisions, Docker section —
  new deps/larger image) — keeping each file's `Specific Instructions`
  section untouched.
- `lang/English.json` (and flag other languages for translators) for any
  new/changed login-related UI strings; remove now-dead device-code
  strings (`login.status.*` entries tied to "enter this code").
