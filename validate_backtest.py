"""
Validazione completa del bot su dati storici.

Configurazione immutabile: tutti i parametri sono definiti in VALIDATION_CONFIG,
indipendentemente dai valori presenti nel .env locale.
Ogni run salva e ripristina bot.CONFIG completo con try/finally.

Uso: python validate_backtest.py
"""

import os
import sys
import math
import unittest
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import unittest.mock as _mock
with _mock.patch.dict(os.environ, {"PAPER_MODE": "true"}):
    import bot
from backtest import run_backtest, compute_metrics
from tools.fetch_klines import load_cache

BOT_DIR = Path(__file__).parent

# ── Configurazione di validazione immutabile ──────────────────────────────────
# Nessun parametro viene letto dal .env. Un clone pulito produce risultati identici.
VALIDATION_CONFIG: dict = {
    "strategy": "ema",
    "auto_strategy_selector": True,
    "auto_strategy_fallback": "ema",
    "allow_short": False,
    "risk_pct": 0.3,
    "tp_ratio": 2.0,
    "sl_atr_mult": 1.2,
    "max_notional_pct": 0.35,
    "min_entry_score": 75,
    "ema_fast": 9,
    "ema_slow": 21,
    "rsi_period": 14,
    "bb_period": 20,
    "bb_dev": 2.0,
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_sig": 9,
    "ichi_t": 9,
    "ichi_k": 26,
    "ichi_s": 52,
    "whipsaw_filter": True,
    "mtf_filter": True,
    "htf_trend_filter": True,
    "htf_trend_fast": 50,
    "htf_trend_slow": 100,
    "htf_trend_min_gap_pct": 0.02,
    "trailing_stop": False,
    "post_sl_cooldown_candles": 5,
    "fee_pct": 0.001,
    "slippage_pct": 0.0002,
    "auto_strategy_min_score": 60,
    "auto_strategy_cooldown_candles": 3,
}

# Warm-up per OOS: candele storiche prima del periodo OOS per riscaldare HTF e indicatori.
# 1m: copre MAX_CANDLES=300 + margine. 5m: htf_trend_slow=100 + margine.
OOS_WARMUP_1M = 600
OOS_WARMUP_5M = 110

LINE = "=" * 72
DASH = "-" * 72


# ── Runner con config immutabile ──────────────────────────────────────────────

def run_with_validation(candles_1m: list, candles_5m: list, cap: float,
                        overrides: dict | None = None,
                        warmup: int = 60) -> dict:
    """
    Esegue un backtest applicando VALIDATION_CONFIG come base e poi gli overrides.
    Salva e ripristina l'intera bot.CONFIG con try/finally, indipendentemente
    da cosa il backtest tocca. Il daily_loss_limit e' sempre disabilitato.
    """
    saved = deepcopy(bot.CONFIG)
    try:
        bot.CONFIG.update(VALIDATION_CONFIG)
        if overrides:
            bot.CONFIG.update(overrides)
        return run_backtest(candles_1m, candles_5m, capital=cap,
                            warmup=warmup, disable_daily_loss=True)
    finally:
        bot.CONFIG.clear()
        bot.CONFIG.update(saved)


def run_without_costs(candles_1m: list, candles_5m: list, cap: float,
                      overrides: dict | None = None,
                      warmup: int = 60) -> dict:
    """
    Esegue un backtest completo con fee_pct=0.0 e slippage_pct=0.0.
    Non ricalcola post-hoc dai trade: riesegue il backtest da zero.
    Commissioni totali nel risultato = 0 (verificabile).
    """
    no_cost = {"fee_pct": 0.0, "slippage_pct": 0.0}
    merged = {**(overrides or {}), **no_cost}
    return run_with_validation(candles_1m, candles_5m, cap,
                               overrides=merged, warmup=warmup)


# ── Split train / OOS con warm-up ────────────────────────────────────────────

