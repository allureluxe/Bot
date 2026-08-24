import math
import time
import unittest

from scalper.backtest import backtest_symbol
from scalper.config import Config
from scalper.indicators import Candle
from scalper.state import Position
from scalper.strategy import Rejection, ScalpingStrategy, Signal


def build_candles(closes, volumes=None, spread=0.0015):
    volumes = volumes or [100.0] * len(closes)
    candles = []
    for i, (close, volume) in enumerate(zip(closes, volumes)):
        previous = closes[i - 1] if i > 0 else close
        candles.append(Candle(
            open_time=i * 60_000,
            open=previous,
            high=max(previous, close) * (1 + spread),
            low=min(previous, close) * (1 - spread),
            close=close,
            volume=volume,
            close_time=i * 60_000 + 59_999,
        ))
    return candles


def uptrend_closes(n=140, drift=0.0009, wave=0.0025):
    """Tendance haussiere avec respirations : RSI realiste, pas sature a 100."""
    return [
        100.0 * (1 + drift * i) * (1 + wave * math.sin(i / 3.0))
        for i in range(n)
    ]


def downtrend_closes(n=140):
    return [100.0 * (1 - 0.0009 * i) * (1 + 0.002 * math.sin(i / 3.0)) for i in range(n)]


def flat_closes(n=140):
    return [100.0 + 0.00001 * math.sin(i / 4.0) for i in range(n)]


def make_config(**overrides) -> Config:
    config = Config()
    config.dry_run = True
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


class TestSignalGeneration(unittest.TestCase):
    def test_uptrend_produces_signal(self):
        """Test central : sur une tendance haussiere nette, le bot DOIT entrer.

        C'est precisement ce que l'ancien bot ne faisait jamais.
        """
        config = make_config()
        strategy = ScalpingStrategy(config)
        closes = uptrend_closes()
        volumes = [100.0] * (len(closes) - 1) + [300.0]
        candles = build_candles(closes, volumes)

        outcome = strategy.evaluate("TESTUSDT", candles, candles)
        self.assertIsInstance(
            outcome, Signal,
            f"aucun signal genere : {getattr(outcome, 'reason', '')}",
        )
        self.assertGreaterEqual(outcome.score, config.min_entry_score)
        self.assertLess(outcome.stop_price, outcome.price)
        self.assertGreater(outcome.take_profit, outcome.price)

    def test_downtrend_is_rejected(self):
        strategy = ScalpingStrategy(make_config())
        candles = build_candles(downtrend_closes())
        self.assertIsInstance(strategy.evaluate("TESTUSDT", candles, candles), Rejection)

    def test_flat_market_rejected_for_low_volatility(self):
        strategy = ScalpingStrategy(make_config())
        candles = build_candles(flat_closes(), spread=0.000001)
        outcome = strategy.evaluate("TESTUSDT", candles, candles)
        self.assertIsInstance(outcome, Rejection)
        self.assertIn("volatilite", outcome.reason)

    def test_lower_score_is_more_aggressive(self):
        """Baisser MIN_ENTRY_SCORE doit produire au moins autant de signaux."""
        closes = uptrend_closes()
        candles = build_candles(closes)

        def count_signals(min_score):
            strategy = ScalpingStrategy(make_config(min_entry_score=min_score))
            hits = 0
            for i in range(60, len(candles)):
                window = candles[: i + 1]
                if isinstance(strategy.evaluate("T", window, window), Signal):
                    hits += 1
            return hits

        self.assertGreaterEqual(count_signals(3), count_signals(5))

    def test_insufficient_history_rejected(self):
        strategy = ScalpingStrategy(make_config())
        candles = build_candles(uptrend_closes(20))
        outcome = strategy.evaluate("TESTUSDT", candles, candles)
        self.assertIsInstance(outcome, Rejection)
        self.assertIn("bougies", outcome.reason)

    def test_stop_respects_bounds(self):
        config = make_config(min_sl_pct=0.4, max_sl_pct=0.9)
        strategy = ScalpingStrategy(config)
        candles = build_candles(uptrend_closes())
        outcome = strategy.evaluate("TESTUSDT", candles, candles)
        if isinstance(outcome, Signal):
            self.assertGreaterEqual(outcome.stop_pct, 0.4 - 1e-9)
            self.assertLessEqual(outcome.stop_pct, 0.9 + 1e-9)

    def test_tp_below_fee_threshold_rejected(self):
        """Un objectif qui ne couvre pas les frais doit etre refuse."""
        config = make_config(fee_rate=0.02, fee_safety_mult=2.0)  # frais absurdes
        strategy = ScalpingStrategy(config)
        candles = build_candles(uptrend_closes())
        outcome = strategy.evaluate("TESTUSDT", candles, candles)
        self.assertIsInstance(outcome, Rejection)
        self.assertIn("rentabilite", outcome.reason)

    def test_trend_filter_can_block_entry(self):
        strategy = ScalpingStrategy(make_config(require_trend_filter=True))
        up = build_candles(uptrend_closes())
        down = build_candles(downtrend_closes())
        outcome = strategy.evaluate("TESTUSDT", up, down)
        self.assertIsInstance(outcome, Rejection)
        self.assertIn("tendance", outcome.reason)


