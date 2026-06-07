"""
PATCH 4 — Validazione completa del bot su 90 giorni.

Esegue backtest con costi realistici (FEE_PCT=0.001, SLIPPAGE_PCT=0.0002):
  - BTCUSDT 90 giorni interi
  - Train 60d / Test out-of-sample 30d
  - Ogni strategia singola
  - Auto strategy selector
  - HTF attivo e disattivo
  - Risultati con e senza costi separati

Criteri minimi per "configurazione promettente":
  - >= 50 trade
  - Profit Factor out-of-sample > 1
  - P&L positivo dopo fee e slippage
  - Non dipende esclusivamente da long o short
  - Nessun mese con perdita anomala

Uso: python validate_backtest.py
"""

import os
import sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import unittest.mock as _mock
with _mock.patch.dict(os.environ, {"PAPER_MODE": "true"}):
    import bot
from backtest import run_backtest, compute_metrics
from tools.fetch_klines import load_cache

BOT_DIR = Path(__file__).parent

LINE = "=" * 72
DASH = "-" * 72


def _pct(v: float, total: float) -> str:
    if total == 0:
        return "n/a"
    return f"{v/total*100:.1f}%"


def expectancy(m: dict) -> float:
    if not m["trades"]:
        return 0.0
    return m["pnl_net"] / m["trades"]


def print_full_report(label: str, m: dict, cap: float) -> None:
    trades = m["trades"]
    longs = m["by_direction"].get("LONG", {})
    shorts = m["by_direction"].get("SHORT", {})
    long_t = longs.get("trades", 0)
    short_t = shorts.get("trades", 0)
    pnl_sign = "+" if m["pnl_net"] >= 0 else ""
    promising = _is_promising(m)
    tag = " [PROMETTENTE]" if promising else " [NON SUPERA CRITERI]"
    print(f"\n{LINE}")
    print(f"  {label}{tag}")
    print(DASH)
    print(f"  Trade totali : {trades}  |  Long: {long_t}  Short: {short_t}")
    print(f"  Win rate     : {m['win_rate']}%  |  Profit Factor: {m['profit_factor']}")
    print(f"  P&L netto    : {pnl_sign}{m['pnl_net']:.2f} USDT  ({pnl_sign}{m['pnl_pct']:.3f}%)")
    print(f"  Commissioni  : {m['total_fees']:.4f} USDT")
    print(f"  Max Drawdown : {m['max_dd']:.2f}%")
    print(f"  Expectancy   : {expectancy(m):.4f} USDT/trade")
    print(f"  TP/SL uscite : {m['tp_exits']}/{m['sl_exits']}")
    if m["by_strategy"]:
        strats = "  ".join(
            f"{s}: {v['trades']}t {v['win_rate']}% {'+' if v['pnl_net']>=0 else ''}{v['pnl_net']:.2f}"
            for s, v in m["by_strategy"].items()
        )
        print(f"  Strategie    : {strats}")
    print(LINE)


def _is_promising(m: dict) -> bool:
    if m["trades"] < 50:
        return False
    if m["profit_factor"] <= 1.0:
        return False
    if m["pnl_net"] <= 0:
        return False
    longs = m["by_direction"].get("LONG", {})
    shorts = m["by_direction"].get("SHORT", {})
    long_pnl = longs.get("pnl_net", 0)
    short_pnl = shorts.get("pnl_net", 0)
    total = abs(long_pnl) + abs(short_pnl)
    if total > 0:
        dominant_pct = max(abs(long_pnl), abs(short_pnl)) / total
        if dominant_pct > 0.9:
            return False
    return True


def _no_cost_metrics(trades: list, equity: list, cap: float) -> dict:
    """Ricalcola metriche rimuovendo fee e slippage."""
    gross_trades = []
    cur = cap
    eq = [cap]
    for t in trades:
        gross_pnl = t.get("pnl_gross", t["pnl_net"])
        cur += gross_pnl
        eq.append(round(cur, 2))
        gross_trades.append({**t, "pnl_net": gross_pnl})
    return compute_metrics({"trades": gross_trades, "equity": eq}, cap)


