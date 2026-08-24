import math
import unittest

from scalper.indicators import Candle, atr, ema, rate_of_change_pct, rsi, volume_ratio


def make_candles(closes, volumes=None):
    volumes = volumes or [100.0] * len(closes)
    candles = []
    for i, (close, volume) in enumerate(zip(closes, volumes)):
        candles.append(Candle(
            open_time=i * 60_000,
            open=close,
            high=close * 1.002,
            low=close * 0.998,
            close=close,
            volume=volume,
            close_time=i * 60_000 + 59_999,
        ))
    return candles


class TestRSI(unittest.TestCase):
    def test_pure_uptrend_is_100(self):
        self.assertEqual(rsi([float(i) for i in range(1, 40)], 14), 100.0)

    def test_pure_downtrend_is_zero(self):
        value = rsi([float(i) for i in range(40, 1, -1)], 14)
        self.assertLess(value, 1.0)

    def test_insufficient_data_returns_none(self):
        self.assertIsNone(rsi([1.0, 2.0, 3.0], 14))

    def test_rsi_reacts_to_latest_candles(self):
        """Regression : l'ancien RSI n'utilisait que les 15 plus vieilles bougies.

        Une chute brutale en fin de serie doit faire s'effondrer le RSI. Avec le
        bug d'origine, la valeur restait identique.
        """
        base = [100.0 + i * 0.5 for i in range(60)]
        calm = rsi(base, 14)

        crashed = base[:-10] + [base[-11] * (1 - 0.02 * i) for i in range(1, 11)]
        after_crash = rsi(crashed, 14)

        self.assertIsNotNone(calm)
        self.assertIsNotNone(after_crash)
        self.assertLess(after_crash, calm - 30,
                        "le RSI doit refleter les dernieres bougies, pas les premieres")

    def test_known_wilder_value(self):
        # Serie de reference Wilder (14 periodes), RSI attendu ~70.5
        prices = [
            44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
            45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28,
        ]
        value = rsi(prices, 14)
        self.assertIsNotNone(value)
        self.assertTrue(69.0 < value < 72.0, f"RSI attendu ~70.5, obtenu {value}")


class TestEMA(unittest.TestCase):
    def test_constant_series(self):
        self.assertAlmostEqual(ema([10.0] * 30, 9), 10.0, places=6)

    def test_fast_ema_reacts_faster(self):
        prices = [100.0] * 30 + [110.0] * 10
        fast = ema(prices, 5)
        slow = ema(prices, 20)
        self.assertGreater(fast, slow)

    def test_insufficient_data(self):
        self.assertIsNone(ema([1.0, 2.0], 9))


class TestATR(unittest.TestCase):
    def test_atr_positive_on_moving_market(self):
        candles = make_candles([100 + i * 0.3 for i in range(40)])
        value = atr(candles, 14)
        self.assertIsNotNone(value)
        self.assertGreater(value, 0)

    def test_atr_none_when_short(self):
        self.assertIsNone(atr(make_candles([100.0, 101.0]), 14))


class TestHelpers(unittest.TestCase):
    def test_volume_ratio_detects_spike(self):
        candles = make_candles([100.0] * 25, volumes=[100.0] * 24 + [500.0])
        self.assertAlmostEqual(volume_ratio(candles, 20), 5.0, places=4)

    def test_rate_of_change(self):
        value = rate_of_change_pct([100.0, 101.0, 102.0, 103.0], 3)
        self.assertAlmostEqual(value, 3.0, places=6)


if __name__ == "__main__":
    unittest.main()