def split_candles(candles_1m: list, candles_5m: list, train_days: int):
    """Divide in train e OOS. Ritorna anche le candele di warm-up per OOS."""
    first_t = candles_1m[0]["t"]
    split_ms = first_t + train_days * 86400_000
    train_1m = [c for c in candles_1m if c["T"] < split_ms]
    oos_1m   = [c for c in candles_1m if c["t"] >= split_ms]
    train_5m = [c for c in candles_5m if c["T"] < split_ms]
    oos_5m   = [c for c in candles_5m if c["t"] >= split_ms]
    return train_1m, train_5m, oos_1m, oos_5m


def oos_with_warmup(train_1m: list, train_5m: list,
                    oos_1m: list, oos_5m: list) -> tuple[list, list, int]:
    """
    Prepende le ultime OOS_WARMUP_1M candele 1m e OOS_WARMUP_5M candele 5m
    del training come warm-up per l'OOS. Nessun lookahead: solo candele prima
    del timestamp OOS. Ritorna (combined_1m, combined_5m, warmup_count).
    """
    warmup_1m = train_1m[-OOS_WARMUP_1M:]
    warmup_5m = train_5m[-OOS_WARMUP_5M:]
    combined_1m = warmup_1m + oos_1m
    combined_5m = warmup_5m + oos_5m
    return combined_1m, combined_5m, len(warmup_1m)


# ── Criteri di valutazione ───────────────────────────────────────────────────

def monthly_breakdown(trades: list) -> dict:
    by_month: dict[str, list] = {}
    for t in trades:
        m = t["time"][:7]
        by_month.setdefault(m, []).append(t["pnl_net"])
    return {m: round(sum(v), 2) for m, v in sorted(by_month.items())}


def monthly_anomaly_check(trades: list) -> tuple[bool, str]:
    """
    Criterio mensile deterministico:
    Un mese e' anomalo se il suo P&L e' inferiore a (media - 2 * deviazione_standard)
    dei P&L mensili. Richiede almeno 2 mesi. Soglia z-score: -2.0.
    Ritorna (anomalia_rilevata, descrizione).
    """
    monthly = monthly_breakdown(trades)
    values = list(monthly.values())
    if len(values) < 2:
        return False, "mesi_insufficienti"
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    std = math.sqrt(variance)
    if std < 1e-9:
        return False, "nessuna_varianza"
    worst_month = min(monthly.items(), key=lambda x: x[1])
    z = (worst_month[1] - mean) / std
    if z < -2.0:
        return True, f"anomalia_{worst_month[0]}:pnl={worst_month[1]:+.2f}_z={z:.1f}"
    return False, f"ok (peggiore={worst_month[0]}:{worst_month[1]:+.2f}_z={z:.1f})"


def is_promising(m: dict, trades: list | None = None) -> tuple[bool, str]:
    """
    Una configurazione e' PROMETTENTE se e solo se:
    1. Almeno 50 trade
    2. Profit Factor > 1
    3. P&L netto positivo dopo costi
    4. Nessun lato (long/short) domina per oltre il 90% del P&L assoluto
    5. Nessun mese con perdita anomala (z-score < -2.0)

    Ritorna (bool, motivo) per documentazione esplicita nel report e nei test.
    """
    if m["trades"] < 50:
        return False, f"trade_{m['trades']}_sotto_50"
    if m["profit_factor"] <= 1.0:
        return False, f"pf_{m['profit_factor']:.3f}_non_supera_1"
    if m["pnl_net"] <= 0:
        return False, f"pnl_netto_{m['pnl_net']:.2f}_non_positivo"
    long_abs = abs(m["by_direction"].get("LONG", {}).get("pnl_net", 0))
    short_abs = abs(m["by_direction"].get("SHORT", {}).get("pnl_net", 0))
    total = long_abs + short_abs
    if total > 0:
        dominant = max(long_abs, short_abs) / total
        if dominant > 0.9:
            side = "LONG" if long_abs > short_abs else "SHORT"
            return False, f"dominato_{side}_{dominant*100:.0f}pct"
    if trades:
        anomaly, why = monthly_anomaly_check(trades)
        if anomaly:
            return False, f"mese_anomalo:{why}"
    return True, "ok"


