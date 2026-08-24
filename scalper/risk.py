"""Money management : dimensionnement progressif / regressif et coupe-circuits.

Principes :
1. La taille est derivee du RISQUE, pas d'un montant fixe.
   quantite = (capital x risque%) / distance_au_stop
   -> chaque perte coute le meme pourcentage du capital, quel que soit l'actif.
2. Progressif : le lot suit le capital (compounding) et augmente apres une
   serie de gains.
3. Regressif : le lot diminue apres une serie de pertes et quand le capital
   baisse. Jamais de martingale (on n'augmente JAMAIS apres une perte).
4. Le risque en % decroit quand le capital grossit (protection du capital).
"""
import logging
import math
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from .config import Config
from .state import BotState, today_key

logger = logging.getLogger(__name__)


@dataclass
class SizingResult:
    quantity: float = 0.0
    notional: float = 0.0
    risk_pct: float = 0.0
    risk_amount: float = 0.0
    ok: bool = False
    reason: str = ""


def floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    # Arrondi defensif : 0.1/0.001 en flottant donne 99.99999 -> floor casse.
    return math.floor(round(value / step, 10)) * step


def step_decimals(step: float) -> int:
    if step <= 0:
        return 8
    text = f"{step:.10f}".rstrip("0")
    if "." not in text:
        return 0
    return len(text.split(".")[1])


def format_quantity(quantity: float, step: float) -> str:
    return f"{quantity:.{step_decimals(step)}f}"


