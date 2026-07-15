from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

import json as _json

from src.config import DATA_DIR, GQL_OPERATIONS
from src.utils import task_wrapper

if TYPE_CHECKING:
    from src.core.client import Twitch

logger = logging.getLogger("TwitchDrops")

MAX_HISTORY = 500


def _get_predictions_file():
    try:
        import os
        from pathlib import Path
        _wc = DATA_DIR / "web_config.json"
        cfg = _json.loads(_wc.read_text()) if _wc.exists() else {}
        account = cfg.get("active_account")
        if account:
            d = DATA_DIR / "accounts" / account
            d.mkdir(parents=True, exist_ok=True)
            return d / "predictions_history.json"
    except Exception:
        pass
    return DATA_DIR / "predictions_history.json"

def _get_overrides_file():
    return DATA_DIR / "streamer_overrides.json"

def _load_overrides() -> dict:
    p = _get_overrides_file()
    try:
        return _json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


class PredictionService:
    def __init__(self, twitch: Twitch):
        self._twitch = twitch
        self._pending: dict[str, asyncio.Task] = {}  # event_id -> bet task
        self._active_events: dict[str, dict] = {}    # event_id -> event data

    def _get_channel_settings(self, channel_login: str) -> dict:
        overrides = _load_overrides().get(channel_login.lower(), {})
        s = self._twitch.settings
        ch = channel_login.lower()
        ui_strategy = s.channel_strategies.get(ch) if hasattr(s, "channel_strategies") else None
        return {
            "make_predictions": overrides.get("make_predictions", s.make_predictions),
            "bet_strategy": overrides.get("bet_strategy", ui_strategy or s.bet_strategy),
            "bet_percentage": overrides.get("bet_percentage", s.bet_percentage),
            "bet_max_points": overrides.get("bet_max_points", s.bet_max_points),
            "bet_minimum_points": overrides.get("bet_minimum_points", s.bet_minimum_points),
            "bet_percentage_gap": overrides.get("bet_percentage_gap", s.bet_percentage_gap),
            "bet_delay_seconds": overrides.get("bet_delay_seconds", s.bet_delay_seconds),
        }

    def _choose_outcome(self, outcomes: list[dict], cfg: dict) -> dict | None:
        if len(outcomes) < 2:
            return None
        strategy = cfg["bet_strategy"].upper()

        def total_points(o): return o.get("total_points", 0) or 1
        def total_users(o): return o.get("total_users", 0) or 1
        def odds(o): return (sum(x["total_points"] for x in outcomes) / total_points(o))

        if strategy == "MOST_VOTED":
            return max(outcomes, key=lambda o: total_users(o))
        elif strategy == "HIGH_ODDS":
            return max(outcomes, key=lambda o: odds(o))
        elif strategy == "PERCENTAGE":
            return max(outcomes, key=lambda o: total_points(o))
        else:  # SMART
            sorted_by_users = sorted(outcomes, key=lambda o: total_users(o), reverse=True)
            top, second = sorted_by_users[0], sorted_by_users[1]
            top_pct = top_users_pct = total_users(top) / sum(total_users(o) for o in outcomes) * 100
            second_pct = total_users(second) / sum(total_users(o) for o in outcomes) * 100
            gap = abs(top_pct - second_pct)
            if gap < cfg["bet_percentage_gap"]:
                return None  # too close, skip
            return top

    def _calc_amount(self, balance: int, cfg: dict) -> int:
        amount = int(balance * cfg["bet_percentage"] / 100)
        amount = min(amount, cfg["bet_max_points"])
        return max(amount, 10)  # Twitch minimum is 10 channel points

    @task_wrapper
    async def process_prediction(self, channel_id: int, message: dict) -> None:
        if not self._twitch.settings.make_predictions:
            return
        channel = self._twitch.channels.get(channel_id)
        if not channel:
            return
        cfg = self._get_channel_settings(channel.name)
        if not cfg["make_predictions"]:
            return

        whitelist = [c.lower() for c in self._twitch.settings.prediction_channels]
        if whitelist and channel.name.lower() not in whitelist:
            return

        event = message.get("data", {}).get("event", {})
        event_id = event.get("id", "")
        msg_type = message.get("type", "")

        if msg_type == "event-created":
            self._active_events[event_id] = event
            delay = self._effective_delay(event, cfg)
            if event_id not in self._pending:
                task = asyncio.create_task(
                    self._delayed_bet(event_id, channel, cfg, delay)
                )
                self._pending[event_id] = task
                logger.info(f"Prediction '{event.get('title')}' on {channel.name} — betting in {delay:.1f}s")

        elif msg_type == "event-updated":
            if event_id in self._active_events:
                self._active_events[event_id] = event
            # Twitch resolves predictions via event-updated with status RESOLVED (no separate event-ended)
            if event.get("status") == "RESOLVED":
                if event_id in self._pending:
                    self._pending[event_id].cancel()
                    del self._pending[event_id]
                self._active_events.pop(event_id, None)
                await self._record_result(event_id, event, channel.name)

        elif msg_type in ("event-locked", "event-ended"):
            if event_id in self._pending:
                self._pending[event_id].cancel()
                del self._pending[event_id]
            self._active_events.pop(event_id, None)
            if msg_type == "event-ended":
                await self._record_result(event_id, event, channel.name)

    def _effective_delay(self, event: dict, cfg: dict) -> float:
        """
        Twitch predictions include a fixed window (`prediction_window_seconds`,
        counted from `created_at`) after which the event locks and betting is
        no longer possible. Fast-paced predictions (e.g. per-round esports)
        can have windows well under the configured `bet_delay_seconds` —
        using the fixed delay unconditionally meant the scheduled bet
        routinely got cancelled by the lock before it ever fired, with no
        bet placed and nothing logged (silent since 2026-07-08). Cap the
        delay to whatever's actually left in the window (minus a small
        safety buffer), falling back to the configured delay when the
        window/created_at fields aren't present in the event payload.
        """
        from datetime import datetime, timezone
        window = event.get("prediction_window_seconds")
        created_at = event.get("created_at")
        if not window or not created_at:
            return float(cfg["bet_delay_seconds"])
        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return float(cfg["bet_delay_seconds"])
        elapsed = (datetime.now(timezone.utc) - created).total_seconds()
        remaining = window - elapsed
        safety_buffer = 3  # leave a few seconds of margin before the lock
        return max(min(cfg["bet_delay_seconds"], remaining - safety_buffer), 0.5)

    async def _delayed_bet(self, event_id: str, channel, cfg: dict, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            logger.info(
                f"Prediction {event_id} on {channel.name}: bet cancelled — "
                f"event locked before the scheduled bet fired"
            )
            raise
        try:
            event = self._active_events.get(event_id)
            if not event:
                logger.info(
                    f"Prediction {event_id} on {channel.name}: event already "
                    f"resolved/locked by bet time, skipping"
                )
                return
            outcomes = event.get("outcomes", [])
            chosen = self._choose_outcome(outcomes, cfg)
            if not chosen:
                logger.info(f"Prediction {event_id}: no suitable outcome, skipping")
                return
            # Get current balance
            from src.services.message_handlers import _get_points_file
            import json as _j
            pfile = _get_points_file()
            try:
                pts = _j.loads(pfile.read_text()) if pfile.exists() else {}
                balance = pts.get(channel.name.lower(), 0)
            except Exception:
                balance = 0
            if balance < cfg["bet_minimum_points"]:
                logger.info(f"Prediction {event_id}: balance {balance} < minimum {cfg['bet_minimum_points']}, skipping")
                return
            amount = self._calc_amount(balance, cfg)
            if amount <= 0:
                return
            transaction_id = str(uuid.uuid4())
            try:
                await self._twitch.gql_request(
                    GQL_OPERATIONS["MakePrediction"].with_variables({
                        "input": {
                            "eventID": event_id,
                            "outcomeID": chosen["id"],
                            "points": amount,
                            "transactionID": transaction_id,
                        }
                    })
                )
                logger.info(f"Bet {amount} pts on '{chosen.get('title')}' for prediction '{event.get('title')}' on {channel.name}")
                # Record pending bet
                self._save_pending_bet(event_id, channel.name, event, chosen, amount, cfg["bet_strategy"])
            except Exception as e:
                logger.warning(f"MakePrediction GQL failed: {e}")
        finally:
            # process_prediction only ever pops _active_events/_pending when a
            # LATER event-updated/event-locked/event-ended message for this
            # same event_id arrives — but the miner constantly switches away
            # from channels to chase drops, dropping that channel's PubSub
            # subscription before a still-open prediction resolves. Every
            # early "skip" return above (and the resolved-bet path, previously
            # only popped _pending) left its event permanently in
            # _active_events with nothing left to ever clean it up — an
            # unbounded per-prediction leak that grows with uptime and
            # channel-hopping (reported as ~15GB after a day in Docker).
            # This task's own lifetime is a hard upper bound on how long its
            # entries should live, so clean up both here regardless of which
            # branch returned.
            self._pending.pop(event_id, None)
            self._active_events.pop(event_id, None)

    def _save_pending_bet(self, event_id, channel, event, outcome, amount, strategy):
        from datetime import datetime, timezone
        p = _get_predictions_file()
        try:
            hist = _json.loads(p.read_text()) if p.exists() else []
        except Exception:
            hist = []
        entry = {
            "event_id": event_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "channel": channel.lower(),
            "title": event.get("title", ""),
            "strategy": strategy,
            "outcome_chosen": outcome.get("title", ""),
            "outcome_id": outcome.get("id", ""),
            "points_bet": amount,
            "result": "PENDING",
            "points_won": 0,
        }
        hist.append(entry)
        if len(hist) > MAX_HISTORY:
            hist = hist[-MAX_HISTORY:]
        try:
            p.write_text(_json.dumps(hist, indent=2))
        except Exception:
            pass

    async def _record_result(self, event_id, event, channel_name):
        from datetime import datetime, timezone
        p = _get_predictions_file()
        try:
            hist = _json.loads(p.read_text()) if p.exists() else []
        except Exception:
            return
        winning_outcome_id = (
            event.get("winning_outcome_id")
            or event.get("data", {}).get("winning_outcome_id")
            or ""
        )
        if not winning_outcome_id:
            # fallback: look for badge version "1"
            for o in event.get("outcomes", []):
                if o.get("badge", {}).get("version") == "1":
                    winning_outcome_id = o.get("id", "")
                    break
        for entry in reversed(hist):
            if entry.get("event_id") == event_id and entry.get("result") == "PENDING":
                if winning_outcome_id and entry.get("outcome_id") == winning_outcome_id:
                    entry["result"] = "WIN"
                    # Estimate winnings from odds
                    outcomes = event.get("outcomes", [])
                    total = sum(o.get("total_points", 0) for o in outcomes)
                    chosen_pts = next((o.get("total_points", 1) for o in outcomes if o["id"] == entry["outcome_id"]), 1)
                    odds = total / max(chosen_pts, 1)
                    entry["points_won"] = int(entry["points_bet"] * odds)
                else:
                    entry["result"] = "LOSE"
                    entry["points_won"] = 0
                # Notify frontend so daily-points counter can correct for bet refund on WIN
                try:
                    await self._twitch.gui._broadcaster.emit("prediction_result", {
                        "result": entry["result"],
                        "points_bet": entry["points_bet"],
                    })
                except Exception:
                    pass
                # Discord notification
                webhook = self._twitch.settings.discord_webhook_points
                if webhook:
                    color = 0x00b368 if entry["result"] == "WIN" else 0xeb4a4a
                    embed = {
                        "title": f"{'✅ Win' if entry['result'] == 'WIN' else '❌ Lose'} — Prediction",
                        "color": color,
                        "fields": [
                            {"name": "Channel", "value": entry["channel"], "inline": True},
                            {"name": "Prediction", "value": entry["title"][:100], "inline": False},
                            {"name": "Bet", "value": f"{entry['points_bet']:,} pts on {entry['outcome_chosen']}", "inline": True},
                            {"name": "Profit", "value": f"+{entry['points_won'] - entry['points_bet']:,} pts" if entry["result"] == "WIN" else f"−{entry['points_bet']:,} pts", "inline": True},
                        ],
                    }
                    import aiohttp
                    try:
                        async with aiohttp.ClientSession() as session:
                            await session.post(webhook, json={"embeds": [embed]}, timeout=aiohttp.ClientTimeout(total=10))
                    except Exception:
                        pass
                break
        try:
            p.write_text(_json.dumps(hist, indent=2))
        except Exception:
            pass

    def get_history(self) -> list[dict]:
        p = _get_predictions_file()
        try:
            return _json.loads(p.read_text()) if p.exists() else []
        except Exception:
            return []