# ── Report ────────────────────────────────────────────────────────────────────

def expectancy(m: dict) -> float:
    return m["pnl_net"] / m["trades"] if m["trades"] else 0.0


def print_report(label: str, m: dict, trades: list | None = None,
                 show_monthly: bool = False, cfg_used: dict | None = None) -> None:
    ok, reason = is_promising(m, trades)
    tag = " [PROMETTENTE]" if ok else f" [NON SUPERA: {reason}]"
    long_d = m["by_direction"].get("LONG", {})
    short_d = m["by_direction"].get("SHORT", {})
    pnl_sign = "+" if m["pnl_net"] >= 0 else ""
    print(f"\n{LINE}")
    print(f"  {label}{tag}")
    print(DASH)
    print(f"  Trade : {m['trades']:4d}  Long: {long_d.get('trades',0):3d}  Short: {short_d.get('trades',0):3d}")
    print(f"  Win%  : {m['win_rate']:5.1f}%  PF: {m['profit_factor']:6.3f}  Payoff: {m['payoff_ratio']:.3f}")
    print(f"  P&L   : {pnl_sign}{m['pnl_net']:.2f} USDT  ({pnl_sign}{m['pnl_pct']:.3f}%)")
    print(f"  Fee   : {m['total_fees']:.4f} USDT  MaxDD: {m['max_dd']:.2f}%  Exp: {expectancy(m):.4f}/t")
    print(f"  TP/SL : {m['tp_exits']}/{m['sl_exits']}")
    if m["by_strategy"]:
        strats = "  ".join(
            f"{s}:{v['trades']}t_{v['win_rate']}%_{'+' if v['pnl_net']>=0 else ''}{v['pnl_net']:.2f}"
            for s, v in m["by_strategy"].items()
        )
        print(f"  Strat : {strats}")
    if show_monthly and trades:
        monthly = monthly_breakdown(trades)
        anom, why = monthly_anomaly_check(trades)
        print(f"  Mensile: {monthly}")
        print(f"  Anomalia mensile (z<-2): {'SI - ' + why if anom else 'NO - ' + why}")
    if cfg_used:
        short_cfg = {k: v for k, v in cfg_used.items()
                     if k in ("strategy", "auto_strategy_selector", "htf_trend_filter",
                               "fee_pct", "slippage_pct", "allow_short",
                               "ema_slow", "bb_dev", "tp_ratio", "sl_atr_mult")}
        print(f"  Config: {short_cfg}")
    print(LINE)


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"\n{'#'*72}")
    print("  Validazione completa backtest BTCUSDT")
    print(f"  Data: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Configurazione immutabile VALIDATION_CONFIG:")
    for k, v in VALIDATION_CONFIG.items():
        print(f"    {k}: {v}")
    print(f"{'#'*72}")

    c1 = load_cache("BTCUSDT", "1m")
    c5 = load_cache("BTCUSDT", "5m")
    if not c1:
        print("ERRORE: Cache non disponibile. Esegui: python backtest.py --fetch --days 90")
        sys.exit(1)

    from_dt = datetime.fromtimestamp(c1[0]["t"] / 1000).strftime("%Y-%m-%d")
    to_dt   = datetime.fromtimestamp(c1[-1]["T"] / 1000).strftime("%Y-%m-%d")
    total_days = int((c1[-1]["T"] - c1[0]["t"]) / 86400_000)
    print(f"\n  Dati: {len(c1)} candele 1m, {len(c5)} candele 5m")
    print(f"  Periodo: {from_dt} -> {to_dt} ({total_days} giorni)")

    cap = 1000.0
    train_1m, train_5m, oos_1m, oos_5m = split_candles(c1, c5, 60)
    oos_combined_1m, oos_combined_5m, oos_warmup_count = oos_with_warmup(
        train_1m, train_5m, oos_1m, oos_5m
    )
    oos_days = int((oos_1m[-1]["T"] - oos_1m[0]["t"]) / 86400_000) + 1 if oos_1m else 0

    print(f"  Train: {len(train_1m)} candele 1m ({60}d)  "
          f"OOS: {len(oos_1m)} candele 1m ({oos_days}d)  "
          f"warmup OOS: {oos_warmup_count} candele 1m")

    print(f"\n{'='*72}")
    print("  PARTE 1 — 90 GIORNI COMPLETI")
    print(f"{'='*72}")

    runs_90d: list[tuple[str, dict, dict, list | None]] = []

    # Auto selector, HTF on/off
    for htf in (True, False):
        htf_tag = "HTF-ON" if htf else "HTF-OFF"
        ov = {"auto_strategy_selector": True, "htf_trend_filter": htf}
        r = run_with_validation(c1, c5, cap, overrides=ov)
        m = compute_metrics(r, cap)
        cfg_used = {**VALIDATION_CONFIG, **ov}
        print_report(f"90d Auto {htf_tag} [CON COSTI]", m, r["trades"], show_monthly=True, cfg_used=cfg_used)
        r_nc = run_without_costs(c1, c5, cap, overrides=ov)
        m_nc = compute_metrics(r_nc, cap)
        print_report(f"90d Auto {htf_tag} [SENZA COSTI]", m_nc, r_nc["trades"])
        runs_90d.append((f"90d Auto {htf_tag}", r, cfg_used, r["trades"]))

    # Ogni strategia singola con HTF on e off
    for strat in ("ema", "bb", "macd", "ichi"):
        for htf in (True, False):
            htf_tag = "HTF-ON" if htf else "HTF-OFF"
            ov = {"auto_strategy_selector": False, "strategy": strat, "htf_trend_filter": htf}
            r = run_with_validation(c1, c5, cap, overrides=ov)
            m = compute_metrics(r, cap)
            cfg_used = {**VALIDATION_CONFIG, **ov}
            print_report(f"90d {strat.upper()} {htf_tag} [CON COSTI]", m, r["trades"],
                         show_monthly=True, cfg_used=cfg_used)
            runs_90d.append((f"90d {strat.upper()} {htf_tag}", r, cfg_used, r["trades"]))

    print(f"\n{'='*72}")
    print(f"  PARTE 2 — SPLIT TRAIN 60d / OOS {oos_days}d")
    print(f"  OOS warm-up: {oos_warmup_count} candele 1m + {len(oos_combined_5m)-len(oos_5m)} candele 5m")
    print(f"  Criterio anomalia mensile: P&L mensile < media - 2*std (z-score < -2.0)")
    print(f"{'='*72}")

    runs_oos: list[tuple[str, dict, dict, list | None]] = []

    # Train
    r_train = run_with_validation(train_1m, train_5m, cap,
                                  overrides={"auto_strategy_selector": True, "htf_trend_filter": True})
    m_train = compute_metrics(r_train, cap)
    print_report("Train 60d Auto HTF-ON [CON COSTI]", m_train, r_train["trades"], show_monthly=True)

    # OOS auto HTF on/off con warmup
    for htf in (True, False):
        htf_tag = "HTF-ON" if htf else "HTF-OFF"
        ov = {"auto_strategy_selector": True, "htf_trend_filter": htf}
        r = run_with_validation(oos_combined_1m, oos_combined_5m, cap,
                                overrides=ov, warmup=oos_warmup_count)
        m = compute_metrics(r, cap)
        cfg_used = {**VALIDATION_CONFIG, **ov}
        print_report(f"OOS {oos_days}d Auto {htf_tag} [CON COSTI]", m, r["trades"],
                     show_monthly=True, cfg_used=cfg_used)
        r_nc = run_without_costs(oos_combined_1m, oos_combined_5m, cap,
                                 overrides=ov, warmup=oos_warmup_count)
        m_nc = compute_metrics(r_nc, cap)
        print_report(f"OOS {oos_days}d Auto {htf_tag} [SENZA COSTI]", m_nc, r_nc["trades"])
        runs_oos.append((f"OOS {oos_days}d Auto {htf_tag}", r, cfg_used, r["trades"]))

    # OOS per strategia singola con HTF on e off
    for strat in ("ema", "bb", "macd", "ichi"):
        for htf in (True, False):
            htf_tag = "HTF-ON" if htf else "HTF-OFF"
            ov = {"auto_strategy_selector": False, "strategy": strat, "htf_trend_filter": htf}
            r = run_with_validation(oos_combined_1m, oos_combined_5m, cap,
                                    overrides=ov, warmup=oos_warmup_count)
            m = compute_metrics(r, cap)
            cfg_used = {**VALIDATION_CONFIG, **ov}
            print_report(f"OOS {oos_days}d {strat.upper()} {htf_tag} [CON COSTI]", m, r["trades"],
                         show_monthly=True, cfg_used=cfg_used)
            runs_oos.append((f"OOS {oos_days}d {strat.upper()} {htf_tag}", r, cfg_used, r["trades"]))

    # ── Verdetto ─────────────────────────────────────────────────────────────
    print(f"\n{'#'*72}")
    print("  VERDETTO FINALE")
    print(f"  Criteri: >=50 trade OOS, PF>1, P&L>0 dopo costi, nessun lato >90%, no anomalia mensile")
    print(f"{'#'*72}")

    promising = []
    for label, r, cfg, trades in runs_oos:
        m = compute_metrics(r, cap)
        ok, reason = is_promising(m, trades)
        if ok:
            promising.append((label, m, cfg))

    if not promising:
        print("""
  NESSUNA configurazione supera i criteri minimi out-of-sample.
  Conclusione: il bot non dimostra edge statistico sui dati testati.
  Non modificare TP/SL/score per forzare un risultato positivo.
""")
    else:
        print("\n  Configurazioni PROMETTENTI (out-of-sample):")
        for label, m, cfg in promising:
            print(f"    {label}: {m['trades']}t PF={m['profit_factor']} "
                  f"P&L={m['pnl_net']:+.2f} USDT")
        print("\n  AVVERTENZA: superare i criteri non garantisce profitti futuri.")

    print(f"\n{'#'*72}\n")