def make_position(entry=100.0, stop=99.0, tp=102.0, atr=0.5, age=0.0):
    return Position(
        symbol="TESTUSDT", side="LONG", quantity=1.0, entry_price=entry,
        stop_price=stop, take_profit=tp, initial_stop=stop, atr=atr,
        opened_at=time.time() - age, risk_amount=entry - stop, notional=entry,
        highest_price=entry,
    )


class TestExitLogic(unittest.TestCase):
    def test_stop_loss_triggers(self):
        strategy = ScalpingStrategy(make_config())
        self.assertEqual(strategy.exit_decision(make_position(), 98.5), "STOP_LOSS")

    def test_take_profit_triggers(self):
        strategy = ScalpingStrategy(make_config())
        self.assertEqual(strategy.exit_decision(make_position(), 102.5), "TAKE_PROFIT")

    def test_hold_between_levels(self):
        strategy = ScalpingStrategy(make_config(max_hold_seconds=0))
        self.assertIsNone(strategy.exit_decision(make_position(), 100.5))

    def test_timeout_closes_stale_position(self):
        strategy = ScalpingStrategy(make_config(max_hold_seconds=60))
        self.assertEqual(strategy.exit_decision(make_position(age=120), 100.5), "TIMEOUT")

    def test_breakeven_moves_stop_up(self):
        config = make_config(breakeven_at_r=0.5, trail_activate_r=99)
        strategy = ScalpingStrategy(config)
        position = make_position()
        strategy.update_trailing(position, 100.7)  # +0.7R
        self.assertTrue(position.breakeven_done)
        self.assertGreaterEqual(position.stop_price, position.entry_price)

    def test_trailing_stop_follows_price(self):
        config = make_config(trail_activate_r=1.0, trail_atr_mult=0.5, breakeven_at_r=0)
        strategy = ScalpingStrategy(config)
        position = make_position(atr=0.4)
        strategy.update_trailing(position, 101.5)
        self.assertTrue(position.trailing_active)
        first_stop = position.stop_price
        strategy.update_trailing(position, 103.0)
        self.assertGreater(position.stop_price, first_stop)

    def test_stop_never_moves_down(self):
        config = make_config(trail_activate_r=1.0, trail_atr_mult=0.5)
        strategy = ScalpingStrategy(config)
        position = make_position(atr=0.4)
        strategy.update_trailing(position, 103.0)
        raised = position.stop_price
        strategy.update_trailing(position, 100.2)  # repli
        self.assertEqual(position.stop_price, raised)

    def test_unrealized_r_is_consistent(self):
        position = make_position(entry=100.0, stop=99.0)
        self.assertAlmostEqual(position.unrealized_r(102.0), 2.0, places=6)
        self.assertAlmostEqual(position.unrealized_r(99.0), -1.0, places=6)


class TestBacktestIntegration(unittest.TestCase):
    def test_backtest_executes_trades_on_trending_data(self):
        """Bout en bout : donnees -> signaux -> dimensionnement -> trades."""
        config = make_config(max_hold_seconds=0, symbol_cooldown_seconds=0)
        closes = uptrend_closes(400)
        candles = build_candles(closes)
        result = backtest_symbol(config, "TESTUSDT", candles, candles, 1000.0)
        self.assertGreater(result.trades, 0, "le backtest n'a execute aucun trade")
        self.assertEqual(result.wins + result.losses, result.trades)

    def test_backtest_reports_rejections_when_flat(self):
        config = make_config()
        candles = build_candles(flat_closes(300), spread=0.000001)
        result = backtest_symbol(config, "TESTUSDT", candles, candles, 1000.0)
        self.assertEqual(result.trades, 0)
        self.assertTrue(result.rejections)

    def test_backtest_accounts_for_fees(self):
        config = make_config(max_hold_seconds=0, symbol_cooldown_seconds=0)
        candles = build_candles(uptrend_closes(400))
        result = backtest_symbol(config, "TESTUSDT", candles, candles, 1000.0)
        if result.trades:
            self.assertGreater(result.fees, 0.0)


if __name__ == "__main__":
    unittest.main()
