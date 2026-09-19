import unittest
from unittest.mock import MagicMock

from src.auth.auth_state import LOGIN_CLIENT, _AuthState
from src.config import ClientType


class TestGqlHeaderIdentityConsistency(unittest.TestCase):
    """
    Regression test for the v1.5.5 "GQLException: Unauthorized: The
    'Authorization' token is invalid" crash (Discord-reported, thermalux/
    Stumpn): the access token is minted under LOGIN_CLIENT, but headers()
    was sending it alongside Client-Id/Origin/Referer/User-Agent from
    self._twitch._client_type (ANDROID_APP) instead -- Twitch's GQL
    gateway rejects a token whose identity doesn't match the headers it
    travels with. Every gql=True header must now come from LOGIN_CLIENT,
    consistently; every non-gql header stays on the browsing client
    (self._twitch._client_type) so page-scraping/device_id extraction is
    unaffected.
    """

    def setUp(self):
        self.twitch = MagicMock()
        self.twitch._client_type = ClientType.ANDROID_APP
        self.auth = _AuthState(self.twitch)
        self.auth.access_token = "test-token"
        self.auth.device_id = "dev-id"

    def test_gql_headers_all_match_login_client(self):
        headers = self.auth.headers(user_agent=ClientType.ANDROID_APP.USER_AGENT, gql=True)
        self.assertEqual(headers["Client-Id"], LOGIN_CLIENT.CLIENT_ID)
        self.assertEqual(headers["Origin"], str(LOGIN_CLIENT.CLIENT_URL))
        self.assertEqual(headers["Referer"], str(LOGIN_CLIENT.CLIENT_URL))
        self.assertEqual(headers["User-Agent"], LOGIN_CLIENT.USER_AGENT)
        self.assertEqual(headers["Authorization"], "OAuth test-token")

    def test_gql_headers_never_mix_in_the_browsing_client(self):
        # The whole point: none of the identity-bearing headers should ever
        # come from self._twitch._client_type once gql=True.
        headers = self.auth.headers(user_agent=ClientType.ANDROID_APP.USER_AGENT, gql=True)
        self.assertNotEqual(headers["Client-Id"], ClientType.ANDROID_APP.CLIENT_ID)
        self.assertNotEqual(headers["Origin"], str(ClientType.ANDROID_APP.CLIENT_URL))

    def test_non_gql_headers_stay_on_the_browsing_client(self):
        headers = self.auth.headers()
        self.assertEqual(headers["Client-Id"], ClientType.ANDROID_APP.CLIENT_ID)
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("Origin", headers)


if __name__ == "__main__":
    unittest.main()