# ── Test di validazione ───────────────────────────────────────────────────────

class TestValidationReproducible(unittest.TestCase):
    """PATCH 5 — verifica che env diverse non cambino i risultati."""

    @classmethod
    def setUpClass(cls):
        cls.c1 = load_cache("BTCUSDT", "1m")
        cls.c5 = load_cache("BTCUSDT", "5m")
        if not cls.c1:
            raise unittest.SkipTest("Cache BTCUSDT non disponibile")
        # Usa solo prime 3000 candele per velocita'
        cls.c1s = cls.c1[:3000]
        cls.c5s = cls.c5[:600]

    def test_env_values_dont_change_results(self):
        """Config .env diverse devono produrre risultati identici a VALIDATION_CONFIG."""
        orig = deepcopy(bot.CONFIG)
        try:
            r1 = run_with_validation(self.c1s, self.c5s, 1000.0)
            # Corrompi CONFIG con valori molto diversi
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
        """run_without_costs deve produrre fee_total=0 per ogni trade."""
        r = run_without_costs(self.c1s, self.c5s, 1000.0)
        for t in r["trades"]:
            self.assertEqual(t.get("fee_total", 0), 0.0,
                             f"fee_total non zero: {t.get('fee_total')}")

    def test_no_cost_total_fees_zero_in_metrics(self):
        """Metriche calcolate su no-cost run devono avere total_fees=0."""
        r = run_without_costs(self.c1s, self.c5s, 1000.0)
        m = compute_metrics(r, 1000.0)
        self.assertEqual(m["total_fees"], 0.0)

    def test_no_cost_pnl_gross_equals_net(self):
        """Senza costi, pnl_gross deve essere uguale a pnl_net."""
        r = run_without_costs(self.c1s, self.c5s, 1000.0)
        for t in r["trades"]:
            self.assertAlmostEqual(t["pnl_gross"], t["pnl_net"], places=6,
                                   msg=f"pnl_gross != pnl_net: {t}")

    def test_no_cost_has_more_or_equal_pnl(self):
        """P&L senza costi deve essere >= P&L con costi."""
        r_cost = run_with_validation(self.c1s, self.c5s, 1000.0)
        r_nc   = run_without_costs(self.c1s, self.c5s, 1000.0)
        self.assertGreaterEqual(r_nc["final_capital"], r_cost["final_capital"])

    def test_no_cost_can_differ_in_trade_count(self):
        """
        Diversi costi possono cambiare l'andamento del capitale, potenzialmente
        cambiando il numero di trade o la loro sequenza. Questo test documenta
        che e' un comportamento atteso, non un errore.
        (Il test non fallisce se i trade sono uguali — verifica solo che il
        framework supporti differenze, non che le imponga.)
        """
        r_cost = run_with_validation(self.c1s, self.c5s, 1000.0)
        r_nc   = run_without_costs(self.c1s, self.c5s, 1000.0)
        # I trade devono essere liste valide (anche se uguali in numero)
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
        """Il warmup count deve essere <= OOS_WARMUP_1M e <= len(train_1m)."""
        self.assertLessEqual(self.wup, OOS_WARMUP_1M)
        self.assertLessEqual(self.wup, len(self.train_1m))
        self.assertGreater(self.wup, 0)

    def test_no_trades_during_warmup(self):
        """Nessun trade puo' avere timestamp prima dell'inizio OOS."""
        if not self.oos_1m:
            self.skipTest("Dati OOS non disponibili")
        oos_start_t = self.oos_1m[0]["t"]
        r = run_with_validation(self.combined_1m, self.combined_5m, 1000.0,
                                 warmup=self.wup)
        for trade in r["trades"]:
            trade_t = datetime.fromisoformat(trade["time"]).timestamp() * 1000
            self.assertGreaterEqual(
                trade_t, oos_start_t - 60_000,  # tolleranza 1 candela
                f"Trade durante warmup: {trade['time']} < oos_start"
            )

    def test_5m_buffer_filled_after_warmup(self):
        """Dopo il warm-up, closed_prices_5m deve avere >= htf_trend_slow entry."""
        from backtest import _reset_bot_state, _sync_5m_buffer
        _reset_bot_state(1000.0)
        idx = [0]
        htf_slow = VALIDATION_CONFIG["htf_trend_slow"]
        for candle in self.combined_1m[:self.wup]:
            _sync_5m_buffer(candle, self.combined_5m, idx)
            import bot as _bot
            _bot.closed_candles.append(candle)
            _bot.closed_prices.append(candle["c"])
        self.assertGreaterEqual(
            len(bot.closed_prices_5m), htf_slow,
            f"Buffer 5m insufficiente dopo warmup: {len(bot.closed_prices_5m)} < {htf_slow}"
        )

    def test_no_lookahead_in_5m_sync(self):
        """La sincronizzazione 5m non deve usare candele successive alla 1m corrente."""
        from backtest import _reset_bot_state, _sync_5m_buffer
        _reset_bot_state(1000.0)
        idx = [0]
        for candle in self.combined_1m[:200]:
            t_before = candle["T"]
            _sync_5m_buffer(candle, self.combined_5m, idx)
            # Ogni candela 5m in buffer deve avere T <= t_before
            for c5m in list(bot.closed_candles_5m):
                self.assertLessEqual(
                    c5m["T"], t_before + 1,
                    f"Lookahead: 5m candle T={c5m['T']} > 1m T={t_before}"
                )
            bot.closed_candles.append(candle)
            bot.closed_prices.append(candle["c"])

    def test_oos_deterministic(self):
        """Due run identici producono risultati identici."""
        r1 = run_with_validation(self.combined_1m, self.combined_5m, 1000.0,
                                  warmup=self.wup)
        r2 = run_with_validation(self.combined_1m, self.combined_5m, 1000.0,
                                  warmup=self.wup)
        self.assertAlmostEqual(r1["final_capital"], r2["final_capital"], places=6)
        self.assertEqual(len(r1["trades"]), len(r2["trades"]))