def split_candles(candles_1m: list, candles_5m: list, train_days: int, total_days: int):
    """Divide le candele in finestra train e test out-of-sample."""
    first_t = candles_1m[0]["t"]
    train_end_ms = first_t + train_days * 86400_000
    test_start_ms = first_t + train_days * 86400_000

    train_1m = [c for c in candles_1m if c["T"] < train_end_ms]
    test_1m  = [c for c in candles_1m if c["t"] >= test_start_ms]
    train_5m = [c for c in candles_5m if c["T"] < train_end_ms]
    test_5m  = [c for c in candles_5m if c["t"] >= test_start_ms]
    return train_1m, train_5m, test_1m, test_5m


def run_config(label: str, candles_1m: list, candles_5m: list, cap: float,
               config_overrides: dict) -> dict:
    orig = {}
    for k, v in config_overrides.items():
        orig[k] = bot.CONFIG[k]
        bot.CONFIG[k] = v
    try:
        result = run_backtest(candles_1m, candles_5m, capital=cap)
    finally:
        for k, v in orig.items():
            bot.CONFIG[k] = v
    return result


def monthly_breakdown(trades: list) -> dict:
    by_month: dict[str, list] = {}
    for t in trades:
        m = t["time"][:7]
        by_month.setdefault(m, []).append(t["pnl_net"])
    return {m: round(sum(v), 2) for m, v in sorted(by_month.items())}


