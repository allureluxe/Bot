"""Backtest rapide sur historique Binance.

Sert a repondre a une seule question avant de risquer de l'argent :
« est-ce que cette configuration declenche des trades, et a quel rythme ? »

Hypotheses volontairement pessimistes :
- entree au prix de cloture de la bougie de signal (pas au meilleur prix),
- si SL et TP sont touches dans la meme bougie, on considere le SL touche,
- frais preleves a l'aller et au retour.
Un backtest reste une approximation : il ne modele ni le slippage reel ni la
profondeur du carnet.
"""
import bisect
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .config import Config
from .exchange import BinanceClient
from .indicators import Candle
from .risk import RiskManager
from .state import BotState, Position
from .strategy import ScalpingStrategy, Signal

logger = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    symbol: str = ""
    candles: int = 0
    signals: int = 0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    pnl: float = 0.0
    fees: float = 0.0
    max_drawdown_pct: float = 0.0
    reasons: Dict[str, int] = field(default_factory=dict)
    rejections: Dict[str, int] = field(default_factory=dict)
    hours_covered: float = 0.0

    @property
    def win_rate(self) -> float:
        closed = self.wins + self.losses
        return (self.wins / closed * 100.0) if closed else 0.0

    @property
    def trades_per_day(self) -> float:
        return (self.trades / self.hours_covered * 24.0) if self.hours_covered else 0.0


def _trend_slice(trend_candles: Sequence[Candle], close_times: List[int], upto: int) -> List[Candle]:
    index = bisect.bisect_right(close_times, upto)
    return list(trend_candles[:index])


def backtest_symbol(
    config: Config,
    symbol: str,
    entry_candles: Sequence[Candle],
    trend_candles: Sequence[Candle],
    starting_equity: float = 1000.0,
) -> BacktestResult:
    strategy = ScalpingStrategy(config)
    state = BotState()
    state.daily.starting_equity = starting_equity
    risk = RiskManager(config, state)

    result = BacktestResult(symbol=symbol, candles=len(entry_candles))
    if len(entry_candles) < 60:
        return result

    result.hours_covered = (
        (entry_candles[-1].close_time - entry_candles[0].open_time) / 3_600_000.0
    )

    trend_close_times = [c.close_time for c in trend_candles]
    # Filtres synthetiques : pas de contrainte d'echange en backtest.
    filters = {"step_size": 1e-8, "min_qty": 0.0, "min_notional": 0.0, "max_qty": 0.0}

    equity = starting_equity
    peak = starting_equity
    warmup = max(config.ema_trend, config.rsi_period, config.atr_period) + 6
    open_position: Optional[Position] = None

    for i in range(warmup, len(entry_candles)):
        candle = entry_candles[i]

        # --- Gestion de la position ouverte, bougie par bougie -------------
        if open_position is not None:
            # Les sorties sont testees contre le stop tel qu'il etait AU DEBUT
            # de la bougie ; le stop suiveur ne remonte qu'ensuite, pour la
            # bougie suivante. Remonter le stop avant de tester la sortie
            # ferait sortir a un niveau qui n'existait pas encore.
            exit_price: Optional[float] = None
            reason = ""

            # Pessimiste : le stop est teste avant l'objectif.
            if candle.low <= open_position.stop_price:
                exit_price = open_position.stop_price
                reason = "TRAILING_STOP" if open_position.trailing_active else "STOP_LOSS"
            elif candle.high >= open_position.take_profit:
                exit_price = open_position.take_profit
                reason = "TAKE_PROFIT"
            elif (
                config.max_hold_seconds > 0
                and (candle.close_time - open_position.opened_at * 1000) / 1000.0
                >= config.max_hold_seconds
            ):
                exit_price = candle.close
                reason = "TIMEOUT"

            if exit_price is not None:
                qty = open_position.quantity
                entry_notional = open_position.entry_price * qty
                exit_notional = exit_price * qty
                fees = (entry_notional + exit_notional) * config.fee_rate
                pnl = exit_notional - entry_notional - fees

                equity += pnl
                result.pnl += pnl
                result.fees += fees
                result.trades += 1
                result.reasons[reason] = result.reasons.get(reason, 0) + 1
                if pnl > 0:
                    result.wins += 1
                else:
                    result.losses += 1
                risk.register_result(symbol, pnl, fees)
                state.cooldowns.clear()  # le cooldown temps reel n'a pas de sens ici

                peak = max(peak, equity)
                if peak > 0:
                    drawdown = (peak - equity) / peak * 100.0
                    result.max_drawdown_pct = max(result.max_drawdown_pct, drawdown)

                open_position = None
            else:
                strategy.update_trailing(open_position, candle.high)
                continue

        # --- Recherche d'une entree ---------------------------------------
        window = list(entry_candles[: i + 1])
        trend_window = (
            _trend_slice(trend_candles, trend_close_times, candle.close_time)
            if config.require_trend_filter else []
        )
        outcome = strategy.evaluate(symbol, window, trend_window)

        if not isinstance(outcome, Signal):
            key = outcome.reason.split("(")[0].strip()
            result.rejections[key] = result.rejections.get(key, 0) + 1
            continue

        result.signals += 1
        sizing = risk.size_position(
            equity, equity, outcome.price, outcome.stop_price, filters
        )
        if not sizing.ok:
            result.rejections["dimensionnement"] = result.rejections.get("dimensionnement", 0) + 1
            continue

        open_position = Position(
            symbol=symbol,
            side="LONG",
            quantity=sizing.quantity,
            entry_price=outcome.price,
            stop_price=outcome.stop_price,
            take_profit=outcome.take_profit,
            initial_stop=outcome.stop_price,
            atr=outcome.atr_value,
            opened_at=candle.close_time / 1000.0,
            risk_amount=sizing.risk_amount,
            notional=sizing.notional,
            highest_price=outcome.price,
            dry_run=True,
        )

    return result


