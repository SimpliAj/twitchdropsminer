import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.config.settings import Settings, default_settings
from src.models.campaign import DropsCampaign
from src.models.game import Game
from src.services.stream_selector import StreamSelector
from src.web.app import SettingsUpdate
from src.web.managers.settings import SettingsManager


def _benefit(name, benefit_type="BADGE"):
    benefit = MagicMock()
    benefit.name = name
    benefit.image_url = None
    benefit.type.name = benefit_type
    benefit.is_wanted.return_value = True
    return benefit


def _campaign(game_id, game_name, ends_in_hours, benefit_type="BADGE"):
    campaign = MagicMock(spec=DropsCampaign)
    campaign.game = Game({"id": game_id, "name": game_name})
    campaign.can_earn_within.return_value = True
    campaign.id = f"c-{game_id}-{ends_in_hours}"
    campaign.name = f"{game_name} campaign"
    campaign.campaign_url = "http://test.url"
    campaign.ends_at = datetime.now(timezone.utc) + timedelta(hours=ends_in_hours)
    drop = MagicMock()
    drop.id = f"d-{game_id}-{ends_in_hours}"
    drop.required_minutes = 30
    drop.name = "Drop"
    drop.is_claimed = False
    drop.benefits = [_benefit("Benefit", benefit_type)]
    campaign.drops = [drop]
    return campaign


class TestPriorityListFallback(unittest.TestCase):
    def setUp(self):
        self.settings = MagicMock()
        self.settings.games_to_watch = ["Watched"]
        self.settings.preferred_games = ["Preferred"]
        self.settings.drop_name_blacklist = []
        self.settings.blacklisted_drop_ids = []
        self.settings.mining_benefits = {"BADGE": True, "DIRECT_ENTITLEMENT": True}
        self.campaigns = [
            _campaign(1, "Watched", 100, "DIRECT_ENTITLEMENT"),
            _campaign(2, "Preferred", 50, "DIRECT_ENTITLEMENT"),
            _campaign(3, "OtherLate", 20),
            _campaign(4, "OtherSoon", 5),
        ]

    def _names(self):
        return [g.name for g in StreamSelector().get_wanted_games(self.settings, self.campaigns)]

    def test_priority_list_only_keeps_old_behavior(self):
        self.settings.priority_list_only = True
        self.assertEqual(self._names(), ["Watched", "Preferred"])

    def test_fallback_appends_other_games_sorted_by_end(self):
        self.settings.priority_list_only = False
        self.assertEqual(self._names(), ["Watched", "Preferred", "OtherSoon", "OtherLate"])

    def test_fallback_games_never_outrank_priority_games(self):
        # Fallback games carry badges (best category rank), priority games only
        # items -- the category grouping must not lift them above the list.
        self.settings.priority_list_only = False
        names = self._names()
        self.assertLess(names.index("Preferred"), names.index("OtherSoon"))
        self.assertLess(names.index("Watched"), names.index("OtherSoon"))

    def test_fallback_skips_games_without_earnable_campaign(self):
        self.settings.priority_list_only = False
        self.campaigns[3].can_earn_within.return_value = False
        self.assertEqual(self._names(), ["Watched", "Preferred", "OtherLate"])

    def test_missing_setting_behaves_like_priority_list_only(self):
        del self.settings.priority_list_only
        self.assertEqual(self._names(), ["Watched", "Preferred"])


class TestPriorityListOnlySetting(unittest.TestCase):
    def test_default_is_true(self):
        self.assertIs(default_settings["priority_list_only"], True)

    def test_settings_dataclass_declares_field(self):
        self.assertIn("priority_list_only", Settings.__annotations__)

    def test_settings_update_model_accepts_field(self):
        self.assertIs(SettingsUpdate(priority_list_only=False).priority_list_only, False)

    def test_settings_manager_persists_and_triggers_reload(self):
        mock_broadcaster = MagicMock()
        mock_broadcaster.emit = AsyncMock()
        mock_settings = MagicMock()
        mock_settings.priority_list_only = True

        with patch("asyncio.create_task", side_effect=lambda coro: coro.close()):
            manager = SettingsManager(
                mock_broadcaster, mock_settings, MagicMock(), on_change=MagicMock()
            )
            with patch.object(manager, "_trigger_reload_debounced") as reload:
                manager.update_settings({"priority_list_only": False})

        self.assertIs(mock_settings.priority_list_only, False)
        reload.assert_called_once()


ROOT = Path(__file__).resolve().parents[1]


class TestPriorityListOnlyUi(unittest.TestCase):
    def test_toggle_lives_in_preferred_games_section(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        section = html[html.index('data-i18n="gui.settings.preferred_games"'):]
        section = section[:section.index("</section>")]
        self.assertRegex(section, r'<input type="checkbox" id="priority-list-only" onchange="saveSettings\(\)">')
        self.assertIn('data-i18n="gui.settings.priority_list_only"', section)
        self.assertIn('data-i18n="gui.settings.priority_list_only_help"', section)

    def test_app_js_renders_and_collects_setting(self):
        js = (ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("settings.priority_list_only !== false", js)
        self.assertRegex(
            js, r"priority_list_only: document\.getElementById\('priority-list-only'\)\?\.checked \?\? true"
        )

    def test_english_translation_has_both_keys(self):
        lang = json.loads((ROOT / "lang" / "English.json").read_text(encoding="utf-8"))
        settings = lang["gui"]["settings"]
        self.assertTrue(settings["priority_list_only"])
        self.assertTrue(settings["priority_list_only_help"])


if __name__ == "__main__":
    unittest.main()
