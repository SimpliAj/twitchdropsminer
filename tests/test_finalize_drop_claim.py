import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.services.drop_history import finalize_drop_claim


class _FakeBenefit:
    def __init__(self, image_url="https://example.com/img.png"):
        self.image_url = image_url


class _FakeGame:
    def __init__(self, name="Some Game"):
        self.name = name


class _FakeCampaign:
    def __init__(self, game_name="Some Game"):
        self.game = _FakeGame(game_name)


class _FakeDrop:
    def __init__(self, drop_id="drop-1", name="Some Drop"):
        self.id = drop_id
        self.name = name
        self.benefits = [_FakeBenefit()]

    def rewards_text(self, delim=", "):
        return "Some Reward"


def _make_twitch(webhook_url="https://discord.com/api/webhooks/x/y"):
    twitch = MagicMock()
    twitch._claim_finalized_drops = set()
    twitch.settings.discord_webhook_drops = webhook_url
    twitch._message_handler_service._send_discord_webhook = AsyncMock()
    return twitch


class TestFinalizeDropClaim(unittest.IsolatedAsyncioTestCase):
    async def test_saves_history_and_sends_webhook_once(self):
        twitch = _make_twitch()
        campaign = _FakeCampaign()
        drop = _FakeDrop()

        with patch("src.services.drop_history.save_drop_claim") as mock_save, \
                patch("src.services.drop_history._WEB_CONFIG_FILE") as mock_cfg:
            mock_cfg.exists.return_value = False
            finalize_drop_claim(twitch, campaign, drop)
            await asyncio.sleep(0)

        mock_save.assert_called_once_with("Some Game", "Some Drop", "Some Reward", "https://example.com/img.png")
        twitch._message_handler_service._send_discord_webhook.assert_called_once()

    async def test_second_call_for_same_drop_is_a_noop(self):
        """Regression test: previously the same claimed drop could be recorded by
        multiple call sites racing each other (local progress-complete detection,
        websocket drop-claim confirmation, periodic inventory reconciliation),
        each seeing claim() return True and independently saving/notifying —
        producing duplicate drops_history.json entries, or (when only the
        webhook-less path won the race) a saved entry with no notification."""
        twitch = _make_twitch()
        campaign = _FakeCampaign()
        drop = _FakeDrop()

        with patch("src.services.drop_history.save_drop_claim") as mock_save, \
                patch("src.services.drop_history._WEB_CONFIG_FILE") as mock_cfg:
            mock_cfg.exists.return_value = False
            finalize_drop_claim(twitch, campaign, drop)
            await asyncio.sleep(0)
            finalize_drop_claim(twitch, campaign, drop)
            await asyncio.sleep(0)

        mock_save.assert_called_once()
        twitch._message_handler_service._send_discord_webhook.assert_called_once()

    async def test_no_webhook_url_still_saves_history(self):
        twitch = _make_twitch(webhook_url="")
        campaign = _FakeCampaign()
        drop = _FakeDrop()

        with patch("src.services.drop_history.save_drop_claim") as mock_save:
            finalize_drop_claim(twitch, campaign, drop)
            await asyncio.sleep(0)

        mock_save.assert_called_once()
        twitch._message_handler_service._send_discord_webhook.assert_not_called()

    async def test_different_drops_both_finalize_independently(self):
        twitch = _make_twitch()
        campaign = _FakeCampaign()
        drop_a = _FakeDrop(drop_id="drop-a")
        drop_b = _FakeDrop(drop_id="drop-b")

        with patch("src.services.drop_history.save_drop_claim") as mock_save, \
                patch("src.services.drop_history._WEB_CONFIG_FILE") as mock_cfg:
            mock_cfg.exists.return_value = False
            finalize_drop_claim(twitch, campaign, drop_a)
            finalize_drop_claim(twitch, campaign, drop_b)
            await asyncio.sleep(0)

        self.assertEqual(mock_save.call_count, 2)
        self.assertEqual(twitch._message_handler_service._send_discord_webhook.call_count, 2)


if __name__ == "__main__":
    unittest.main()
