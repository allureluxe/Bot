"""Etat persistant du bot.

L'ancien bot gardait `active_trades` dans un dict en memoire : chaque
redemarrage (toutes les 5 h sur GitHub Actions) effacait les positions, qui
devenaient orphelines et n'etaient plus jamais fermees. Ici tout est ecrit sur
disque a chaque changement.
"""
import csv
import json
import logging
import os
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Position:
    symbol: str
    side: str                    # "LONG" (Binance Spot = long uniquement)
    quantity: float
    entry_price: float
    stop_price: float
    take_profit: float
    initial_stop: float
    atr: float
    opened_at: float
    risk_amount: float
    notional: float
    order_id: Optional[str] = None
    highest_price: float = 0.0
    breakeven_done: bool = False
    trailing_active: bool = False
    dry_run: bool = False

    def __post_init__(self) -> None:
        if self.highest_price <= 0:
            self.highest_price = self.entry_price

    @property
    def stop_distance(self) -> float:
        return max(1e-12, self.entry_price - self.initial_stop)

    def unrealized_r(self, price: float) -> float:
        """Profit courant exprime en multiples du risque initial (R)."""
        return (price - self.entry_price) / self.stop_distance

    def unrealized_pct(self, price: float) -> float:
        return ((price - self.entry_price) / self.entry_price) * 100.0

    def age_seconds(self) -> float:
        return time.time() - self.opened_at

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "Position":
        known = {f for f in Position.__dataclass_fields__}
        return Position(**{k: v for k, v in data.items() if k in known})


@dataclass
class DailyStats:
    day: str = ""
    starting_equity: float = 0.0
    realized_pnl: float = 0.0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    fees_paid: float = 0.0

    def pnl_pct(self) -> float:
        if self.starting_equity <= 0:
            return 0.0
        return (self.realized_pnl / self.starting_equity) * 100.0

    def win_rate(self) -> float:
        closed = self.wins + self.losses
        return (self.wins / closed * 100.0) if closed else 0.0


@dataclass
class BotState:
    positions: Dict[str, Position] = field(default_factory=dict)
    daily: DailyStats = field(default_factory=DailyStats)
    win_streak: int = 0
    loss_streak: int = 0
    cooldowns: Dict[str, float] = field(default_factory=dict)
    halted_until_next_day: bool = False
    halt_reason: str = ""
    paused: bool = False
    total_trades: int = 0
    total_pnl: float = 0.0


def today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class StateStore:
    """Lecture/ecriture atomique de l'etat + journal CSV des trades."""

    def __init__(self, state_file: str, trade_log_file: str):
        self.state_file = state_file
        self.trade_log_file = trade_log_file
        for path in (state_file, trade_log_file):
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)

    def load(self) -> BotState:
        if not os.path.exists(self.state_file):
            return BotState()
        try:
            with open(self.state_file, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Etat illisible (%s), redemarrage a vide", exc)
            return BotState()

        state = BotState()
        state.positions = {
            symbol: Position.from_dict(data)
            for symbol, data in raw.get("positions", {}).items()
        }
        daily_raw = raw.get("daily", {})
        state.daily = DailyStats(**{
            k: v for k, v in daily_raw.items() if k in DailyStats.__dataclass_fields__
        })
        state.win_streak = raw.get("win_streak", 0)
        state.loss_streak = raw.get("loss_streak", 0)
        state.cooldowns = raw.get("cooldowns", {})
        state.halted_until_next_day = raw.get("halted_until_next_day", False)
        state.halt_reason = raw.get("halt_reason", "")
        state.paused = raw.get("paused", False)
        state.total_trades = raw.get("total_trades", 0)
        state.total_pnl = raw.get("total_pnl", 0.0)
        logger.info(
            "Etat recharge : %d position(s) ouverte(s), %d trade(s) aujourd'hui",
            len(state.positions), state.daily.trades,
        )
        return state

    def save(self, state: BotState) -> None:
        payload = {
            "positions": {s: p.to_dict() for s, p in state.positions.items()},
            "daily": asdict(state.daily),
            "win_streak": state.win_streak,
            "loss_streak": state.loss_streak,
            "cooldowns": state.cooldowns,
            "halted_until_next_day": state.halted_until_next_day,
            "halt_reason": state.halt_reason,
            "paused": state.paused,
            "total_trades": state.total_trades,
            "total_pnl": state.total_pnl,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        temp = f"{self.state_file}.tmp"
        try:
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
            os.replace(temp, self.state_file)
        except OSError as exc:
            logger.error("Impossible d'ecrire l'etat : %s", exc)

    def log_trade(self, row: Dict[str, Any]) -> None:
        columns = [
            "closed_at", "symbol", "side", "quantity", "entry_price", "exit_price",
            "stop_price", "take_profit", "pnl", "pnl_pct", "r_multiple", "fees",
            "reason", "hold_seconds", "risk_pct", "dry_run",
        ]
        exists = os.path.exists(self.trade_log_file)
        try:
            with open(self.trade_log_file, "a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
                if not exists:
                    writer.writeheader()
                writer.writerow(row)
        except OSError as exc:
            logger.error("Impossible d'ecrire le journal de trades : %s", exc)