async def run_backtest(
    config: Config, client: BinanceClient, symbols: List[str], limit: int = 1000
) -> List[BacktestResult]:
    results: List[BacktestResult] = []
    await client.get_exchange_info()
    for symbol in symbols:
        try:
            entry_candles = await client.get_candles(symbol, config.entry_timeframe, limit)
            trend_candles = (
                await client.get_candles(symbol, config.trend_timeframe, limit)
                if config.require_trend_filter else []
            )
        except Exception as exc:
            logger.warning("%s : donnees indisponibles (%s)", symbol, exc)
            continue
        results.append(backtest_symbol(config, symbol, entry_candles, trend_candles))
    return results


def render_backtest(results: List[BacktestResult], config: Config) -> str:
    lines = ["=" * 78, "  BACKTEST", "=" * 78]
    lines.append(
        f"Timeframe {config.entry_timeframe} | tendance {config.trend_timeframe} | "
        f"score min {config.min_entry_score}/6 | SL {config.sl_atr_mult}xATR | "
        f"TP {config.tp_atr_mult}xATR | frais {config.fee_rate * 100:.3f}%/cote"
    )
    lines.append("-" * 78)
    lines.append(
        f"{'Paire':<12}{'Trades':>7}{'/jour':>7}{'Reussite':>10}{'PnL %':>10}{'DD max':>9}{'Frais':>10}"
    )
    lines.append("-" * 78)

    total_trades = 0
    total_pnl = 0.0
    total_fees = 0.0
    total_hours = 0.0
    all_rejections: Dict[str, int] = {}

    for result in results:
        total_trades += result.trades
        total_pnl += result.pnl
        total_fees += result.fees
        total_hours = max(total_hours, result.hours_covered)
        for key, count in result.rejections.items():
            all_rejections[key] = all_rejections.get(key, 0) + count
        lines.append(
            f"{result.symbol:<12}{result.trades:>7}{result.trades_per_day:>7.1f}"
            f"{result.win_rate:>9.1f}%{result.pnl / 10.0:>10.2f}"
            f"{result.max_drawdown_pct:>8.2f}%{result.fees:>10.2f}"
        )

    lines.append("-" * 78)
    lines.append(
        f"TOTAL : {total_trades} trade(s) sur ~{total_hours:.1f} h "
        f"({total_trades / total_hours * 24 if total_hours else 0:.1f}/jour toutes paires confondues)"
    )
    lines.append(
        f"PnL cumule : {total_pnl:+.2f} (base 1000/paire) | frais payes : {total_fees:.2f}"
    )

    if total_trades == 0:
        lines.append("")
        lines.append("AUCUN TRADE. Principales causes de refus :")
        for reason, count in sorted(all_rejections.items(), key=lambda x: -x[1])[:6]:
            lines.append(f"  - {reason} : {count} fois")
        lines.append("")
        lines.append("Pistes : baisser MIN_ENTRY_SCORE, mettre REQUIRE_TREND_FILTER=false,")
        lines.append("ou baisser MIN_ATR_PCT.")

    lines.append("=" * 78)
    return "\n".join(lines)
