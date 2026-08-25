import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.api.gql_client import GQLClient
from src.exceptions import GQLException


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class _FakeRequestContext:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return _FakeResponse(self._payload)

    async def __aexit__(self, *args):
        return False


def _service_error(op_name: str = "CampaignDetails") -> dict:
    return {
        "data": {},
        "errors": [{"message": "service error"}],
        "extensions": {"operationName": op_name},
    }


class TestGQLClientServiceErrorRetryBudget(unittest.IsolatedAsyncioTestCase):
    """
    Regression coverage for the crash where a transient "service error" from
    Twitch for one DropCampaignDetails query took down the whole miner
    (uncaught GQLException propagating through fetch_inventory -> _run ->
    run -> __main__, exit_status=1).

    gql_client.request() used a single boolean flag shared across the *entire*
    call (including every op in a batched request) to decide whether to retry
    a "service error"/"PersistedQueryNotFound" - so a second, unrelated
    occurrence within the same request would raise immediately instead of
    retrying. It's now a small retry budget instead of a one-shot flag.
    """

    def _make_client(self, responses: list) -> GQLClient:
        http_client = MagicMock()
        http_client.request = MagicMock(
            side_effect=[_FakeRequestContext(r) for r in responses]
        )

        validated = MagicMock()
        validated.headers.return_value = {}
        auth_state = MagicMock()
        auth_state.validate = AsyncMock(return_value=validated)

        client_type = MagicMock()
        client_type.USER_AGENT = "test-agent"

        client = GQLClient(http_client, auth_state, client_type)
        self._http_client = http_client
        return client

    async def test_retries_service_error_more_than_once_per_request(self):
        responses = [
            [_service_error()],
            [_service_error()],
            [{"data": {"ok": True}}],
        ]
        gql_client = self._make_client(responses)

        with patch("src.api.gql_client.asyncio.sleep", new=AsyncMock()):
            result = await gql_client.request([MagicMock(), MagicMock()])

        self.assertEqual(result, [{"data": {"ok": True}}])
        self.assertEqual(self._http_client.request.call_count, 3)

    async def test_gives_up_after_retry_budget_exhausted(self):
        # Retry budget is 3, so the 5th consecutive "service error" (1 initial
        # attempt + 3 retries = 4 requests total) should finally raise instead
        # of retrying forever.
        responses = [[_service_error()] for _ in range(5)]
        gql_client = self._make_client(responses)

        with (
            patch("src.api.gql_client.asyncio.sleep", new=AsyncMock()),
            self.assertRaises(GQLException),
        ):
            await gql_client.request([MagicMock()])

        self.assertEqual(self._http_client.request.call_count, 4)


if __name__ == "__main__":
    unittest.main()
