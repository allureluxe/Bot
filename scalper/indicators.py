"""Indicateurs techniques.

Toutes les fonctions renvoient la valeur *courante* (basee sur la fin de la
serie). C'est le bug principal de l'ancien bot : son RSI etait calcule sur les
15 bougies les plus anciennes du buffer, donc toujours perime.
"""
from dataclasses import dataclass
from typing import List, Optional, Sequence


@dataclass(frozen=True)
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int

    @staticmethod
    def from_binance(row: Sequence) -> "Candle":
        return Candle(
            open_time=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            close_time=int(row[6]),
        )


def ema_series(values: Sequence[float], period: int) -> List[float]:
    """Serie complete d'EMA, alignee sur la fin de `values`."""
    if period <= 0 or len(values) < period:
        return []
    multiplier = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    out = [seed]
    for price in values[period:]:
        seed = (price - seed) * multiplier + seed
        out.append(seed)
    return out


def ema(values: Sequence[float], period: int) -> Optional[float]:
    series = ema_series(values, period)
    return series[-1] if series else None


def rsi(values: Sequence[float], period: int = 14) -> Optional[float]:
    """RSI de Wilder calcule sur la totalite de la serie (valeur courante).

    Contrairement a une moyenne simple sur les `period` premieres variations,
    on applique le lissage de Wilder jusqu'a la derniere bougie.
    """
    if period <= 0 or len(values) < period + 1:
        return None

    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change

    avg_gain = gains / period
    avg_loss = losses / period

    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(candles: Sequence[Candle], period: int = 14) -> Optional[float]:
    """Average True Range (lissage de Wilder), valeur courante."""
    if period <= 0 or len(candles) < period + 1:
        return None

    true_ranges: List[float] = []
    for i in range(1, len(candles)):
        current = candles[i]
        prev_close = candles[i - 1].close
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - prev_close),
                abs(current.low - prev_close),
            )
        )

    if len(true_ranges) < period:
        return None

    value = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def sma(values: Sequence[float], period: int) -> Optional[float]:
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def rate_of_change_pct(values: Sequence[float], lookback: int = 3) -> Optional[float]:
    """Momentum: variation en % sur les `lookback` dernieres bougies."""
    if lookback <= 0 or len(values) < lookback + 1:
        return None
    past = values[-1 - lookback]
    if past == 0:
        return None
    return ((values[-1] - past) / past) * 100.0


def volume_ratio(candles: Sequence[Candle], lookback: int = 20) -> Optional[float]:
    """Volume de la derniere bougie rapporte a la moyenne des precedentes."""
    if len(candles) < lookback + 1:
        return None
    window = candles[-lookback - 1:-1]
    average = sum(c.volume for c in window) / len(window)
    if average <= 0:
        return None
    return candles[-1].volume / average
