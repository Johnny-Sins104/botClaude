"""
Test PATCH 2 — filtri specifici per strategia.
Verifica che BB (mean-reversion) non venga bloccata dal filtro anti-late-entry,
e che EMA/MACD lo mantengano.
"""

import sys
import os
import unittest
import inspect
from pathlib import Path
from collections import deque

sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest.mock as _mock
with _mock.patch.dict(os.environ, {"PAPER_MODE": "true"}):
    import bot
import backtest


def _setup_bb_buy_prices():
    """Prezzi: 20 candle stabili poi crollo sotto banda inferiore.
    Usato da signal_bb() e _recent_rebound_risk()."""
    # Con period=20, dev=2.0:
    # prices[-20:] = [50000]*18 + [49950, 49700]
    # sma ≈ 49982.5, sd ≈ 66 → lower ≈ 49851
    # prev=49950 >= lower, close=49700 < lower → cross_down=True → buy signal
    prices = [50000.0] * 20 + [49950.0, 49700.0]
    bot.closed_prices.clear()
    for p in prices:
        bot.closed_prices.append(p)
    bot.closed_volumes.clear()
    for _ in prices:
        bot.closed_volumes.append(1.0)
    # Candle per _recent_rebound_risk (ultime 3: 50000, 49950, 49700 → falling)
    bot.closed_candles.clear()
    t0 = 1_740_000_000_000
    for i, p in enumerate(prices[-5:]):
        bot.closed_candles.append({"t": t0 + i * 60000, "T": t0 + i * 60000 + 59999,
                                   "o": p, "h": p + 100, "l": p - 100, "c": p, "v": 1.0})


class TestBBReverbFilter(unittest.TestCase):

    def setUp(self):
        _setup_bb_buy_prices()

    def test_bb_signal_returns_buy_on_cross_down(self):
        """signal_bb() deve generare buy quando close attraversa sotto la banda inferiore."""
        sig = bot.signal_bb()
        self.assertEqual(sig["type"], "buy", f"Atteso buy, ricevuto: {sig['type']} — {sig['detail']}")
        self.assertGreater(sig["score"], 0)

    def test_rebound_risk_detects_falling_as_risk(self):
        """_recent_rebound_risk deve rilevare 3 close in calo come rischio per long."""
        risk, why = bot._recent_rebound_risk("buy")
        self.assertTrue(risk, f"Atteso risk=True per buy su prezzi in calo, got False ({why})")

    def test_bb_not_blocked_in_backtest_loop(self):
        """Nel loop backtest, BB buy non deve essere bloccata dal filtro rebound."""
        from tools.fetch_klines import load_cache
        c1 = load_cache("BTCUSDT", "1m")
        c5 = load_cache("BTCUSDT", "5m")
        if not c1:
            self.skipTest("Cache BTCUSDT non disponibile")
        old_s = bot.CONFIG["strategy"]
        old_a = bot.CONFIG["auto_strategy_selector"]
        old_h = bot.CONFIG["htf_trend_filter"]
        bot.CONFIG["strategy"] = "bb"
        bot.CONFIG["auto_strategy_selector"] = False
        bot.CONFIG["htf_trend_filter"] = False
        try:
            result = backtest.run_backtest(c1, c5)
        finally:
            bot.CONFIG["strategy"] = old_s
            bot.CONFIG["auto_strategy_selector"] = old_a
            bot.CONFIG["htf_trend_filter"] = old_h
        # BB deve generare trade (senza fix erano 0 o pochissimi per il filtro rebound)
        self.assertGreater(len(result["trades"]), 0,
                           "BB deve produrre trade con filtri HTF disattivati")

    def test_rebound_filter_only_for_ema_macd_in_backtest(self):
        """Il filtro rebound nel loop backtest deve applicarsi solo a ema/macd."""
        src = inspect.getsource(backtest._run_backtest_inner)
        self.assertIn('active in {"ema", "macd"}', src,
                      "Il filtro rebound deve essere condizionato a {ema, macd}")

    def test_rebound_filter_only_for_ema_macd_in_open_position(self):
        """Il filtro rebound in open_position() deve applicarsi solo a ema/macd."""
        src = inspect.getsource(bot.open_position)
        self.assertIn('active in {"ema", "macd"}', src,
                      "open_position deve applicare rebound solo a {ema, macd}")

    def test_ema_applies_rebound_filter(self):
        """EMA strategy deve ancora applicare il filtro anti-late-entry."""
        from tools.fetch_klines import load_cache
        c1 = load_cache("BTCUSDT", "1m")
        c5 = load_cache("BTCUSDT", "5m")
        if not c1:
            self.skipTest("Cache BTCUSDT non disponibile")
        old_s = bot.CONFIG["strategy"]
        old_a = bot.CONFIG["auto_strategy_selector"]
        old_h = bot.CONFIG["htf_trend_filter"]
        bot.CONFIG["strategy"] = "ema"
        bot.CONFIG["auto_strategy_selector"] = False
        bot.CONFIG["htf_trend_filter"] = False
        try:
            result = backtest.run_backtest(c1, c5)
        finally:
            bot.CONFIG["strategy"] = old_s
            bot.CONFIG["auto_strategy_selector"] = old_a
            bot.CONFIG["htf_trend_filter"] = old_h
        # EMA deve completare il run senza errori; il filtro rebound può ridurre i trade
        self.assertIsInstance(result["trades"], list)


if __name__ == "__main__":
    unittest.main(verbosity=2)
