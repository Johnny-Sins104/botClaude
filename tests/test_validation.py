"""
Test per validate_backtest.py.
Spostati da validate_backtest.py in tests/ — PATCH 11.
Copertura: riproducibilita' config, no-cost, warm-up OOS, criteri is_promising,
concentrazione temporale, modalita' long-only.
"""

import os
import sys
import unittest
from copy import deepcopy
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest.mock as _mock
with _mock.patch.dict(os.environ, {"PAPER_MODE": "true"}):
    import bot
from backtest import compute_metrics
from validate_backtest import (
    run_with_validation, run_without_costs, split_candles, oos_with_warmup,
    OOS_WARMUP_1M, OOS_WARMUP_5M, VALIDATION_CONFIG,
    weekly_breakdown, temporal_concentration_check, is_promising, monthly_breakdown,
)
from tools.fetch_klines import load_cache


class TestValidationReproducible(unittest.TestCase):
    """PATCH 5 — verifica che env diverse non cambino i risultati."""

    @classmethod
    def setUpClass(cls):
        cls.c1 = load_cache("BTCUSDT", "1m")
        cls.c5 = load_cache("BTCUSDT", "5m")
        if not cls.c1:
            raise unittest.SkipTest("Cache BTCUSDT non disponibile")
        cls.c1s = cls.c1[:3000]
        cls.c5s = cls.c5[:600]

    def test_env_values_dont_change_results(self):
        """Config .env diverse devono produrre risultati identici a VALIDATION_CONFIG."""
        orig = deepcopy(bot.CONFIG)
        try:
            r1 = run_with_validation(self.c1s, self.c5s, 1000.0)
            bot.CONFIG["fee_pct"] = 0.99
            bot.CONFIG["ema_slow"] = 200
            bot.CONFIG["tp_ratio"] = 10.0
            bot.CONFIG["allow_short"] = True
            r2 = run_with_validation(self.c1s, self.c5s, 1000.0)
            self.assertAlmostEqual(r1["final_capital"], r2["final_capital"], places=6)
            self.assertEqual(len(r1["trades"]), len(r2["trades"]))
        finally:
            bot.CONFIG.clear()
            bot.CONFIG.update(orig)

    def test_config_fully_restored_after_run(self):
        """bot.CONFIG completamente ripristinato dopo run_with_validation."""
        orig = deepcopy(bot.CONFIG)
        run_with_validation(self.c1s, self.c5s, 1000.0)
        self.assertEqual(bot.CONFIG, orig)

    def test_config_fully_restored_after_overrides(self):
        """Anche con overrides specifici, CONFIG e' ripristinato."""
        orig = deepcopy(bot.CONFIG)
        run_with_validation(self.c1s, self.c5s, 1000.0,
                            overrides={"strategy": "bb", "htf_trend_filter": False})
        self.assertEqual(bot.CONFIG, orig)

    def test_validation_config_keys_are_complete(self):
        """VALIDATION_CONFIG deve coprire almeno le chiavi richieste dall'audit."""
        required = {
            "strategy", "auto_strategy_selector", "auto_strategy_fallback",
            "allow_short", "risk_pct", "tp_ratio", "sl_atr_mult",
            "max_notional_pct", "min_entry_score",
            "ema_fast", "ema_slow", "rsi_period", "bb_period", "bb_dev",
            "macd_fast", "macd_slow", "macd_sig",
            "ichi_t", "ichi_k", "ichi_s",
            "whipsaw_filter", "mtf_filter",
            "htf_trend_filter", "htf_trend_fast", "htf_trend_slow", "htf_trend_min_gap_pct",
            "trailing_stop", "post_sl_cooldown_candles",
            "fee_pct", "slippage_pct",
        }
        missing = required - set(VALIDATION_CONFIG.keys())
        self.assertEqual(missing, set(), f"Chiavi mancanti in VALIDATION_CONFIG: {missing}")


