from datetime import date, datetime, timedelta, timezone

from src.config.settings import Settings
from src.models.campaign import DropsCampaign
from src.models.game import Game


class StreamSelector:
    def _has_unclaimed_streak_today(self, channel_login: str) -> bool:
        from src.services.message_handlers import get_streak_state
        state = get_streak_state(channel_login)
        if not state.get("active"):
            return False
        last = state.get("last_claimed_date", "")
        return last != date.today().isoformat()

    def _get_wanted_game_tree(
        self, settings: Settings, campaigns: list[DropsCampaign]
    ) -> list[dict]:
        """
        Get the hierarchical tree of wanted items (Games -> Campaigns -> Drops -> Benefits).
        Ignoring 'can earn within' time constraint.
        """
        wanted_games = []
        games_to_watch = settings.games_to_watch
        preferred_games = getattr(settings, "preferred_games", [])
        mining_benefits = settings.mining_benefits
        blacklist = [kw.lower() for kw in getattr(settings, "drop_name_blacklist", []) if kw.strip()]
        next_hour = datetime.now(timezone.utc) + timedelta(hours=1)

        # Build games_to_watch first (preserving user order), then append preferred games
        # sorted by soonest campaign end time so the most urgent game is watched first.
        all_game_names = list(games_to_watch)
        seen = set(g.lower() for g in games_to_watch)

        # Map preferred game names (not already in games_to_watch) to their soonest ends_at
        preferred_only: list[str] = []
        preferred_end_time: dict[str, datetime] = {}
        for g in preferred_games:
            if g.lower() not in seen:
                preferred_only.append(g)
                seen.add(g.lower())
        if preferred_only:
            preferred_lower = {g.lower(): g for g in preferred_only}
            for campaign in campaigns:
                name_lower = campaign.game.name.lower()
                if name_lower in preferred_lower and campaign.can_earn_within(next_hour):
                    g_key = preferred_lower[name_lower]
                    if g_key not in preferred_end_time or campaign.ends_at < preferred_end_time[g_key]:
                        preferred_end_time[g_key] = campaign.ends_at
            # Sort by soonest end time; games without active campaigns go last
            far_future = datetime.max.replace(tzinfo=timezone.utc)
            preferred_only.sort(key=lambda g: preferred_end_time.get(g, far_future))
            all_game_names.extend(preferred_only)

        for game_name in all_game_names:
            wanted_campaigns = []
            game_obj = None
            game_name_lower = game_name.lower()

            # Find all campaigns for this game
            for campaign in campaigns:
                if campaign.game.name.lower() != game_name_lower:
                    continue

                if game_obj is None:
                    game_obj = campaign.game

                if not campaign.can_earn_within(next_hour):
                    continue

                wanted_drops = []
                for drop in campaign.drops:
                    if drop.is_claimed or drop.required_minutes <= 0:
                        continue
                    if not drop._base_can_earn():
                        continue
                    if blacklist and any(kw in drop.name.lower() for kw in blacklist):
                        continue

                    filtered_benefits = [
                        {"name": b.name, "image_url": b.image_url}
                        for b in drop.benefits
                        if b.is_wanted(mining_benefits) and not drop.is_claimed
                    ]

                    if len(filtered_benefits) > 0:
                        wanted_drops.append({
                            "name": drop.name,
                            "benefits": filtered_benefits,
                            "image_url": filtered_benefits[0]["image_url"] if filtered_benefits else None,
                        })

                if len(wanted_drops) > 0:
                    wanted_campaigns.append(
                        {
                            "id": campaign.id,
                            "name": campaign.name,
                            "url": campaign.campaign_url,
                            "drops": wanted_drops,
                        }
                    )

            is_preferred = game_name.lower() in {g.lower() for g in preferred_games}
            if len(wanted_campaigns) > 0:
                wanted_games.append(
                    {
                        "game_id": game_obj.id if game_obj else None,
                        "game_name": game_name,
                        "game_icon": game_obj.box_art_url if game_obj else None,
                        "game_obj": game_obj,
                        "campaigns": wanted_campaigns,
                        "preferred": is_preferred,
                    }
                )

        return wanted_games

    def get_wanted_game_tree(
        self, settings: Settings, campaigns: list[DropsCampaign]
    ) -> list[dict]:
        return [
            {**game, "game_obj": None} for game in self._get_wanted_game_tree(settings, campaigns)
        ]

    def get_wanted_games(self, settings: Settings, campaigns: list[DropsCampaign]) -> list[Game]:
        return [game["game_obj"] for game in self._get_wanted_game_tree(settings, campaigns)]