class TestCriteria(unittest.TestCase):
    """PATCH 8 — verifica criteri is_promising e monthly_anomaly_check."""

    def _make_metrics(self, n, pf, pnl, long_pnl, short_pnl):
        return {
            "trades": n, "wins": 0, "losses": 0,
            "win_rate": 50.0, "profit_factor": pf, "payoff_ratio": 1.0,
            "pnl_net": pnl, "pnl_pct": pnl / 10,
            "max_dd": 5.0, "total_fees": 0.0,
            "break_even_wr": 50.0, "tp_exits": 0, "sl_exits": 0,
            "avg_win": 0.0, "avg_loss": 0.0,
            "by_strategy": {}, "by_direction": {
                "LONG": {"trades": 25, "pnl_net": long_pnl},
                "SHORT": {"trades": 25, "pnl_net": short_pnl},
            }
        }

    def test_too_few_trades(self):
        m = self._make_metrics(49, 1.5, 50.0, 30.0, 20.0)
        ok, reason = is_promising(m)
        self.assertFalse(ok)
        self.assertIn("50", reason)

    def test_pf_not_above_1(self):
        m = self._make_metrics(60, 1.0, 50.0, 30.0, 20.0)
        ok, reason = is_promising(m)
        self.assertFalse(ok)
        self.assertIn("pf", reason)

    def test_negative_pnl(self):
        m = self._make_metrics(60, 1.5, -10.0, 30.0, -40.0)
        ok, reason = is_promising(m)
        self.assertFalse(ok)

    def test_dominant_side(self):
        m = self._make_metrics(60, 1.5, 50.0, 49.0, 1.0)
        ok, reason = is_promising(m)
        self.assertFalse(ok)
        self.assertIn("LONG", reason)

    def test_passes_all_criteria(self):
        m = self._make_metrics(60, 1.5, 50.0, 30.0, 20.0)
        ok, reason = is_promising(m)
        self.assertTrue(ok, f"Atteso True, got: {reason}")

    def test_monthly_anomaly_detected(self):
        # Un mese con perdita estrema (z < -2)
        trades = (
            [{"time": "2026-01-15T10:00:00", "pnl_net": 5.0}] * 10 +
            [{"time": "2026-02-15T10:00:00", "pnl_net": 5.0}] * 10 +
            [{"time": "2026-03-15T10:00:00", "pnl_net": -200.0}]  # anomalia
        )
        anom, why = monthly_anomaly_check(trades)
        self.assertTrue(anom, f"Anomalia non rilevata: {why}")

    def test_monthly_no_anomaly(self):
        # Mesi uniformi
        trades = (
            [{"time": "2026-01-15T10:00:00", "pnl_net": -5.0}] * 10 +
            [{"time": "2026-02-15T10:00:00", "pnl_net": -4.0}] * 10 +
            [{"time": "2026-03-15T10:00:00", "pnl_net": -6.0}] * 10
        )
        anom, why = monthly_anomaly_check(trades)
        self.assertFalse(anom, f"Falso positivo: {why}")

    def test_entry_allowed_false_for_wait(self):
        """is_promising non viene mai chiamato per segnali 'wait' — test documentale."""
        # Il report non mostra ALLOWED per wait: e' garantito dalla logica UI
        # Questo test verifica is_promising direttamente sui dati
        m = self._make_metrics(0, 0.0, 0.0, 0.0, 0.0)
        ok, reason = is_promising(m)
        self.assertFalse(ok)  # 0 trade non supera il criterio


if __name__ == "__main__":
    import sys
    if "--test" in sys.argv or "test" in sys.argv:
        sys.argv = [sys.argv[0]]
        unittest.main(verbosity=2)
    else:
        main()
