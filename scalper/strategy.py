"""Moteur de signaux de scalping.

Difference majeure avec l'ancien bot : celui-ci exigeait
`prix > EMA20 > EMA50 ET RSI > 55` simultanement sur 2 timeframes, avec un RSI
calcule sur des bougies perimees. Resultat : quasiment jamais de signal.

Ici on utilise un score : chaque condition rapporte un point, et on entre des
que le score depasse `MIN_ENTRY_SCORE`. Baisser ce seuil rend le bot plus
agressif, l'augmenter le rend plus selectif.
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .config import Config
from .indicators import (
    Candle, atr, ema, ema_series, rate_of_change_pct, rsi, volume_ratio,
)

logger = logging.getLogger(__name__)

MAX_SCORE = 6


@dataclass
class Signal:
    symbol: str
    score: int
    price: float
    atr_value: float
    atr_pct: float
    stop_price: float
    take_profit: float
    stop_pct: float
    tp_pct: float
    rsi_value: float
    volume_ratio: float
    reasons: List[str] = field(default_factory=list)

    @property
    def risk_reward(self) -> float:
        return self.tp_pct / self.stop_pct if self.stop_pct > 0 else 0.0


@dataclass
class Rejection:
    symbol: str
    reason: str
    score: int = 0


class ScalpingStrategy:
    def __init__(self, config: Config):
        self.config = config

    # ---------------------------------------------------------------- filtre

    def trend_is_bullish(self, trend_candles: Sequence[Candle]) -> Optional[bool]:
        """Regime haussier sur le timeframe superieur."""
        closes = [c.close for c in trend_candles]
        slow = ema(closes, self.config.ema_slow)
        trend = ema(closes, self.config.ema_trend)
        if slow is None or trend is None:
            return None
        return slow > trend and closes[-1] > trend

    # --------------------------------------------------------------- signaux

    def evaluate(
        self,
        symbol: str,
        entry_candles: Sequence[Candle],
        trend_candles: Sequence[Candle],
    ) -> Signal | Rejection:
        cfg = self.config
        needed = max(cfg.ema_trend, cfg.rsi_period, cfg.atr_period) + 5
        if len(entry_candles) < needed:
            return Rejection(symbol, f"pas assez de bougies ({len(entry_candles)} < {needed})")

        closes = [c.close for c in entry_candles]
        price = closes[-1]
        if price <= 0:
            return Rejection(symbol, "prix invalide")

        fast_series = ema_series(closes, cfg.ema_fast)
        slow_series = ema_series(closes, cfg.ema_slow)
        rsi_value = rsi(closes, cfg.rsi_period)
        atr_value = atr(entry_candles, cfg.atr_period)
        vol_ratio = volume_ratio(entry_candles, 20)
        momentum = rate_of_change_pct(closes, 3)

        if len(fast_series) < 2 or len(slow_series) < 2:
            return Rejection(symbol, "series EMA incompletes")
        if rsi_value is None or atr_value is None:
            return Rejection(symbol, "indicateurs indisponibles")

        ema_fast_now, ema_fast_prev = fast_series[-1], fast_series[-2]
        ema_slow_now, ema_slow_prev = slow_series[-1], slow_series[-2]

        # --- Filtres durs -------------------------------------------------
        atr_pct = (atr_value / price) * 100.0
        if atr_pct < cfg.min_atr_pct:
            return Rejection(symbol, f"volatilite trop faible (ATR {atr_pct:.3f}%)")
        if atr_pct > cfg.max_atr_pct:
            return Rejection(symbol, f"volatilite excessive (ATR {atr_pct:.3f}%)")

        if cfg.require_trend_filter:
            bullish = self.trend_is_bullish(trend_candles)
            if bullish is None:
                return Rejection(symbol, "tendance superieure indisponible")
            if not bullish:
                return Rejection(symbol, f"tendance {cfg.trend_timeframe} non haussiere")

        # --- Score --------------------------------------------------------
        score = 0
        reasons: List[str] = []

        if ema_fast_now > ema_slow_now:
            score += 1
            reasons.append(f"EMA{cfg.ema_fast} > EMA{cfg.ema_slow}")

        if price > ema_fast_now:
            score += 1
            reasons.append("prix au-dessus de l'EMA rapide")

        cross_up = ema_fast_now > ema_slow_now and ema_fast_prev <= ema_slow_prev
        if cross_up:
            score += 1
            reasons.append("croisement haussier des EMA")
        elif price > ema_slow_now:
            score += 1
            reasons.append("prix au-dessus de l'EMA lente")

        if cfg.rsi_long_min <= rsi_value <= cfg.rsi_long_max:
            score += 1
            reasons.append(f"RSI {rsi_value:.1f} dans la zone d'achat")

        if vol_ratio is not None and vol_ratio >= cfg.volume_spike_mult:
            score += 1
            reasons.append(f"volume x{vol_ratio:.2f}")

        if momentum is not None and momentum > 0:
            score += 1
            reasons.append(f"momentum +{momentum:.3f}%")

        if score < cfg.min_entry_score:
            return Rejection(symbol, f"score {score}/{MAX_SCORE} < {cfg.min_entry_score}", score)

        # --- Stop / objectif ---------------------------------------------
        stop_distance = atr_value * cfg.sl_atr_mult
        stop_pct = (stop_distance / price) * 100.0
        stop_pct = max(cfg.min_sl_pct, min(cfg.max_sl_pct, stop_pct))
        stop_distance = price * stop_pct / 100.0

        risk_reward = cfg.tp_atr_mult / cfg.sl_atr_mult
        tp_distance = stop_distance * risk_reward
        tp_pct = (tp_distance / price) * 100.0

        # Un scalp dont l'objectif ne couvre pas les frais aller-retour est
        # perdant meme quand il "gagne".
        min_tp = cfg.min_profitable_tp_pct()
        if tp_pct < min_tp:
            return Rejection(
                symbol,
                f"TP {tp_pct:.3f}% sous le seuil de rentabilite apres frais ({min_tp:.3f}%)",
                score,
            )

        return Signal(
            symbol=symbol,
            score=score,
            price=price,
            atr_value=atr_value,
            atr_pct=atr_pct,
            stop_price=price - stop_distance,
            take_profit=price + tp_distance,
            stop_pct=stop_pct,
            tp_pct=tp_pct,
            rsi_value=rsi_value,
            volume_ratio=vol_ratio or 0.0,
            reasons=reasons,
        )

    # --------------------------------------------------------------- sorties

    def exit_decision(self, position, price: float) -> Optional[str]:
        """Retourne le motif de cloture, ou None pour rester en position."""
        cfg = self.config

        if price <= position.stop_price:
            if position.trailing_active:
                return "TRAILING_STOP"
            if position.breakeven_done and position.stop_price >= position.entry_price:
                return "BREAKEVEN"
            return "STOP_LOSS"

        if price >= position.take_profit:
            return "TAKE_PROFIT"

        if cfg.max_hold_seconds > 0 and position.age_seconds() >= cfg.max_hold_seconds:
            return "TIMEOUT"

        return None

    def update_trailing(self, position, price: float) -> bool:
        """Fait remonter le stop. Retourne True si le stop a bouge."""
        cfg = self.config
        changed = False

        if price > position.highest_price:
            position.highest_price = price
            changed = True

        r_multiple = position.unrealized_r(price)

        # 1) Passage au point mort une fois `breakeven_at_r` atteint.
        if (
            not position.breakeven_done
            and cfg.breakeven_at_r > 0
            and r_multiple >= cfg.breakeven_at_r
        ):
            fee_cushion = position.entry_price * (cfg.fee_rate * 2)
            new_stop = position.entry_price + fee_cushion
            if new_stop > position.stop_price:
                position.stop_price = new_stop
                position.breakeven_done = True
                changed = True

        # 2) Stop suiveur au-dela de `trail_activate_r`.
        if cfg.trail_activate_r > 0 and r_multiple >= cfg.trail_activate_r:
            trail_stop = position.highest_price - position.atr * cfg.trail_atr_mult
            if trail_stop > position.stop_price:
                position.stop_price = trail_stop
                position.trailing_active = True
                changed = True

        return changed