class RiskManager:
    def __init__(self, config: Config, state: BotState):
        self.config = config
        self.state = state

    # ------------------------------------------------------------- calendrier

    def roll_day_if_needed(self, equity: float) -> bool:
        """Reinitialise les compteurs journaliers au changement de jour UTC."""
        key = today_key()
        if self.state.daily.day == key:
            return False
        logger.info("Nouvelle journee de trading (%s) - compteurs remis a zero", key)
        self.state.daily.day = key
        self.state.daily.starting_equity = equity
        self.state.daily.realized_pnl = 0.0
        self.state.daily.trades = 0
        self.state.daily.wins = 0
        self.state.daily.losses = 0
        self.state.daily.fees_paid = 0.0
        self.state.halted_until_next_day = False
        self.state.halt_reason = ""
        return True

    # ------------------------------------------------------- calcul du risque

    def equity_scale(self, equity: float) -> float:
        """Le risque en % decroit quand le capital augmente.

        equity_decay = 0   -> risque constant
        equity_decay = 0.15 -> petit compte legerement plus agressif,
                               gros compte plus prudent.
        """
        if equity <= 0 or self.config.equity_ref <= 0 or self.config.equity_decay <= 0:
            return 1.0
        return (self.config.equity_ref / equity) ** self.config.equity_decay

    def streak_multiplier(self) -> float:
        """Progressif sur les gains, regressif sur les pertes."""
        if self.state.loss_streak > 0:
            return max(
                self.config.min_streak_mult,
                self.config.loss_streak_step ** self.state.loss_streak,
            )
        if self.state.win_streak > 0:
            return min(
                self.config.max_streak_mult,
                self.config.win_streak_step ** self.state.win_streak,
            )
        return 1.0

    def drawdown_multiplier(self) -> float:
        """Reduit de moitie le risque une fois la perte douce du jour atteinte."""
        pnl_pct = self.state.daily.pnl_pct()
        if self.config.daily_soft_loss_pct > 0 and pnl_pct <= -self.config.daily_soft_loss_pct:
            return 0.5
        return 1.0

    def current_risk_pct(self, equity: float) -> float:
        risk = (
            self.config.base_risk_pct
            * self.equity_scale(equity)
            * self.streak_multiplier()
            * self.drawdown_multiplier()
        )
        return max(self.config.min_risk_pct, min(self.config.max_risk_pct, risk))

    # --------------------------------------------------------- autorisations

    def open_exposure(self) -> float:
        return sum(p.notional for p in self.state.positions.values())

    def can_open(self, symbol: str, equity: float) -> Tuple[bool, str]:
        if self.state.paused:
            return False, "bot en pause"
        if self.state.halted_until_next_day:
            return False, f"coupe-circuit actif : {self.state.halt_reason}"
        if symbol in self.state.positions:
            return False, "position deja ouverte sur ce symbole"
        if len(self.state.positions) >= self.config.max_open_positions:
            return False, f"maximum de {self.config.max_open_positions} positions atteint"

        cooldown_until = self.state.cooldowns.get(symbol, 0)
        if cooldown_until > time.time():
            return False, f"cooldown {int(cooldown_until - time.time())}s apres une perte"

        if self.config.max_trades_per_day > 0 and self.state.daily.trades >= self.config.max_trades_per_day:
            return False, f"quota journalier atteint ({self.config.max_trades_per_day} trades)"

        if self.config.max_consecutive_losses > 0 and self.state.loss_streak >= self.config.max_consecutive_losses:
            return False, f"{self.state.loss_streak} pertes consecutives : pause de securite"

        max_exposure = equity * self.config.max_total_exposure_pct / 100.0
        if self.open_exposure() >= max_exposure:
            return False, f"exposition totale maximale atteinte ({self.config.max_total_exposure_pct}%)"

        return True, ""

    def check_daily_circuit_breakers(self, equity: float) -> Optional[str]:
        """Retourne un motif d'arret si un seuil journalier est franchi."""
        if self.state.daily.starting_equity <= 0:
            return None
        pnl_pct = self.state.daily.pnl_pct()

        if pnl_pct <= -self.config.daily_max_loss_pct:
            return (
                f"perte journaliere de {pnl_pct:.2f}% "
                f"(limite {self.config.daily_max_loss_pct}%)"
            )
        if self.config.daily_profit_target_pct > 0 and pnl_pct >= self.config.daily_profit_target_pct:
            return (
                f"objectif journalier atteint : +{pnl_pct:.2f}% "
                f"(cible {self.config.daily_profit_target_pct}%)"
            )
        return None

    # ------------------------------------------------------- dimensionnement

    def size_position(
        self,
        equity: float,
        available_quote: float,
        entry_price: float,
        stop_price: float,
        filters: Dict[str, float],
    ) -> SizingResult:
        if entry_price <= 0:
            return SizingResult(reason="prix d'entree invalide")
        stop_distance = entry_price - stop_price
        if stop_distance <= 0:
            return SizingResult(reason="stop loss au-dessus du prix d'entree")

        step = filters.get("step_size", 0.0)
        if step <= 0:
            return SizingResult(reason="stepSize Binance invalide")

        risk_pct = self.current_risk_pct(equity)
        risk_amount = equity * risk_pct / 100.0
        if risk_amount <= 0:
            return SizingResult(risk_pct=risk_pct, reason="capital insuffisant")

        # Taille theorique issue du risque.
        quantity = risk_amount / stop_distance

        # Plafonds : part du capital, solde disponible, exposition globale.
        max_position_notional = equity * self.config.max_position_pct / 100.0
        remaining_exposure = max(
            0.0, equity * self.config.max_total_exposure_pct / 100.0 - self.open_exposure()
        )
        cap_notional = min(max_position_notional, available_quote * 0.98, remaining_exposure)
        if cap_notional <= 0:
            return SizingResult(risk_pct=risk_pct, reason="aucune marge d'exposition disponible")

        quantity = min(quantity, cap_notional / entry_price)
        quantity = floor_to_step(quantity, step)

        min_qty = filters.get("min_qty", 0.0)
        min_notional = filters.get("min_notional", 0.0)

        # Remontee au minimum impose par Binance, si le budget le permet.
        if quantity < min_qty:
            quantity = floor_to_step(min_qty, step)
            if quantity < min_qty:
                quantity += step

        notional = quantity * entry_price
        if min_notional > 0 and notional < min_notional:
            steps_needed = math.ceil((min_notional / entry_price) / step)
            quantity = steps_needed * step
            notional = quantity * entry_price

        max_qty = filters.get("max_qty", 0.0)
        if max_qty > 0 and quantity > max_qty:
            quantity = floor_to_step(max_qty, step)
            notional = quantity * entry_price

        if quantity <= 0:
            return SizingResult(risk_pct=risk_pct, reason="quantite calculee nulle")
        if notional > available_quote * 0.98:
            return SizingResult(
                risk_pct=risk_pct,
                reason=(
                    f"minimum Binance {notional:.2f} > solde disponible "
                    f"{available_quote:.2f} {self.config.quote_asset}"
                ),
            )
        if notional > cap_notional * 1.0001:
            return SizingResult(
                risk_pct=risk_pct,
                reason=(
                    f"le minimum Binance ({notional:.2f}) depasse le plafond par position "
                    f"({cap_notional:.2f} {self.config.quote_asset}) - capital trop faible "
                    f"pour ce symbole"
                ),
            )

        # Le minimum Binance peut forcer un risque superieur a la cible : on
        # verifie que le depassement reste acceptable.
        effective_risk = quantity * stop_distance
        effective_risk_pct = (effective_risk / equity * 100.0) if equity > 0 else 0.0
        if effective_risk_pct > self.config.max_risk_pct * 1.5:
            return SizingResult(
                risk_pct=risk_pct,
                reason=(
                    f"risque effectif {effective_risk_pct:.2f}% > plafond "
                    f"{self.config.max_risk_pct * 1.5:.2f}% (stop trop large pour ce capital)"
                ),
            )

        return SizingResult(
            quantity=quantity,
            notional=notional,
            risk_pct=risk_pct,
            risk_amount=effective_risk,
            ok=True,
        )

    # ------------------------------------------------------------ resultats

    def register_result(self, symbol: str, pnl: float, fees: float) -> None:
        """Met a jour series, statistiques et cooldown apres une cloture."""
        self.state.daily.trades += 1
        self.state.daily.realized_pnl += pnl
        self.state.daily.fees_paid += fees
        self.state.total_trades += 1
        self.state.total_pnl += pnl

        if pnl > 0:
            self.state.daily.wins += 1
            self.state.win_streak += 1
            self.state.loss_streak = 0
        else:
            self.state.daily.losses += 1
            self.state.loss_streak += 1
            self.state.win_streak = 0
            if self.config.symbol_cooldown_seconds > 0:
                self.state.cooldowns[symbol] = time.time() + self.config.symbol_cooldown_seconds

        # Purge des cooldowns expires.
        now = time.time()
        self.state.cooldowns = {s: t for s, t in self.state.cooldowns.items() if t > now}
