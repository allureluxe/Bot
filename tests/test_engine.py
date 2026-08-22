"""Test d'integration du moteur : scan -> ouverture -> surveillance -> cloture.

Utilise un faux client Binance : aucune connexion reseau, resultat
deterministe. C'est ce cycle complet qui n'existait pas dans l'ancien bot.
"""
import asyncio
import os
import shutil
import tempfile
import unittest

from scalper.config import Config
from scalper.engine import ScalpingEngine
from scalper.state import today_key
from tests.test_strategy import build_candles, uptrend_closes

SYMBOL = "TESTUSDT"


class FakeClient:
    """Reproduit la surface de BinanceClient utilisee par le moteur."""

    def __init__(self, candles, price=None):
        self.candles = candles
        self.price = price if price is not None else candles[-1].close
        self.orders = []
        self.free_base = 0.0

    async def start(self):
        pass

    async def close(self):
        pass

    async def get_exchange_info(self, force=False):
        return {"symbols": [{
            "symbol": SYMBOL, "quoteAsset": "USDT", "baseAsset": "TEST",
            "status": "TRADING", "isSpotTradingAllowed": True,
            "filters": [
                {"filterType": "LOT_SIZE", "minQty": "0.001",
                 "maxQty": "100000", "stepSize": "0.001"},
                {"filterType": "NOTIONAL", "minNotional": "5.0"},
            ],
        }]}

    async def get_24h_tickers(self):
        return [{"symbol": SYMBOL, "quoteVolume": "999999999"}]

    async def get_book_tickers(self):
        return [{
            "symbol": SYMBOL,
            "bidPrice": str(self.price * 0.9999),
            "askPrice": str(self.price * 1.0001),
        }]

    async def get_prices(self, symbols=None):
        return {SYMBOL: self.price}

    async def get_candles(self, symbol, interval, limit=120):
        return self.candles

    async def get_free_balance(self, asset):
        return self.free_base

    async def place_market_order(self, symbol, side, quantity):
        self.orders.append((symbol, side, quantity))
        qty = float(quantity)
        return {
            "orderId": len(self.orders),
            "executedQty": quantity,
            "fills": [{
                "price": str(self.price), "qty": quantity,
                "commission": "0", "commissionAsset": "USDT",
            }],
        }

    def symbol_filters(self, symbol):
        return {
            "base_asset": "TEST", "quote_asset": "USDT",
            "min_qty": 0.001, "max_qty": 100000.0,
            "step_size": 0.001, "min_notional": 5.0, "tick_size": 0.0001,
        }


def make_engine(tmpdir, candles, **overrides):
    config = Config()
    config.dry_run = True
    config.quote_asset = "USDT"
    config.state_file = os.path.join(tmpdir, "state.json")
    config.trade_log_file = os.path.join(tmpdir, "trades.csv")
    config.max_open_positions = 2
    config.symbol_cooldown_seconds = 0
    config.max_hold_seconds = 0
    for key, value in overrides.items():
        setattr(config, key, value)

    engine = ScalpingEngine(config)
    engine.client = FakeClient(candles)
    engine._paper_balance = 1000.0
    return engine


