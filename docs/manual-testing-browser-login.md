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
- [ ] **Panel appears for a dashboard opened late**: start the app with an
      empty data dir and do NOT open the dashboard yet. Wait ~30s, then
      open it for the first time. The browser-login panel must be there
      (it comes from `/api/status`'s `browser_login_ready`, not only from
      the live Socket.IO event).
- [ ] **Cancel mid-login**: start a login, click "Cancel login" before
      completing it. Confirm the app keeps running, the dashboard reports
      the cancellation and a fresh login browser is offered a few seconds
      later, and check `ps aux` on the host/container for any lingering
      `Xvfb`/`chromium`/`x11vnc` process from the cancelled
      attempt (there should be none within a few seconds).
- [ ] **Timeout with no user action**: start a login and leave it alone
      for the full timeout window (10 minutes). Confirm the dashboard
      shows a timed-out state, the process does NOT exit, a fresh login
      browser is offered, and the same zombie-process check above stays
      clean.
- [ ] **Display slots are reused**: cancel/time out several logins in a
      row in one container. Confirm `ls /tmp/.X9*-lock` does not
      accumulate entries and that the 11th attempt still works (the old
      behaviour burned one of 10 reserved display numbers per attempt and
      then failed permanently).
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
      via `ps aux` that no Xvfb/x11vnc from the killed
      container survived).
- [ ] **Two dashboard tabs open at once**: open the dashboard in two
      browser tabs while logged out. Both must show the SAME live browser
      session (there is only one login attempt system-wide, owned by the
      auth flow -- there is no manual "start login" endpoint), and
      completing the login in either tab must log both in.
- [ ] **No handler leak on disconnect**: with a login in progress, close
      and reopen the dashboard tab a dozen times over a minute. Confirm
      the server's connection count to the local x11vnc port stays
      at roughly one (`ss -tnp | grep 599` inside the container) rather
      than growing with each reconnect.
