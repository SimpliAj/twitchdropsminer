from datetime import datetime, timedelta, timezone

from src.config.settings import Settings
from src.models.campaign import DropsCampaign
from src.models.game import Game


class StreamSelector:
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

        # Merge games_to_watch + preferred_games (deduplicated, preserving order)
        all_game_names = list(games_to_watch)
        seen = set(g.lower() for g in games_to_watch)
        for g in preferred_games:
            if g.lower() not in seen:
                all_game_names.append(g)
                seen.add(g.lower())

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
            elif is_preferred:
                # Preferred game with no active campaigns — keep in list so it auto-connects when one appears
                wanted_games.append(
                    {
                        "game_id": game_obj.id if game_obj else None,
                        "game_name": game_name,
                        "game_icon": game_obj.box_art_url if game_obj else None,
                        "game_obj": game_obj,
                        "campaigns": [],
                        "preferred": True,
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
