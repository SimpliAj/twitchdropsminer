import unittest
from unittest.mock import AsyncMock, MagicMock, patch


class _FakeResponse:
    def __init__(self, status: int, body: str = ""):
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class TestDiscordWebhook(unittest.IsolatedAsyncioTestCase):
    def _make_service(self):
        from src.services.message_handlers import MessageHandlerService

        service = MessageHandlerService.__new__(MessageHandlerService)
        return service

    async def test_success_status_does_not_warn(self):
        service = self._make_service()
        fake_response = _FakeResponse(204)
        fake_session = MagicMock()
        fake_session.post = MagicMock(return_value=fake_response)
        fake_session.__aenter__ = AsyncMock(return_value=fake_session)
        fake_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=fake_session), \
                patch("src.services.message_handlers.logger") as mock_logger:
            await service._send_discord_webhook("https://discord.com/api/webhooks/x/y", {"embeds": []})

        mock_logger.warning.assert_not_called()

    async def test_rejected_status_logs_warning_with_body(self):
        service = self._make_service()
        fake_response = _FakeResponse(400, body="Invalid Form Body")
        fake_session = MagicMock()
        fake_session.post = MagicMock(return_value=fake_response)
        fake_session.__aenter__ = AsyncMock(return_value=fake_session)
        fake_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=fake_session), \
                patch("src.services.message_handlers.logger") as mock_logger:
            await service._send_discord_webhook("https://discord.com/api/webhooks/x/y", {"embeds": []})

        mock_logger.warning.assert_called_once()
        warning_text = mock_logger.warning.call_args[0][0]
        self.assertIn("400", warning_text)
        self.assertIn("Invalid Form Body", warning_text)

    async def test_empty_url_is_a_noop(self):
        service = self._make_service()
        with patch("aiohttp.ClientSession") as mock_session_cls:
            await service._send_discord_webhook("", {"embeds": []})
        mock_session_cls.assert_not_called()

    async def test_network_exception_logs_warning(self):
        service = self._make_service()
        with patch("aiohttp.ClientSession", side_effect=RuntimeError("boom")), \
                patch("src.services.message_handlers.logger") as mock_logger:
            await service._send_discord_webhook("https://discord.com/api/webhooks/x/y", {"embeds": []})

        mock_logger.warning.assert_called_once()
        self.assertIn("boom", mock_logger.warning.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