def main() -> None:
    print(f"\n{'#'*72}")
    print("  PATCH 4 — Validazione completa backtest BTCUSDT")
    print(f"  Data: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  FEE_PCT={bot.CONFIG['fee_pct']}  SLIPPAGE_PCT={bot.CONFIG['slippage_pct']}")
    print(f"{'#'*72}")

    c1 = load_cache("BTCUSDT", "1m")
    c5 = load_cache("BTCUSDT", "5m")
    if not c1:
        print("ERRORE: Cache non disponibile. Esegui: python backtest.py --fetch --days 90")
        sys.exit(1)

    from_dt = datetime.fromtimestamp(c1[0]["t"] / 1000).strftime("%Y-%m-%d")
    to_dt = datetime.fromtimestamp(c1[-1]["T"] / 1000).strftime("%Y-%m-%d")
    total_days = (c1[-1]["T"] - c1[0]["t"]) / 86400_000
    print(f"\n  Dati: {len(c1)} candele 1m, {len(c5)} candele 5m")
    print(f"  Periodo: {from_dt} -> {to_dt} ({total_days:.0f} giorni)")

    cap = 1000.0
    train_1m, train_5m, test_1m, test_5m = split_candles(c1, c5, 60, 90)

    print(f"\n  Finestra train:      {len(train_1m)} candele 1m  ({60} giorni)")
    print(f"  Finestra test OOS:   {len(test_1m)} candele 1m  ({int(total_days)-60} giorni)")

    results = {}

    # ── 1. BTCUSDT 90 giorni — auto selector, HTF on ──────────────────────────
    r = run_config("90d auto+HTF", c1, c5, cap,
                   {"auto_strategy_selector": True, "htf_trend_filter": True})
    results["90d_auto_htf_on"] = r
    m = compute_metrics(r, cap)
    print_full_report("BTCUSDT 90d — Auto selector, HTF ON  [CON COSTI]", m, cap)
    mg = _no_cost_metrics(r["trades"], r["equity"], cap)
    print_full_report("BTCUSDT 90d — Auto selector, HTF ON  [SENZA COSTI]", mg, cap)
    monthly = monthly_breakdown(r["trades"])
    if monthly:
        print(f"  Breakdown mensile P&L: {monthly}")

    # ── 2. 90d — auto, HTF off ────────────────────────────────────────────────
    r = run_config("90d auto HTF-off", c1, c5, cap,
                   {"auto_strategy_selector": True, "htf_trend_filter": False})
    results["90d_auto_htf_off"] = r
    m = compute_metrics(r, cap)
    print_full_report("BTCUSDT 90d — Auto selector, HTF OFF  [CON COSTI]", m, cap)

    # ── 3. Ogni strategia singola — 90d, HTF off (isolamento) ─────────────────
    for strat in ("ema", "bb", "macd", "ichi"):
        r = run_config(f"90d {strat}", c1, c5, cap,
                       {"auto_strategy_selector": False, "strategy": strat,
                        "htf_trend_filter": False})
        results[f"90d_{strat}"] = r
        m = compute_metrics(r, cap)
        print_full_report(f"BTCUSDT 90d — Strategia {strat.upper()}, HTF OFF  [CON COSTI]", m, cap)

    # ── 4. Train 60d / Test OOS 30d — auto, HTF on ────────────────────────────
    print(f"\n{LINE}")
    print("  SPLIT TRAIN/TEST (60d train -> 30d out-of-sample)")
    print(LINE)

    r_train = run_config("train60d", train_1m, train_5m, cap,
                         {"auto_strategy_selector": True, "htf_trend_filter": True})
    r_test = run_config("test30d", test_1m, test_5m, cap,
                        {"auto_strategy_selector": True, "htf_trend_filter": True})
    results["train_60d"] = r_train
    results["test_30d_oos"] = r_test

    m_train = compute_metrics(r_train, cap)
    m_test = compute_metrics(r_test, cap)
    print_full_report("Train 60d — Auto+HTF  [CON COSTI]", m_train, cap)
    print_full_report("Test OOS 30d — Auto+HTF  [CON COSTI]", m_test, cap)

    mg_test = _no_cost_metrics(r_test["trades"], r_test["equity"], cap)
    print_full_report("Test OOS 30d — Auto+HTF  [SENZA COSTI]", mg_test, cap)

    # ── 5. OOS per strategia singola ──────────────────────────────────────────
    print(f"\n{LINE}")
    print("  OUT-OF-SAMPLE 30d — strategie singole, HTF off")
    print(LINE)
    for strat in ("ema", "bb", "macd", "ichi"):
        r = run_config(f"oos30d {strat}", test_1m, test_5m, cap,
                       {"auto_strategy_selector": False, "strategy": strat,
                        "htf_trend_filter": False})
        results[f"oos30d_{strat}"] = r
        m = compute_metrics(r, cap)
        print_full_report(f"OOS 30d — {strat.upper()}, HTF OFF  [CON COSTI]", m, cap)

    # ── Verdetto finale ───────────────────────────────────────────────────────
    print(f"\n{'#'*72}")
    print("  VERDETTO FINALE")
    print(f"{'#'*72}")

    promising_configs = []
    for label, r in [
        ("90d Auto+HTF ON", results["90d_auto_htf_on"]),
        ("90d Auto+HTF OFF", results["90d_auto_htf_off"]),
        ("90d EMA", results["90d_ema"]),
        ("90d BB", results["90d_bb"]),
        ("90d MACD", results["90d_macd"]),
        ("90d ICHI", results["90d_ichi"]),
        ("OOS 30d Auto+HTF", results["test_30d_oos"]),
    ]:
        m = compute_metrics(r, cap)
        if _is_promising(m):
            promising_configs.append((label, m))

    if not promising_configs:
        print("""
  NESSUNA configurazione testata supera i criteri minimi:
    - >= 50 trade
    - Profit Factor out-of-sample > 1
    - P&L positivo dopo fee e slippage
    - Non dipendente esclusivamente da long o short

  Conclusione: il bot non dimostra edge statistico sui dati testati.
  Non modificare TP/SL/score per forzare un risultato positivo.
  Correggere prima il sistema di segnali o ampliare l'analisi.
""")
    else:
        print("\n  Configurazioni che superano i criteri minimi:")
        for label, m in promising_configs:
            print(f"    {label}: {m['trades']}t PF={m['profit_factor']} P&L={m['pnl_net']:+.2f}")
        print("\n  AVVERTENZA: superare i criteri minimi non garantisce profitti futuri.")
        print("  Validare su periodi e simboli diversi prima di qualsiasi uso reale.")

    print(f"\n{'#'*72}\n")


if __name__ == "__main__":
    main()
