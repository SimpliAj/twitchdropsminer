import asyncio
import json
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock


def _make_campaign(id, game_name, ends_in_hours, unclaimed_drops):
    c = MagicMock()
    c.id = id
    c.name = f"Campaign {id}"
    c.game = MagicMock()
    c.game.name = game_name
    c.ends_at = datetime.now(timezone.utc) + timedelta(hours=ends_in_hours)
    c.is_claimed = unclaimed_drops == 0
    c.unclaimed_drops = unclaimed_drops
    c.campaign_url = f"https://twitch.tv/drops/campaigns?dropID={id}"
    return c


def test_finds_expiring_campaigns():
    from src.services.campaign_alert_service import CampaignAlertService

    service = CampaignAlertService.__new__(CampaignAlertService)
    service._alerted: set = set()

    expiring = _make_campaign("c1", "R6", 10, 2)
    not_expiring = _make_campaign("c2", "Rust", 48, 1)
    already_claimed = _make_campaign("c3", "R6", 5, 0)
    already_alerted = _make_campaign("c4", "Rust", 3, 1)
    service._alerted.add("c4")

    campaigns = [expiring, not_expiring, already_claimed, already_alerted]
    result = service._get_campaigns_to_alert(campaigns)

    assert len(result) == 1
    assert result[0].id == "c1"


def test_already_alerted_not_repeated():
    from src.services.campaign_alert_service import CampaignAlertService

    service = CampaignAlertService.__new__(CampaignAlertService)
    service._alerted = {"c1"}
    campaign = _make_campaign("c1", "R6", 5, 2)
    result = service._get_campaigns_to_alert([campaign])
    assert result == []
