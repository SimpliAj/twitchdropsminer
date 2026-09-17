import time
import unittest

from src.web import app as app_module


class TestSessionTokens(unittest.TestCase):
    """
    Regression test for __tdm_session no longer being the plaintext password.

    Before this change, /__auth_login and /__setup_post set the session
    cookie to the raw password itself, and PasswordAuthMiddleware just
    compared the cookie against _get_password() -- any leak of the cookie
    (logs, a proxy, XSS) handed over the real credential, not just a
    revocable session. _new_session_token()/_session_valid()/_revoke_session()
    are the real random-token store that replaced that comparison.
    """

    def setUp(self):
        app_module._SESSIONS.clear()

    def test_new_token_is_valid_and_not_the_password(self):
        token = app_module._new_session_token()
        self.assertTrue(app_module._session_valid(token))
        # The whole point: the token must never equal a real password value
        # someone might plausibly set, and must be long/random, not a
        # predictable or empty string.
        self.assertGreaterEqual(len(token), 32)

    def test_empty_or_unknown_token_is_invalid(self):
        self.assertFalse(app_module._session_valid(""))
        self.assertFalse(app_module._session_valid("not-a-real-token"))

    def test_expired_token_is_invalid(self):
        token = app_module._new_session_token()
        app_module._SESSIONS[token] = time.time() - 1  # force expiry
        self.assertFalse(app_module._session_valid(token))

    def test_revoke_invalidates_immediately(self):
        token = app_module._new_session_token()
        self.assertTrue(app_module._session_valid(token))
        app_module._revoke_session(token)
        self.assertFalse(app_module._session_valid(token))

    def test_two_logins_get_different_tokens(self):
        # Guards against a regression back to a fixed/shared value (e.g. the
        # password itself) -- every login must mint its own unique token.
        self.assertNotEqual(app_module._new_session_token(), app_module._new_session_token())

    def test_session_count_is_bounded(self):
        # Same memory-growth discipline as the rest of this project (see the
        # 2026-09-17 websocket-task-leak fix, issue #12) -- an unbounded
        # dict fed by every login over a long-running process's lifetime is
        # exactly that class of bug. _SESSION_MAX_COUNT caps it.
        for _ in range(app_module._SESSION_MAX_COUNT + 20):
            app_module._new_session_token()
        self.assertLessEqual(len(app_module._SESSIONS), app_module._SESSION_MAX_COUNT)

    def test_pruning_drops_only_expired_tokens(self):
        fresh = app_module._new_session_token()
        stale = app_module._new_session_token()
        app_module._SESSIONS[stale] = time.time() - 1
        app_module._prune_sessions()
        self.assertTrue(app_module._session_valid(fresh))
        self.assertNotIn(stale, app_module._SESSIONS)


class TestFleetAuthHeader(unittest.TestCase):
    """
    Regression test for fleet-to-fleet auth moving off the session cookie.

    _fleet_auth_cookies() used to send this instance's own password AS the
    session cookie (relying on cookies not being port-scoped) -- that only
    worked because the cookie WAS the password. Now that the cookie is a
    random per-login token a sibling instance could never recognize, fleet
    calls authenticate via an explicit X-Fleet-Password header instead,
    checked by PasswordAuthMiddleware before it looks at the session cookie.
    """

    def setUp(self):
        self._orig_get_password = app_module._get_password

    def tearDown(self):
        app_module._get_password = self._orig_get_password

    def test_returns_fleet_password_header_when_password_set(self):
        app_module._get_password = lambda: "hunter2"
        self.assertEqual(app_module._fleet_auth_headers(), {"X-Fleet-Password": "hunter2"})

    def test_returns_empty_when_no_password_set(self):
        app_module._get_password = lambda: ""
        self.assertEqual(app_module._fleet_auth_headers(), {})


if __name__ == "__main__":
    unittest.main()
