import time
import unittest

from scalper.config import Config
from scalper.risk import RiskManager, floor_to_step, format_quantity
from scalper.state import BotState, Position

FILTERS = {
    "base_asset": "BTC",
    "min_qty": 0.00001,
    "max_qty": 9000.0,
    "step_size": 0.00001,
    "min_notional": 5.0,
}


def make_manager(**overrides) -> RiskManager:
    config = Config()
    config.base_risk_pct = 1.0
    config.min_risk_pct = 0.2
    config.max_risk_pct = 2.0
    config.equity_decay = 0.0
    config.max_position_pct = 50.0
    config.max_total_exposure_pct = 100.0
    config.daily_soft_loss_pct = 3.0
    for key, value in overrides.items():
        setattr(config, key, value)
    state = BotState()
    state.daily.starting_equity = 1000.0
    return RiskManager(config, state)


class TestStepRounding(unittest.TestCase):
    def test_floor_handles_float_error(self):
        # 0.3 / 0.1 vaut 2.9999... en flottant : sans arrondi defensif on
        # obtiendrait 0.2 au lieu de 0.3.
        self.assertAlmostEqual(floor_to_step(0.3, 0.1), 0.3, places=10)

    def test_floor_truncates(self):
        self.assertAlmostEqual(floor_to_step(1.2345678, 0.001), 1.234, places=10)

    def test_format_matches_step_precision(self):
        self.assertEqual(format_quantity(1.23456789, 0.001), "1.235")
        self.assertEqual(format_quantity(5.0, 1.0), "5")


class TestPositionSizing(unittest.TestCase):
    def test_risk_defines_quantity(self):
        manager = make_manager()
        # 1% de 1000 = 10 de risque ; stop a 2% de 100 = 2 par unite -> 5 unites
        result = manager.size_position(1000.0, 1000.0, 100.0, 98.0, FILTERS)
        self.assertTrue(result.ok, result.reason)
        self.assertAlmostEqual(result.quantity, 5.0, places=4)
        self.assertAlmostEqual(result.risk_amount, 10.0, places=2)

    def test_lot_grows_with_capital(self):
        """Progressif : a risque % egal, le lot suit le capital."""
        manager = make_manager()
        small = manager.size_position(500.0, 500.0, 100.0, 98.0, FILTERS)
        large = manager.size_position(2000.0, 2000.0, 100.0, 98.0, FILTERS)
        self.assertTrue(small.ok and large.ok)
        self.assertAlmostEqual(large.quantity / small.quantity, 4.0, places=3)

    def test_wider_stop_gives_smaller_lot(self):
        """A risque monetaire egal, un stop plus large impose un lot plus petit."""
        manager = make_manager(max_position_pct=100.0)
        tight = manager.size_position(1000.0, 1000.0, 100.0, 98.0, FILTERS)
        wide = manager.size_position(1000.0, 1000.0, 100.0, 95.0, FILTERS)
        self.assertTrue(tight.ok and wide.ok)
        self.assertAlmostEqual(tight.risk_amount, wide.risk_amount, places=2)
        self.assertGreater(tight.quantity, wide.quantity)

    def test_position_cap_can_reduce_risk_below_target(self):
        """Un stop tres serre fait mordre le plafond de taille avant le risque cible.

        Le risque reel devient alors INFERIEUR a la cible : c'est voulu, le
        plafond de taille prime toujours sur l'objectif de risque.
        """
        manager = make_manager(max_position_pct=50.0)
        result = manager.size_position(1000.0, 1000.0, 100.0, 99.0, FILTERS)
        self.assertTrue(result.ok, result.reason)
        self.assertLessEqual(result.notional, 500.0 * 1.0001)
        self.assertLess(result.risk_amount, 10.0)

    def test_rejects_stop_above_entry(self):
        manager = make_manager()
        self.assertFalse(manager.size_position(1000.0, 1000.0, 100.0, 101.0, FILTERS).ok)

    def test_respects_max_position_cap(self):
        manager = make_manager(max_position_pct=10.0)
        result = manager.size_position(1000.0, 1000.0, 100.0, 99.9, FILTERS)
        self.assertTrue(result.ok, result.reason)
        self.assertLessEqual(result.notional, 100.0 * 1.0001)

    def test_rejects_when_min_notional_exceeds_budget(self):
        manager = make_manager(max_position_pct=50.0)
        filters = dict(FILTERS, min_notional=500.0)
        result = manager.size_position(20.0, 20.0, 100.0, 98.0, filters)
        self.assertFalse(result.ok)
        self.assertIn("solde disponible", result.reason)

    def test_never_exceeds_available_balance(self):
        manager = make_manager()
        result = manager.size_position(1000.0, 30.0, 100.0, 99.5, FILTERS)
        if result.ok:
            self.assertLessEqual(result.notional, 30.0)