class TestNoCost(unittest.TestCase):
    """PATCH 6 — verifica che run_without_costs sia davvero privo di costi."""

    @classmethod
    def setUpClass(cls):
        cls.c1 = load_cache("BTCUSDT", "1m")
        cls.c5 = load_cache("BTCUSDT", "5m")
        if not cls.c1:
            raise unittest.SkipTest("Cache BTCUSDT non disponibile")
        cls.c1s = cls.c1[:3000]
        cls.c5s = cls.c5[:600]

    def test_no_cost_fees_are_zero(self):
        r = run_without_costs(self.c1s, self.c5s, 1000.0)
        for t in r["trades"]:
            self.assertEqual(t.get("fee_total", 0), 0.0,
                             f"fee_total non zero: {t.get('fee_total')}")

    def test_no_cost_total_fees_zero_in_metrics(self):
        r = run_without_costs(self.c1s, self.c5s, 1000.0)
        m = compute_metrics(r, 1000.0)
        self.assertEqual(m["total_fees"], 0.0)

    def test_no_cost_pnl_gross_equals_net(self):
        r = run_without_costs(self.c1s, self.c5s, 1000.0)
        for t in r["trades"]:
            self.assertAlmostEqual(t["pnl_gross"], t["pnl_net"], places=6,
                                   msg=f"pnl_gross != pnl_net: {t}")

    def test_no_cost_has_more_or_equal_pnl(self):
        r_cost = run_with_validation(self.c1s, self.c5s, 1000.0)
        r_nc   = run_without_costs(self.c1s, self.c5s, 1000.0)
        self.assertGreaterEqual(r_nc["final_capital"], r_cost["final_capital"])

    def test_no_cost_returns_valid_trade_list(self):
        r_cost = run_with_validation(self.c1s, self.c5s, 1000.0)
        r_nc   = run_without_costs(self.c1s, self.c5s, 1000.0)
        self.assertIsInstance(r_cost["trades"], list)
        self.assertIsInstance(r_nc["trades"], list)


class TestOOSWarmup(unittest.TestCase):
    """PATCH 7 — verifica warm-up OOS e assenza di lookahead."""

    @classmethod
    def setUpClass(cls):
        cls.c1 = load_cache("BTCUSDT", "1m")
        cls.c5 = load_cache("BTCUSDT", "5m")
        if not cls.c1:
            raise unittest.SkipTest("Cache BTCUSDT non disponibile")
        cls.train_1m, cls.train_5m, cls.oos_1m, cls.oos_5m = split_candles(
            cls.c1, cls.c5, 60
        )
        cls.combined_1m, cls.combined_5m, cls.wup = oos_with_warmup(
            cls.train_1m, cls.train_5m, cls.oos_1m, cls.oos_5m
        )

    def test_warmup_count_correct(self):
        self.assertLessEqual(self.wup, OOS_WARMUP_1M)
        self.assertLessEqual(self.wup, len(self.train_1m))
        self.assertGreater(self.wup, 0)

    def test_no_trades_during_warmup(self):
        if not self.oos_1m:
            self.skipTest("Dati OOS non disponibili")
        oos_start_t = self.oos_1m[0]["t"]
        r = run_with_validation(self.combined_1m, self.combined_5m, 1000.0,
                                warmup=self.wup)
        for trade in r["trades"]:
            trade_t = datetime.fromisoformat(trade["time"]).timestamp() * 1000
            self.assertGreaterEqual(
                trade_t, oos_start_t - 60_000,
                f"Trade durante warmup: {trade['time']} < oos_start"
            )

    def test_5m_buffer_filled_after_warmup(self):
        from backtest import _reset_bot_state, _sync_5m_buffer
        _reset_bot_state(1000.0)
        idx = [0]
        htf_slow = VALIDATION_CONFIG["htf_trend_slow"]
        for candle in self.combined_1m[:self.wup]:
            _sync_5m_buffer(candle, self.combined_5m, idx)
            bot.closed_candles.append(candle)
            bot.closed_prices.append(candle["c"])
        self.assertGreaterEqual(
            len(bot.closed_prices_5m), htf_slow,
            f"Buffer 5m insufficiente dopo warmup: {len(bot.closed_prices_5m)} < {htf_slow}"
        )

    def test_no_lookahead_in_5m_sync(self):
        from backtest import _reset_bot_state, _sync_5m_buffer
        _reset_bot_state(1000.0)
        idx = [0]
        for candle in self.combined_1m[:200]:
            t_before = candle["T"]
            _sync_5m_buffer(candle, self.combined_5m, idx)
            for c5m in list(bot.closed_candles_5m):
                self.assertLessEqual(
                    c5m["T"], t_before + 1,
                    f"Lookahead: 5m T={c5m['T']} > 1m T={t_before}"
                )
            bot.closed_candles.append(candle)
            bot.closed_prices.append(candle["c"])

    def test_oos_deterministic(self):
        r1 = run_with_validation(self.combined_1m, self.combined_5m, 1000.0,
                                 warmup=self.wup)
        r2 = run_with_validation(self.combined_1m, self.combined_5m, 1000.0,
                                 warmup=self.wup)
        self.assertAlmostEqual(r1["final_capital"], r2["final_capital"], places=6)
        self.assertEqual(len(r1["trades"]), len(r2["trades"]))


