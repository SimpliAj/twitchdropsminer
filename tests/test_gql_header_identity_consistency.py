import unittest
from unittest.mock import MagicMock

from src.auth.auth_state import _AuthState
from src.config import ClientType


class TestGqlHeaderIdentityConsistency(unittest.TestCase):
    """
    Regression coverage for the v1.5.0-v1.5.7 login/GQL identity saga (see
    the git history of auth_state.py and docs/superpowers/specs/
    2026-09-29-server-side-browser-login-design.md): every crash in this
    area traced back to gql=True headers and browsing headers deriving from
    two different Client-Id/Origin/Referer/User-Agent identities, which
    Twitch's GQL gateway either hard-rejects or silently scopes down.

    The old fix (pre-2026-09-29) forced gql=True headers onto a dedicated
    LOGIN_CLIENT identity, deliberately DIFFERENT from the browsing client
    (ANDROID_APP at the time) -- see the now-deleted
    test_gql_headers_never_mix_in_the_browsing_client for that shape of
    test. The new design (server-side real-browser login) removes the
    separate login identity entirely: login now happens through a real
    browser session under the SAME client (ClientType.WEB) used for
    browsing, so gql and non-gql headers must now derive from the same
    self._twitch._client_type consistently -- the invariant inverts from
    "must differ" to "must match".
    """

    def setUp(self):
        self.twitch = MagicMock()
        self.twitch._client_type = ClientType.WEB
        self.auth = _AuthState(self.twitch)
        self.auth.access_token = "test-token"
        self.auth.device_id = "dev-id"

    def test_gql_headers_use_the_browsing_clients_identity(self):
        headers = self.auth.headers(user_agent=ClientType.WEB.USER_AGENT, gql=True)
        self.assertEqual(headers["Client-Id"], ClientType.WEB.CLIENT_ID)
        self.assertEqual(headers["User-Agent"], ClientType.WEB.USER_AGENT)
        self.assertEqual(headers["Authorization"], "OAuth test-token")

    def test_gql_and_non_gql_client_id_now_match(self):
        # The whole point of the redesign: there's only one identity now,
        # so gql=True must never diverge from plain browsing headers on
        # Client-Id (the old design deliberately forced them apart).
        gql_headers = self.auth.headers(user_agent=ClientType.WEB.USER_AGENT, gql=True)
        plain_headers = self.auth.headers()
        self.assertEqual(gql_headers["Client-Id"], plain_headers["Client-Id"])
        self.assertEqual(gql_headers["Client-Id"], ClientType.WEB.CLIENT_ID)

    def test_non_gql_headers_stay_unauthenticated(self):
        headers = self.auth.headers()
        self.assertEqual(headers["Client-Id"], ClientType.WEB.CLIENT_ID)
        self.assertNotIn("Authorization", headers)


if __name__ == "__main__":
    unittest.main()
