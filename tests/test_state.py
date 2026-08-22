import os
import shutil
import tempfile
import time
import unittest

from scalper.state import BotState, Position, StateStore, today_key


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = StateStore(
            os.path.join(self.dir, "state.json"),
            os.path.join(self.dir, "trades.csv"),
        )

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def make_position(self, symbol="BTCUSDT"):
        return Position(
            symbol=symbol, side="LONG", quantity=0.5, entry_price=100.0,
            stop_price=99.0, take_profit=102.0, initial_stop=99.0, atr=0.4,
            opened_at=time.time(), risk_amount=0.5, notional=50.0,
            order_id="123", dry_run=True,
        )

    def test_missing_file_gives_empty_state(self):
        self.assertEqual(len(self.store.load().positions), 0)

    def test_positions_survive_restart(self):
        """Regression : l'ancien bot perdait ses positions a chaque redemarrage."""
        state = BotState()
        state.positions["BTCUSDT"] = self.make_position()
        state.win_streak = 3
        state.daily.day = today_key()
        state.daily.realized_pnl = 12.5
        state.daily.trades = 4
        self.store.save(state)

        reloaded = self.store.load()
        self.assertIn("BTCUSDT", reloaded.positions)
        position = reloaded.positions["BTCUSDT"]
        self.assertEqual(position.quantity, 0.5)
        self.assertEqual(position.entry_price, 100.0)
        self.assertEqual(position.stop_price, 99.0)
        self.assertEqual(reloaded.win_streak, 3)
        self.assertEqual(reloaded.daily.trades, 4)
        self.assertAlmostEqual(reloaded.daily.realized_pnl, 12.5)

    def test_corrupted_file_does_not_crash(self):
        with open(self.store.state_file, "w", encoding="utf-8") as handle:
            handle.write("{ ceci n'est pas du json")
        self.assertEqual(len(self.store.load().positions), 0)

    def test_trade_log_writes_header_once(self):
        row = {
            "closed_at": "2026-01-01T00:00:00Z", "symbol": "BTCUSDT", "side": "LONG",
            "quantity": "0.5", "entry_price": "100", "exit_price": "102",
            "stop_price": "99", "take_profit": "102", "pnl": "1.0", "pnl_pct": "2.0",
            "r_multiple": "2.0", "fees": "0.1", "reason": "TAKE_PROFIT",
            "hold_seconds": "60", "risk_pct": "1.0", "dry_run": True,
        }
        self.store.log_trade(row)
        self.store.log_trade(row)
        with open(self.store.trade_log_file, encoding="utf-8") as handle:
            lines = [line for line in handle.read().splitlines() if line.strip()]
        self.assertEqual(len(lines), 3)  # entete + 2 lignes
        self.assertTrue(lines[0].startswith("closed_at"))

    def test_save_is_atomic(self):
        state = BotState()
        state.positions["ETHUSDT"] = self.make_position("ETHUSDT")
        self.store.save(state)
        self.assertFalse(os.path.exists(self.store.state_file + ".tmp"))


class TestPositionMath(unittest.TestCase):
    def test_pct_and_age(self):
        position = Position(
            symbol="X", side="LONG", quantity=1, entry_price=100.0,
            stop_price=99.0, take_profit=102.0, initial_stop=99.0, atr=1,
            opened_at=time.time() - 30, risk_amount=1, notional=100,
        )
        self.assertAlmostEqual(position.unrealized_pct(101.0), 1.0, places=6)
        self.assertGreaterEqual(position.age_seconds(), 29)

    def test_highest_price_defaults_to_entry(self):
        position = Position(
            symbol="X", side="LONG", quantity=1, entry_price=100.0,
            stop_price=99.0, take_profit=102.0, initial_stop=99.0, atr=1,
            opened_at=time.time(), risk_amount=1, notional=100,
        )
        self.assertEqual(position.highest_price, 100.0)


if __name__ == "__main__":
    unittest.main()
