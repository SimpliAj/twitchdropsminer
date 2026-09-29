# Manual Testing: Real-Browser Login

Automated tests cover BrowserLoginManager's lifecycle logic and the API
endpoints with mocked Playwright/subprocess calls (see
tests/test_browser_login.py, tests/test_auth_state_browser_login.py,
tests/test_login_browser_endpoints.py). The actual interactive login flow
is inherently manual (real Twitch account, possibly real 2FA/CAPTCHA) --
run this checklist after any change touching src/auth/browser_login.py,
src/web/app.py's browser-login routes, or web/static/app.js's login panel.

- [ ] **Fresh login from an empty data dir**: delete/rename the data
      directory, start the app, confirm the dashboard shows the embedded
      browser-login panel, complete a real Twitch login in it (including
      2FA if your account has it), confirm the dashboard transitions to
      logged-in and campaigns load with full visibility (not the old
      SMARTBOX-degraded ~5-10 count).
- [ ] **Cancel mid-login**: start a login, click "Cancel login" before
      completing it. Confirm the panel closes, and check `ps aux` on the
      host/container for any lingering `Xvfb`/`chromium`/`x11vnc`/
      `websockify` process (there should be none within a few seconds).
- [ ] **Timeout with no user action**: start a login and leave it alone
      for the full timeout window (10 minutes). Confirm the dashboard
      shows an error/timed-out state and the same zombie-process check
      above stays clean.
- [ ] **Network blip during an active noVNC session**: start a login,
      open the browser devtools Network tab, and manually throttle/kill
      the WebSocket connection (or briefly disable networking on the
      client machine). Confirm the noVNC view reconnects on its own
      within a few seconds without the server-side Chromium session
      restarting (the in-progress Twitch login page state should still
      be there after reconnecting).
- [ ] **Container restart recovery**: start a login, then forcibly kill
      the container (`docker kill`, not a graceful stop) mid-login.
      Restart it and check the startup log for a
      "Cleaned up N orphaned browser-login process(es)" line (or confirm
      via `ps aux` that no Xvfb/x11vnc/websockify from the killed
      container survived).
- [ ] **Two dashboard tabs, both trigger login**: open the dashboard in
      two browser tabs while logged out, trigger login from both.
      Confirm the second gets a clear "already in progress" error rather
      than a second Chromium session.
