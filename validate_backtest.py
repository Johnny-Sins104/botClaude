"""
Validazione completa del bot su dati storici.

Configurazione immutabile: tutti i parametri sono definiti in VALIDATION_CONFIG,
indipendentemente dai valori presenti nel .env locale.
Ogni run salva e ripristina bot.CONFIG completo con try/finally.

Uso: python validate_backtest.py
     python validate_backtest.py --test
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
    """Divide in train e OOS."""
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
    del training come warm-up per l'OOS. Nessun lookahead. Ritorna
    (combined_1m, combined_5m, warmup_count).
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


def weekly_breakdown(trades: list) -> dict:
    """Aggrega il P&L netto per settimana ISO (YYYY-Www)."""
    by_week: dict[str, list] = {}
    for t in trades:
        dt = datetime.fromisoformat(t["time"])
        week_key = dt.strftime("%G-W%V")
        by_week.setdefault(week_key, []).append(t["pnl_net"])
    return {w: round(sum(v), 2) for w, v in sorted(by_week.items())}


def temporal_concentration_check(trades: list) -> tuple[bool, str]:
    """
    Criterio deterministico di concentrazione temporale. Richiede almeno 3 settimane.
    Segnala anomalia se la settimana peggiore in perdita supera il 50% del P&L
    assoluto totale (somma dei valori assoluti di tutti i bucket settimanali).

    Formulazione esplicita:
        worst_week = settimana con P&L minimo (se < 0)
        total_abs  = sum(|P&L_settimanale| per ogni settimana)
        anomalia   = worst_week < 0 AND |worst_week| / total_abs > 0.50

    Funziona su finestre di 3+ settimane senza ipotesi di distribuzione normale.
    """
    weekly = weekly_breakdown(trades)
    weeks = sorted(weekly.items())
    if len(weeks) < 3:
        return False, f"settimane_insufficienti({len(weeks)}<3)"
    total_abs = sum(abs(v) for _, v in weeks)
    if total_abs < 1e-9:
        return False, "nessuna_varianza"
    worst_week = min(weeks, key=lambda x: x[1])
    if worst_week[1] >= 0:
        return False, f"nessuna_settimana_negativa (peggiore={worst_week[0]}:{worst_week[1]:+.2f})"
    concentration = abs(worst_week[1]) / total_abs
    if concentration > 0.50:
        return True, (f"concentrazione_{worst_week[0]}:"
                      f"pnl={worst_week[1]:+.2f}_conc={concentration:.0%}_totabs={total_abs:.2f}")
    return False, (f"ok (peggiore={worst_week[0]}:{worst_week[1]:+.2f}_conc={concentration:.0%})")


def is_promising(m: dict, trades: list | None = None,
                 allow_short: bool = False) -> tuple[bool, str]:
    """
    Una configurazione e' PROMETTENTE se e solo se:
    1. Almeno 50 trade
    2. Profit Factor > 1
    3. P&L netto positivo dopo costi
    4. [Solo se allow_short=True] Nessun lato domina per oltre il 90% del P&L assoluto
    5. Nessuna settimana concentra oltre il 50% delle perdite assolute totali

    Criterio 4 disabilitato in modalita' long-only (allow_short=False): con short
    disabilitati e' atteso che il 100% dei trade sia LONG, penalizzarlo sarebbe errato.

    Ritorna (bool, motivo) per documentazione esplicita.
    """
    if m["trades"] < 50:
        return False, f"trade_{m['trades']}_sotto_50"
    if m["profit_factor"] <= 1.0:
        return False, f"pf_{m['profit_factor']:.3f}_non_supera_1"
    if m["pnl_net"] <= 0:
        return False, f"pnl_netto_{m['pnl_net']:.2f}_non_positivo"
    if allow_short:
        long_abs = abs(m["by_direction"].get("LONG", {}).get("pnl_net", 0))
        short_abs = abs(m["by_direction"].get("SHORT", {}).get("pnl_net", 0))
        total = long_abs + short_abs
        if total > 0:
            dominant = max(long_abs, short_abs) / total
            if dominant > 0.9:
                side = "LONG" if long_abs > short_abs else "SHORT"
                return False, f"dominato_{side}_{dominant*100:.0f}pct"
    if trades:
        anom, why = temporal_concentration_check(trades)
        if anom:
            return False, f"concentrazione_temporale:{why}"
    return True, "ok"


# ── Report ────────────────────────────────────────────────────────────────────

def expectancy(m: dict) -> float:
    return m["pnl_net"] / m["trades"] if m["trades"] else 0.0


def print_report(label: str, m: dict, trades: list | None = None,
                 show_weekly: bool = False, cfg_used: dict | None = None,
                 allow_short: bool = False) -> None:
    ok, reason = is_promising(m, trades, allow_short)
    tag = " [PROMETTENTE]" if ok else f" [NON SUPERA: {reason}]"
    mode_str = "LONG+SHORT" if allow_short else "LONG-ONLY"
    long_d = m["by_direction"].get("LONG", {})
    short_d = m["by_direction"].get("SHORT", {})
    pnl_sign = "+" if m["pnl_net"] >= 0 else ""
    print(f"\n{LINE}")
    print(f"  {label}{tag}")
    print(DASH)
    print(f"  Modo  : {mode_str}")
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
    if show_weekly and trades:
        weekly = weekly_breakdown(trades)
        monthly = monthly_breakdown(trades)
        anom, why = temporal_concentration_check(trades)
        print(f"  Mensile  : {monthly}")
        print(f"  Settimane: {weekly}")
        print(f"  Conc.temp (>50%): {'SI - ' + why if anom else 'NO - ' + why}")
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
    print(f"\n  Criteri is_promising:")
    print(f"    1. >=50 trade")
    print(f"    2. PF > 1")
    print(f"    3. P&L netto > 0")
    print(f"    4. [Solo allow_short=True] nessun lato >90% del P&L assoluto")
    print(f"    5. Nessuna settimana >50% del P&L assoluto totale (conc. temporale)")

    print(f"\n{'='*72}")
    print("  PARTE 1 — 90 GIORNI COMPLETI")
    print(f"{'='*72}")

    # Auto selector, HTF on/off
    for htf in (True, False):
        htf_tag = "HTF-ON" if htf else "HTF-OFF"
        ov = {"auto_strategy_selector": True, "htf_trend_filter": htf}
        cfg_used = {**VALIDATION_CONFIG, **ov}
        allow_short = cfg_used.get("allow_short", False)
        r = run_with_validation(c1, c5, cap, overrides=ov)
        m = compute_metrics(r, cap)
        print_report(f"90d Auto {htf_tag} [CON COSTI]", m, r["trades"],
                     show_weekly=True, cfg_used=cfg_used, allow_short=allow_short)
        r_nc = run_without_costs(c1, c5, cap, overrides=ov)
        m_nc = compute_metrics(r_nc, cap)
        print_report(f"90d Auto {htf_tag} [SENZA COSTI]", m_nc, r_nc["trades"],
                     allow_short=allow_short)

    # Ogni strategia singola con HTF on e off
    for strat in ("ema", "bb", "macd", "ichi"):
        for htf in (True, False):
            htf_tag = "HTF-ON" if htf else "HTF-OFF"
            ov = {"auto_strategy_selector": False, "strategy": strat, "htf_trend_filter": htf}
            cfg_used = {**VALIDATION_CONFIG, **ov}
            allow_short = cfg_used.get("allow_short", False)
            r = run_with_validation(c1, c5, cap, overrides=ov)
            m = compute_metrics(r, cap)
            print_report(f"90d {strat.upper()} {htf_tag} [CON COSTI]", m, r["trades"],
                         show_weekly=True, cfg_used=cfg_used, allow_short=allow_short)

    print(f"\n{'='*72}")
    print(f"  PARTE 2 — SPLIT TRAIN 60d / OOS {oos_days}d")
    print(f"  OOS warm-up: {oos_warmup_count} candele 1m + "
          f"{len(oos_combined_5m)-len(oos_5m)} candele 5m")
    print(f"  Criterio anomalia: conc. temporale settimanale > 50%")
    print(f"{'='*72}")

    runs_oos: list[tuple[str, dict, dict, list, bool]] = []

    # Train
    ov_train = {"auto_strategy_selector": True, "htf_trend_filter": True}
    cfg_train = {**VALIDATION_CONFIG, **ov_train}
    r_train = run_with_validation(train_1m, train_5m, cap, overrides=ov_train)
    m_train = compute_metrics(r_train, cap)
    print_report("Train 60d Auto HTF-ON [CON COSTI]", m_train, r_train["trades"],
                 show_weekly=True, allow_short=cfg_train.get("allow_short", False))

    # OOS auto HTF on/off con warmup
    for htf in (True, False):
        htf_tag = "HTF-ON" if htf else "HTF-OFF"
        ov = {"auto_strategy_selector": True, "htf_trend_filter": htf}
        cfg_used = {**VALIDATION_CONFIG, **ov}
        allow_short = cfg_used.get("allow_short", False)
        r = run_with_validation(oos_combined_1m, oos_combined_5m, cap,
                                overrides=ov, warmup=oos_warmup_count)
        m = compute_metrics(r, cap)
        print_report(f"OOS {oos_days}d Auto {htf_tag} [CON COSTI]", m, r["trades"],
                     show_weekly=True, cfg_used=cfg_used, allow_short=allow_short)
        r_nc = run_without_costs(oos_combined_1m, oos_combined_5m, cap,
                                 overrides=ov, warmup=oos_warmup_count)
        m_nc = compute_metrics(r_nc, cap)
        print_report(f"OOS {oos_days}d Auto {htf_tag} [SENZA COSTI]", m_nc, r_nc["trades"],
                     allow_short=allow_short)
        runs_oos.append((f"OOS {oos_days}d Auto {htf_tag}", r, cfg_used, r["trades"], allow_short))

    # OOS per strategia singola con HTF on e off
    for strat in ("ema", "bb", "macd", "ichi"):
        for htf in (True, False):
            htf_tag = "HTF-ON" if htf else "HTF-OFF"
            ov = {"auto_strategy_selector": False, "strategy": strat, "htf_trend_filter": htf}
            cfg_used = {**VALIDATION_CONFIG, **ov}
            allow_short = cfg_used.get("allow_short", False)
            r = run_with_validation(oos_combined_1m, oos_combined_5m, cap,
                                    overrides=ov, warmup=oos_warmup_count)
            m = compute_metrics(r, cap)
            print_report(f"OOS {oos_days}d {strat.upper()} {htf_tag} [CON COSTI]", m, r["trades"],
                         show_weekly=True, cfg_used=cfg_used, allow_short=allow_short)
            runs_oos.append((f"OOS {oos_days}d {strat.upper()} {htf_tag}", r, cfg_used,
                             r["trades"], allow_short))

    # ── Verdetto ─────────────────────────────────────────────────────────────
    print(f"\n{'#'*72}")
    print("  VERDETTO FINALE")
    print(f"  Criteri: >=50 trade, PF>1, P&L>0, no conc.temporale >50%")
    print(f"  (criterio dominanza lato disabilitato: allow_short=False in VALIDATION_CONFIG)")
    print(f"{'#'*72}")

    promising = []
    for label, r, cfg, trades, allow_short in runs_oos:
        m = compute_metrics(r, cap)
        ok, reason = is_promising(m, trades, allow_short)
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
            print(f"    {label}: {m['trades']}t PF={m['profit_factor']:.3f} "
                  f"P&L={m['pnl_net']:+.2f} USDT")
        print("\n  AVVERTENZA: superare i criteri non garantisce profitti futuri.")

    print(f"\n{'#'*72}\n")


if __name__ == "__main__":
    if "--test" in sys.argv or "test" in sys.argv:
        # Tests moved to tests/test_validation.py — PATCH 11
        from tests import test_validation
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromModule(test_validation)
        runner = unittest.TextTestRunner(verbosity=2)
        result = runner.run(suite)
        sys.exit(0 if result.wasSuccessful() else 1)
    else:
        main()