class TestProgressiveRegressive(unittest.TestCase):
    def test_win_streak_increases_risk(self):
        manager = make_manager()
        flat = manager.current_risk_pct(1000.0)
        manager.state.win_streak = 3
        self.assertGreater(manager.current_risk_pct(1000.0), flat)

    def test_loss_streak_decreases_risk(self):
        manager = make_manager()
        flat = manager.current_risk_pct(1000.0)
        manager.state.loss_streak = 3
        self.assertLess(manager.current_risk_pct(1000.0), flat)

    def test_never_martingale(self):
        """Le risque doit strictement decroitre a chaque perte consecutive."""
        manager = make_manager()
        previous = manager.current_risk_pct(1000.0)
        for streak in range(1, 5):
            manager.state.loss_streak = streak
            current = manager.current_risk_pct(1000.0)
            self.assertLessEqual(current, previous)
            previous = current

    def test_streak_multipliers_are_bounded(self):
        manager = make_manager()
        manager.state.win_streak = 50
        self.assertLessEqual(manager.streak_multiplier(), manager.config.max_streak_mult)
        manager.state.win_streak = 0
        manager.state.loss_streak = 50
        self.assertGreaterEqual(manager.streak_multiplier(), manager.config.min_streak_mult)

    def test_risk_always_within_bounds(self):
        manager = make_manager()
        for equity in (10.0, 100.0, 1000.0, 100_000.0):
            for wins, losses in ((0, 0), (10, 0), (0, 10)):
                manager.state.win_streak, manager.state.loss_streak = wins, losses
                risk = manager.current_risk_pct(equity)
                self.assertGreaterEqual(risk, manager.config.min_risk_pct)
                self.assertLessEqual(risk, manager.config.max_risk_pct)

    def test_equity_decay_reduces_risk_for_large_accounts(self):
        manager = make_manager(equity_decay=0.15, equity_ref=500.0)
        small = manager.current_risk_pct(200.0)
        large = manager.current_risk_pct(20_000.0)
        self.assertGreater(small, large)

    def test_soft_drawdown_halves_risk(self):
        manager = make_manager()
        normal = manager.current_risk_pct(1000.0)
        manager.state.daily.realized_pnl = -40.0  # -4% > seuil doux de 3%
        self.assertLess(manager.current_risk_pct(1000.0), normal)


