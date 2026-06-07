"""
Test PATCH 1 — motore backtest: daily_loss_limit, config restore, isolamento stato.
"""

import sys
import os
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest.mock as _mock
with _mock.patch.dict(os.environ, {"PAPER_MODE": "true"}):
    import bot
import backtest

STEP_MS = 60_000


def _candle(i: int, price: float) -> dict:
    t = 1_740_000_000_000 + i * STEP_MS
    return {"t": t, "T": t + STEP_MS - 1, "o": price, "h": price * 1.001,
            "l": price * 0.999, "c": price, "v": 1.0}


def _candles_1m(n: int = 200, price: float = 50000.0) -> list:
    return [_candle(i, price) for i in range(n)]


def _candles_5m(n: int = 80, price: float = 50000.0) -> list:
    step = 300_000
    return [{"t": 1_740_000_000_000 + i * step, "T": 1_740_000_000_000 + i * step + step - 1,
             "o": price, "h": price * 1.001, "l": price * 0.999, "c": price, "v": 5.0}
            for i in range(n)]


class TestDailyLossLimitDisabled(unittest.TestCase):

    def test_trading_halted_false_when_config_disabled(self):
        """trading_halted deve restare False quando daily_loss_limit_enabled=False."""
        orig = bot.CONFIG["daily_loss_limit_enabled"]
        try:
            bot.CONFIG["daily_loss_limit_enabled"] = False
            backtest._reset_bot_state(1000.0)
            # Simula perdita superiore al limite giornaliero (-3.1%)
            bot.state["capital"] = 969.0
            bot._refresh_risk_guard()
            self.assertFalse(bot.state["trading_halted"])
            self.assertFalse(bot.state["risk_guard"]["daily_loss_limit_hit"])
        finally:
            bot.CONFIG["daily_loss_limit_enabled"] = orig

    def test_trading_halted_true_when_config_enabled(self):
        """trading_halted deve diventare True quando daily_loss_limit_enabled=True."""
        orig = bot.CONFIG["daily_loss_limit_enabled"]
        try:
            bot.CONFIG["daily_loss_limit_enabled"] = True
            backtest._reset_bot_state(1000.0)
            bot.state["capital"] = 969.0
            bot._refresh_risk_guard()
            self.assertTrue(bot.state["trading_halted"])
            self.assertTrue(bot.state["risk_guard"]["daily_loss_limit_hit"])
        finally:
            bot.CONFIG["daily_loss_limit_enabled"] = orig

    def test_config_restored_after_successful_run(self):
        """CONFIG[daily_loss_limit_enabled] ripristinato dopo run corretto."""
        orig = bot.CONFIG["daily_loss_limit_enabled"]
        c1 = _candles_1m()
        c5 = _candles_5m()
        backtest.run_backtest(c1, c5, disable_daily_loss=True)
        self.assertEqual(bot.CONFIG["daily_loss_limit_enabled"], orig)

    def test_config_restored_when_orig_is_true(self):
        """CONFIG ripristinato a True anche se era True prima del run."""
        bot.CONFIG["daily_loss_limit_enabled"] = True
        c1 = _candles_1m()
        c5 = _candles_5m()
        backtest.run_backtest(c1, c5, disable_daily_loss=True)
        self.assertTrue(bot.CONFIG["daily_loss_limit_enabled"])

    def test_config_not_changed_when_disable_false(self):
        """Con disable_daily_loss=False, CONFIG non viene modificato."""
        orig = bot.CONFIG["daily_loss_limit_enabled"]
        # Imposta a False per verificare che non venga cambiato a False da noi
        bot.CONFIG["daily_loss_limit_enabled"] = True
        c1 = _candles_1m()
        c5 = _candles_5m()
        backtest.run_backtest(c1, c5, disable_daily_loss=False)
        self.assertTrue(bot.CONFIG["daily_loss_limit_enabled"])
        bot.CONFIG["daily_loss_limit_enabled"] = orig

    def test_state_isolation_between_runs(self):
        """Due run consecutivi non condividono stato: capital iniziale indipendente."""
        c1 = _candles_1m()
        c5 = _candles_5m()
        r1 = backtest.run_backtest(c1, c5, capital=1000.0)
        r2 = backtest.run_backtest(c1, c5, capital=2000.0)
        self.assertEqual(r1["equity"][0], 1000.0)
        self.assertEqual(r2["equity"][0], 2000.0)

    def test_no_halt_in_real_data_backtest(self):
        """Backtest su dati reali non si ferma per daily_loss_limit."""
        from tools.fetch_klines import load_cache
        c1 = load_cache("BTCUSDT", "1m")
        c5 = load_cache("BTCUSDT", "5m")
        if not c1:
            self.skipTest("Cache BTCUSDT non disponibile")
        orig = bot.CONFIG["daily_loss_limit_enabled"]
        try:
            bot.CONFIG["daily_loss_limit_enabled"] = True
            r_limited = backtest.run_backtest(c1, c5, disable_daily_loss=False)
            r_free = backtest.run_backtest(c1, c5, disable_daily_loss=True)
        finally:
            bot.CONFIG["daily_loss_limit_enabled"] = orig
        # Con il limite attivo, i trade possono essere meno (bot si ferma a -3%)
        # Con il limite disattivato, il bot non si ferma mai per quel motivo
        # Verifica che entrambi i run completino senza eccezioni
        self.assertIsInstance(r_limited["trades"], list)
        self.assertIsInstance(r_free["trades"], list)
        # Il run senza limite deve avere almeno tanti trade quanti con limite
        self.assertGreaterEqual(len(r_free["trades"]), len(r_limited["trades"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