class TestCriteria(unittest.TestCase):
    """PATCH 8/11/12 — criteri is_promising, concentrazione temporale, long-only."""

    def _make_metrics(self, n, pf, pnl, long_pnl=0.0, short_pnl=0.0):
        return {
            "trades": n, "wins": 0, "losses": 0,
            "win_rate": 50.0, "profit_factor": pf, "payoff_ratio": 1.0,
            "pnl_net": pnl, "pnl_pct": pnl / 10,
            "max_dd": 5.0, "total_fees": 0.0,
            "break_even_wr": 50.0, "tp_exits": 0, "sl_exits": 0,
            "avg_win": 0.0, "avg_loss": 0.0,
            "by_strategy": {}, "by_direction": {
                "LONG": {"trades": n, "pnl_net": long_pnl},
                "SHORT": {"trades": 0, "pnl_net": short_pnl},
            }
        }

    def _trades_weeks(self, weekly_pnls: list) -> list:
        """Genera trade sintetici: un trade per settimana, lunedi' di ogni settimana."""
        from datetime import date, timedelta
        trades = []
        # Parte da lunedi' 2026-01-05
        base = datetime(2026, 1, 5, 10, 0, 0)
        for i, pnl in enumerate(weekly_pnls):
            dt = base + timedelta(weeks=i)
            trades.append({"time": dt.isoformat(), "pnl_net": pnl})
        return trades

    # ── Criteri base ─────────────────────────────────────────────────────────

    def test_too_few_trades(self):
        m = self._make_metrics(49, 1.5, 50.0, 50.0)
        ok, reason = is_promising(m)
        self.assertFalse(ok)
        self.assertIn("50", reason)

    def test_pf_not_above_1(self):
        m = self._make_metrics(60, 1.0, 50.0, 50.0)
        ok, reason = is_promising(m)
        self.assertFalse(ok)
        self.assertIn("pf", reason)

    def test_negative_pnl(self):
        m = self._make_metrics(60, 1.5, -10.0, -10.0)
        ok, reason = is_promising(m)
        self.assertFalse(ok)

    def test_passes_all_criteria_long_only(self):
        """100% LONG non deve essere penalizzato in modalita' long-only."""
        m = self._make_metrics(60, 1.5, 50.0, long_pnl=50.0, short_pnl=0.0)
        ok, reason = is_promising(m, allow_short=False)
        self.assertTrue(ok, f"Atteso True (long-only), got: {reason}")

    # ── PATCH 12: dominanza lato ─────────────────────────────────────────────

    def test_dominant_side_flagged_when_allow_short(self):
        """Dominanza >90% LONG deve fallire quando allow_short=True."""
        m = self._make_metrics(60, 1.5, 50.0, long_pnl=49.0, short_pnl=1.0)
        ok, reason = is_promising(m, allow_short=True)
        self.assertFalse(ok)
        self.assertIn("LONG", reason)

    def test_dominant_side_ignored_when_long_only(self):
        """Dominanza 100% LONG non deve fallire quando allow_short=False."""
        m = self._make_metrics(60, 1.5, 50.0, long_pnl=50.0, short_pnl=0.0)
        ok, reason = is_promising(m, allow_short=False)
        self.assertTrue(ok, f"100% LONG non deve fallire in long-only: {reason}")

    def test_balanced_sides_passes_when_allow_short(self):
        """Lati bilanciati passano anche con allow_short=True."""
        m = self._make_metrics(60, 1.5, 50.0, long_pnl=30.0, short_pnl=20.0)
        ok, reason = is_promising(m, allow_short=True)
        self.assertTrue(ok, f"Atteso True (bilanciato): {reason}")

    # ── PATCH 11: concentrazione temporale ───────────────────────────────────

    def test_weekly_breakdown_keys_format(self):
        trades = self._trades_weeks([10.0, -5.0, 3.0, 7.0])
        wb = weekly_breakdown(trades)
        for k in wb:
            self.assertRegex(k, r"^\d{4}-W\d{2}$", f"Formato settimana non valido: {k}")
        self.assertEqual(len(wb), 4)

    def test_temporal_concentration_detected(self):
        """Una settimana con >50% delle perdite assolute totali deve essere flaggata."""
        # Settimana 1: -100, settimane 2-4: piccole perdite → conc. alta
        trades = self._trades_weeks([-100.0, -5.0, 3.0, 7.0])
        anom, why = temporal_concentration_check(trades)
        # total_abs = 100+5+3+7 = 115, worst=-100, conc=100/115=87% > 50% → anomalia
        self.assertTrue(anom, f"Concentrazione non rilevata: {why}")

    def test_temporal_concentration_not_detected_uniform(self):
        """Perdite uniformi non devono essere flaggate."""
        trades = self._trades_weeks([-20.0, -18.0, -22.0, -19.0, -21.0])
        anom, why = temporal_concentration_check(trades)
        # total_abs = 100, worst=-22, conc=22% < 50% → ok
        self.assertFalse(anom, f"Falso positivo: {why}")

    def test_temporal_concentration_insufficient_weeks(self):
        """Con meno di 3 settimane non deve segnalare anomalia."""
        trades = self._trades_weeks([-50.0, -50.0])
        anom, why = temporal_concentration_check(trades)
        self.assertFalse(anom, f"Non dovrebbe segnalare con 2 settimane: {why}")

    def test_temporal_concentration_no_losing_week(self):
        """Nessuna settimana in perdita non deve segnalare anomalia."""
        trades = self._trades_weeks([10.0, 20.0, 5.0, 15.0])
        anom, why = temporal_concentration_check(trades)
        self.assertFalse(anom, f"Nessuna perdita = nessuna anomalia: {why}")

    def test_is_promising_fails_on_temporal_concentration(self):
        """is_promising deve fallire se c'e' concentrazione temporale."""
        m = self._make_metrics(60, 1.5, 50.0, long_pnl=50.0)
        trades = self._trades_weeks([-100.0, -5.0, 3.0, 7.0, 8.0, 4.0, 3.0, 6.0])
        ok, reason = is_promising(m, trades=trades, allow_short=False)
        self.assertFalse(ok)
        self.assertIn("concentrazione", reason)

    def test_entry_allowed_false_for_wait_documented(self):
        """is_promising non viene mai applicato a segnali wait — test documentale."""
        m = self._make_metrics(0, 0.0, 0.0)
        ok, reason = is_promising(m)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