class TestCircuitBreakers(unittest.TestCase):
    def test_daily_loss_limit_triggers(self):
        manager = make_manager(daily_max_loss_pct=5.0)
        manager.state.daily.realized_pnl = -60.0
        self.assertIsNotNone(manager.check_daily_circuit_breakers(940.0))

    def test_no_halt_within_limits(self):
        manager = make_manager(daily_max_loss_pct=5.0)
        manager.state.daily.realized_pnl = -20.0
        self.assertIsNone(manager.check_daily_circuit_breakers(980.0))

    def test_profit_target_halts(self):
        manager = make_manager(daily_profit_target_pct=4.0)
        manager.state.daily.realized_pnl = 50.0
        self.assertIsNotNone(manager.check_daily_circuit_breakers(1050.0))

    def test_max_positions_blocks_entry(self):
        manager = make_manager(max_open_positions=1)
        manager.state.positions["ETHUSDT"] = Position(
            symbol="ETHUSDT", side="LONG", quantity=1, entry_price=100,
            stop_price=99, take_profit=102, initial_stop=99, atr=1,
            opened_at=time.time(), risk_amount=1, notional=100,
        )
        allowed, reason = manager.can_open("BTCUSDT", 1000.0)
        self.assertFalse(allowed)
        self.assertIn("maximum", reason)

    def test_duplicate_symbol_blocked(self):
        manager = make_manager()
        manager.state.positions["BTCUSDT"] = Position(
            symbol="BTCUSDT", side="LONG", quantity=1, entry_price=100,
            stop_price=99, take_profit=102, initial_stop=99, atr=1,
            opened_at=time.time(), risk_amount=1, notional=100,
        )
        allowed, _ = manager.can_open("BTCUSDT", 1000.0)
        self.assertFalse(allowed)

    def test_cooldown_after_loss(self):
        manager = make_manager(symbol_cooldown_seconds=120)
        manager.register_result("BTCUSDT", -5.0, 0.1)
        allowed, reason = manager.can_open("BTCUSDT", 1000.0)
        self.assertFalse(allowed)
        self.assertIn("cooldown", reason)

    def test_no_cooldown_after_win(self):
        manager = make_manager(symbol_cooldown_seconds=120)
        manager.register_result("BTCUSDT", 5.0, 0.1)
        allowed, _ = manager.can_open("BTCUSDT", 1000.0)
        self.assertTrue(allowed)

    def test_consecutive_loss_guard(self):
        manager = make_manager(max_consecutive_losses=3, symbol_cooldown_seconds=0)
        for _ in range(3):
            manager.register_result("BTCUSDT", -5.0, 0.1)
        allowed, reason = manager.can_open("ETHUSDT", 1000.0)
        self.assertFalse(allowed)
        self.assertIn("consecutives", reason)

    def test_unlimited_trades_when_zero(self):
        manager = make_manager(max_trades_per_day=0)
        manager.state.daily.trades = 5000
        allowed, _ = manager.can_open("BTCUSDT", 1000.0)
        self.assertTrue(allowed)

    def test_daily_quota_enforced_when_set(self):
        manager = make_manager(max_trades_per_day=10)
        manager.state.daily.trades = 10
        allowed, reason = manager.can_open("BTCUSDT", 1000.0)
        self.assertFalse(allowed)
        self.assertIn("quota", reason)


class TestResultTracking(unittest.TestCase):
    def test_streaks_update(self):
        manager = make_manager(symbol_cooldown_seconds=0)
        manager.register_result("A", 5.0, 0.1)
        manager.register_result("B", 5.0, 0.1)
        self.assertEqual(manager.state.win_streak, 2)
        self.assertEqual(manager.state.loss_streak, 0)
        manager.register_result("C", -5.0, 0.1)
        self.assertEqual(manager.state.win_streak, 0)
        self.assertEqual(manager.state.loss_streak, 1)

    def test_daily_stats_accumulate(self):
        manager = make_manager(symbol_cooldown_seconds=0)
        manager.register_result("A", 10.0, 0.2)
        manager.register_result("B", -4.0, 0.2)
        self.assertEqual(manager.state.daily.trades, 2)
        self.assertEqual(manager.state.daily.wins, 1)
        self.assertEqual(manager.state.daily.losses, 1)
        self.assertAlmostEqual(manager.state.daily.realized_pnl, 6.0, places=6)
        self.assertAlmostEqual(manager.state.daily.win_rate(), 50.0, places=6)


if __name__ == "__main__":
    unittest.main()