class TestEngineCycle(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.candles = build_candles(
            uptrend_closes(160), [100.0] * 159 + [400.0]
        )

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_scan_opens_a_position(self):
        engine = make_engine(self.dir, self.candles)
        asyncio.run(engine._scan_once())
        self.assertIn(SYMBOL, engine.state.positions,
                      "le scan n'a ouvert aucune position sur une tendance haussiere")

        position = engine.state.positions[SYMBOL]
        self.assertGreater(position.quantity, 0)
        self.assertLess(position.stop_price, position.entry_price)
        self.assertGreater(position.take_profit, position.entry_price)
        self.assertLess(engine._paper_balance, 1000.0)

    def test_position_survives_reload(self):
        engine = make_engine(self.dir, self.candles)
        asyncio.run(engine._scan_once())
        entry = engine.state.positions[SYMBOL].entry_price

        reloaded = make_engine(self.dir, self.candles)
        self.assertIn(SYMBOL, reloaded.state.positions)
        self.assertAlmostEqual(reloaded.state.positions[SYMBOL].entry_price, entry)

    def test_take_profit_closes_position_and_books_gain(self):
        engine = make_engine(self.dir, self.candles)
        asyncio.run(engine._scan_once())
        position = engine.state.positions[SYMBOL]

        engine.client.price = position.take_profit * 1.001
        asyncio.run(engine._monitor_once())

        self.assertNotIn(SYMBOL, engine.state.positions)
        self.assertEqual(engine.state.daily.trades, 1)
        self.assertEqual(engine.state.daily.wins, 1)
        self.assertGreater(engine.state.daily.realized_pnl, 0)
        self.assertEqual(engine.state.win_streak, 1)
        self.assertTrue(os.path.exists(engine.config.trade_log_file))

    def test_stop_loss_closes_position_and_books_loss(self):
        engine = make_engine(self.dir, self.candles)
        asyncio.run(engine._scan_once())
        position = engine.state.positions[SYMBOL]

        engine.client.price = position.stop_price * 0.999
        asyncio.run(engine._monitor_once())

        self.assertNotIn(SYMBOL, engine.state.positions)
        self.assertEqual(engine.state.daily.losses, 1)
        self.assertLess(engine.state.daily.realized_pnl, 0)
        self.assertEqual(engine.state.loss_streak, 1)

    def test_loss_is_bounded_by_configured_risk(self):
        """La perte reelle ne doit pas depasser le risque annonce."""
        engine = make_engine(self.dir, self.candles, base_risk_pct=1.0,
                             max_risk_pct=1.0, equity_decay=0.0)
        asyncio.run(engine._scan_once())
        position = engine.state.positions[SYMBOL]
        risked = position.risk_amount

        engine.client.price = position.stop_price
        asyncio.run(engine._monitor_once())

        loss = abs(engine.state.daily.realized_pnl)
        # Le risque theorique plus les frais aller-retour.
        ceiling = risked + position.notional * engine.config.fee_rate * 2 * 1.05
        self.assertLessEqual(loss, ceiling)

    def test_no_duplicate_position_on_same_symbol(self):
        engine = make_engine(self.dir, self.candles)
        asyncio.run(engine._scan_once())
        asyncio.run(engine._scan_once())
        self.assertEqual(len(engine.state.positions), 1)

    def test_daily_loss_circuit_breaker_halts_trading(self):
        engine = make_engine(self.dir, self.candles, daily_max_loss_pct=1.0)
        # Scenario reel : le bot redemarre en cours de journee apres des pertes.
        engine.state.daily.day = today_key()
        engine.state.daily.starting_equity = 1000.0
        engine.state.daily.realized_pnl = -50.0
        asyncio.run(engine._scan_once())
        self.assertTrue(engine.state.halted_until_next_day)
        self.assertNotIn(SYMBOL, engine.state.positions)

    def test_paused_engine_does_not_trade(self):
        engine = make_engine(self.dir, self.candles)
        engine.state.paused = True
        asyncio.run(engine._scan_once())
        self.assertEqual(len(engine.state.positions), 0)

    def test_trailing_stop_locks_in_profit(self):
        engine = make_engine(self.dir, self.candles,
                             trail_activate_r=0.5, trail_atr_mult=0.5)
        asyncio.run(engine._scan_once())
        position = engine.state.positions[SYMBOL]
        original_stop = position.stop_price

        # Le prix monte fortement sans atteindre le TP.
        engine.client.price = position.entry_price + position.stop_distance * 1.5
        asyncio.run(engine._monitor_once())

        if SYMBOL in engine.state.positions:
            self.assertGreater(engine.state.positions[SYMBOL].stop_price, original_stop)

    def test_live_mode_sends_real_orders(self):
        engine = make_engine(self.dir, self.candles, dry_run=False)
        engine.client.free_base = 10_000.0
        asyncio.run(engine._scan_once())
        self.assertTrue(engine.client.orders, "aucun ordre transmis en mode reel")
        self.assertEqual(engine.client.orders[0][1], "BUY")

        position = engine.state.positions[SYMBOL]
        engine.client.price = position.take_profit * 1.001
        asyncio.run(engine._monitor_once())
        self.assertEqual(engine.client.orders[-1][1], "SELL")

    def test_new_day_resets_counters_and_lifts_halt(self):
        engine = make_engine(self.dir, self.candles)
        engine.state.daily.day = "2020-01-01"
        engine.state.daily.realized_pnl = -500.0
        engine.state.daily.trades = 42
        engine.state.halted_until_next_day = True
        engine.state.halt_reason = "perte journaliere"

        asyncio.run(engine._scan_once())

        self.assertEqual(engine.state.daily.day, today_key())
        # daily.trades compte les trades CLOTURES : rien n'a encore ete ferme.
        self.assertEqual(engine.state.daily.trades, 0)
        self.assertEqual(engine.state.daily.realized_pnl, 0.0)
        self.assertFalse(engine.state.halted_until_next_day)


if __name__ == "__main__":
    unittest.main()
