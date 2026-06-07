"""
Backtest del paper scalping bot su klines storiche.

Riusa DIRETTAMENTE le funzioni di bot.py (segnali, ATR, sizing, fills, P&L)
per garantire parità backtest↔live. Non usa WebSocket né FastAPI.

Uso:
    python backtest.py                        # run con config attuale (.env)
    python backtest.py --baseline             # run con config "old" (TP=2, SL=1.2, ATR=1m)
    python backtest.py --sweep                # grid search TP × SL × score
    python backtest.py --days 14              # usa 14 giorni di dati
    python backtest.py --symbol ETHUSDT       # altro simbolo
    python backtest.py --strategy ema         # forza una strategia (disabilita selector)
"""

import argparse
import json
import os
import sys
from collections import deque
from copy import deepcopy
from datetime import datetime
from pathlib import Path

# ── Bootstrap: carica .env prima dell'import di bot ──────────────────────────
from dotenv import load_dotenv
BOT_DIR = Path(__file__).parent.resolve()
load_dotenv(BOT_DIR / ".env")

# Impedisce a bot.py di avviare uvicorn o connessioni Binance all'import
import unittest.mock as _mock
with _mock.patch.dict(os.environ, {"PAPER_MODE": "true"}):
    import bot  # importa il modulo reale

from tools.fetch_klines import load_cache, main as fetch_main
import asyncio

LOG_DIR = BOT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers reset
# ─────────────────────────────────────────────────────────────────────────────

def _reset_bot_state(capital: float) -> None:
    """Resetta tutti i buffer e lo stato di bot per un run pulito."""
    bot.closed_candles.clear()
    bot.closed_prices.clear()
    bot.closed_volumes.clear()
    bot.closed_candles_5m.clear()
    bot.closed_prices_5m.clear()
    bot.state.update({
        "running": False, "open_position": None, "capital": capital,
        "init_capital": capital, "trades": [], "equity_curve": [capital],
        "metrics": {"total_pnl": 0.0, "total_pnl_pct": 0.0, "trades": 0,
                    "wins": 0, "win_rate": 0.0, "max_dd": 0.0, "peak_capital": capital},
        "entry_cooldowns": {"buy": 0, "sell": 0},
        "last_sl_direction": None, "last_sl_time": None,
        "kill_switch_active": False, "trading_halted": False,
        "risk_guard": {"daily_date": datetime.now().date().isoformat(),
                       "daily_start_capital": capital, "daily_realized_pnl": 0.0,
                       "daily_unrealized_pnl": 0.0, "daily_total_pnl": 0.0,
                       "daily_loss_limit_pct": bot.CONFIG["daily_loss_limit_pct"],
                       "daily_loss_limit_amount": capital * bot.CONFIG["daily_loss_limit_pct"] / 100,
                       "daily_loss_limit_enabled": False,  # disabilitato in backtest
                       "daily_loss_limit_hit": False, "daily_loss_limit_hit_time": None},
        "last_signal_candle_time": None, "active_strategy": bot.CONFIG["strategy"],
        "strategy": bot.CONFIG["strategy"], "market_regime": "manual",
        "strategy_selector_reason": "backtest", "strategy_scores": {},
        "strategy_cooldown_remaining": 0, "last_strategy_switch_candle_time": None,
        "fallback_strategy": bot.CONFIG["auto_strategy_fallback"],
        "indicators": {}, "signal": {}, "filters": {},
        "log": [], "errors": [], "last_update": None,
        "last_closed_candle_time": None, "last_monitor_log_candle_time": None,
        "last_signal_update_time": None, "last_signal_close_price": None,
        "last_live_price": 0.0, "price": 0.0,
        "historical_bootstrap_status": "ready", "historical_candles_loaded_1m": 0,
        "historical_candles_loaded_5m": 0,
    })


def _sync_5m_buffer(candle_1m: dict, candles_5m: list, idx_5m_state: list) -> None:
    """Aggiunge al buffer 5m tutte le candele chiuse prima della 1m corrente."""
    t_1m = candle_1m["T"]
    while idx_5m_state[0] < len(candles_5m):
        c5m = candles_5m[idx_5m_state[0]]
        if c5m["T"] <= t_1m:
            bot.closed_candles_5m.append(c5m)
            bot.closed_prices_5m.append(c5m["c"])
            idx_5m_state[0] += 1
        else:
            break


# ─────────────────────────────────────────────────────────────────────────────
# Motore di backtest
# ─────────────────────────────────────────────────────────────────────────────

def run_backtest(candles_1m: list, candles_5m: list, capital: float = 1000.0,
                 warmup: int = 60, disable_daily_loss: bool = True) -> dict:
    """
    Esegue il backtest su candles_1m sincronizzando il buffer 5m.
    warmup: numero di candele 1m da ingerire prima di iniziare a tradare.
    disable_daily_loss: se True (default), disabilita il daily_loss_limit_enabled in CONFIG
        per tutta la durata del run, prevenendo stop prematuri a ~-3%.
        Il valore originale viene sempre ripristinato, anche in caso di errore.
    """
    _saved_daily_loss = bot.CONFIG["daily_loss_limit_enabled"]
    if disable_daily_loss:
        bot.CONFIG["daily_loss_limit_enabled"] = False
    try:
        return _run_backtest_inner(candles_1m, candles_5m, capital, warmup)
    finally:
        bot.CONFIG["daily_loss_limit_enabled"] = _saved_daily_loss


def _run_backtest_inner(candles_1m: list, candles_5m: list, capital: float,
                        warmup: int) -> dict:
    _reset_bot_state(capital)
    trades = []
    open_pos = None  # posizione corrente (dict _build_position_params)
    idx_5m = [0]     # cursore sul buffer 5m (lista mutabile per closure)
    equity = [capital]
    current_capital = capital

    for i, candle in enumerate(candles_1m):
        # Sincronizza buffer 5m con il timestamp della candela 1m corrente
        _sync_5m_buffer(candle, candles_5m, idx_5m)

        # Ingesta candela nel motore
        bot.closed_candles.append(candle)
        bot.closed_prices.append(candle["c"])
        bot.closed_volumes.append(candle["v"])

        # Controlla SL/TP sulla posizione aperta usando high/low della candela
        if open_pos is not None:
            pos = open_pos
            high, low = candle["h"], candle["l"]
            # Worst-case: se entrambi vengono toccati, assume SL prima
            hit_sl = (pos["dir"] == 1 and low <= pos["sl"]) or \
                     (pos["dir"] == -1 and high >= pos["sl"])
            hit_tp = (pos["dir"] == 1 and high >= pos["tp"]) or \
                     (pos["dir"] == -1 and low <= pos["tp"])

            if hit_sl or hit_tp:
                exit_price = pos["sl"] if hit_sl else pos["tp"]
                reason = "SL" if hit_sl else "TP"
                pnl = bot._position_pnl(pos, exit_price)
                current_capital += pnl["pnl_net"]
                equity.append(round(current_capital, 2))
                if hit_sl:
                    side = "buy" if pos["dir"] == 1 else "sell"
                    bot.state["entry_cooldowns"][side] = bot.CONFIG["post_sl_cooldown_candles"]
                    bot.state["last_sl_direction"] = pos["direction"]
                trades.append({
                    "time": datetime.fromtimestamp(candle["T"] / 1000).isoformat(),
                    "direction": pos["direction"],
                    "entry": pos["entry_price_fill"],
                    "exit": round(bot._exit_fill(exit_price, pos["dir"]), 6),
                    "size": pos["size"],
                    "reason": reason,
                    "strategy": pos["strategy"],
                    "market_regime": pos.get("market_regime"),
                    "pnl_gross": round(pnl["pnl_gross"], 4),
                    "pnl_net": round(pnl["pnl_net"], 4),
                    "pnl": round(pnl["pnl_net"], 4),
                    "fee_total": round(pnl["fee_total"], 8),
                    "notional": pos["notional"],
                    "capital_after": round(current_capital, 4),
                })
                open_pos = None
                bot.state["open_position"] = None
                bot.state["capital"] = current_capital

        # Warmup: non tradare nelle prime N candele
        if i < warmup:
            bot._tick_entry_cooldowns()
            continue

        # Segnale solo se non c'è posizione aperta
        if open_pos is not None:
            bot._tick_entry_cooldowns()
            continue

        bot._tick_entry_cooldowns()

        # Blocchi di risk (kill switch, loss limit) — in backtest disabilitiamo daily limit
        block = bot._entry_block_reason()
        if block:
            continue

        # Selezione strategia + segnale
        active = bot.select_active_strategy(candle["T"])
        raw = bot.get_signal(active, candle["T"], candle["c"])
        sig_type = raw.get("type", "wait")
        score = raw.get("score", 0) or 0

        if sig_type not in {"buy", "sell"}:
            continue

        # Filtro score
        block_sig = bot._entry_block_reason_for_signal(sig_type, score)
        if block_sig:
            continue

        # Filtro anti-late-entry: solo per strategie trend/momentum.
        # BB è mean-reversion: il drop che il filtro scarta è la condizione entry.
        # Ichi filtra già internamente: signal_ichi() ritorna "wait" se rebound risk.
        if active in {"ema", "macd"}:
            rebound, _ = bot._recent_rebound_risk(sig_type)
            if rebound:
                continue

        # Sizing
        params = bot._build_position_params(
            sig_type, candle["c"], current_capital, active,
            candle["T"], bot.state.get("market_regime"), bot.state.get("strategy_selector_reason"),
        )
        if params is None:
            continue

        params["open_time"] = datetime.fromtimestamp(candle["T"] / 1000).isoformat()
        open_pos = params
        bot.state["open_position"] = params
        bot.state["capital"] = current_capital

    # Chiudi posizione residua a fine dati (prezzo ultimo close)
    if open_pos is not None and candles_1m:
        last = candles_1m[-1]
        pnl = bot._position_pnl(open_pos, last["c"])
        current_capital += pnl["pnl_net"]
        equity.append(round(current_capital, 2))
        trades.append({
            "time": datetime.fromtimestamp(last["T"] / 1000).isoformat(),
            "direction": open_pos["direction"],
            "entry": open_pos["entry_price_fill"],
            "exit": round(bot._exit_fill(last["c"], open_pos["dir"]), 6),
            "size": open_pos["size"],
            "reason": "EOD",  # End Of Data
            "strategy": open_pos["strategy"],
            "market_regime": open_pos.get("market_regime"),
            "pnl_gross": round(pnl["pnl_gross"], 4),
            "pnl_net": round(pnl["pnl_net"], 4),
            "pnl": round(pnl["pnl_net"], 4),
            "fee_total": round(pnl["fee_total"], 8),
            "notional": open_pos["notional"],
            "capital_after": round(current_capital, 4),
        })

    return {"trades": trades, "equity": equity, "final_capital": current_capital}


# ─────────────────────────────────────────────────────────────────────────────
# Report metriche
# ─────────────────────────────────────────────────────────────────────────────

def compute_metrics(result: dict, init_capital: float) -> dict:
    trades = result["trades"]
    equity = result["equity"]
    if not trades:
        return {"trades": 0, "wins": 0, "win_rate": 0, "profit_factor": 0,
                "payoff_ratio": 0, "pnl_net": 0, "pnl_pct": 0, "max_dd": 0,
                "total_fees": 0}

    wins = [t for t in trades if t["pnl_net"] > 0]
    losses = [t for t in trades if t["pnl_net"] <= 0]
    total_profit = sum(t["pnl_net"] for t in wins)
    total_loss = abs(sum(t["pnl_net"] for t in losses))
    pnl_net = sum(t["pnl_net"] for t in trades)

    profit_factor = total_profit / total_loss if total_loss > 0 else float("inf")
    avg_win = total_profit / len(wins) if wins else 0
    avg_loss = total_loss / len(losses) if losses else 0
    payoff = avg_win / avg_loss if avg_loss > 0 else float("inf")
    win_rate = len(wins) / len(trades) * 100

    # Max drawdown su equity curve
    peak = equity[0]
    max_dd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)

    total_fees = sum(t.get("fee_total", 0) for t in trades)
    be_wr = 1 / (1 + payoff) * 100 if payoff > 0 else 50

    # Segmentazione
    by_strategy: dict[str, list] = {}
    for t in trades:
        s = t.get("strategy", "unknown")
        by_strategy.setdefault(s, []).append(t)
    strat_summary = {
        s: {
            "trades": len(tt),
            "win_rate": round(len([x for x in tt if x["pnl_net"] > 0]) / len(tt) * 100, 1),
            "pnl_net": round(sum(x["pnl_net"] for x in tt), 2),
        }
        for s, tt in by_strategy.items()
    }

    by_dir: dict[str, list] = {}
    for t in trades:
        d = t.get("direction", "?")
        by_dir.setdefault(d, []).append(t)
    dir_summary = {
        d: {"trades": len(tt), "pnl_net": round(sum(x["pnl_net"] for x in tt), 2)}
        for d, tt in by_dir.items()
    }

    tp_count = sum(1 for t in trades if t["reason"] == "TP")
    sl_count = sum(1 for t in trades if t["reason"] == "SL")

    return {
        "trades": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate": round(win_rate, 1), "profit_factor": round(profit_factor, 3),
        "payoff_ratio": round(payoff, 3), "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4), "pnl_net": round(pnl_net, 2),
        "pnl_pct": round(pnl_net / init_capital * 100, 3),
        "max_dd": round(max_dd, 2), "total_fees": round(total_fees, 2),
        "break_even_wr": round(be_wr, 1),
        "tp_exits": tp_count, "sl_exits": sl_count,
        "by_strategy": strat_summary, "by_direction": dir_summary,
    }


def print_report(label: str, metrics: dict, config_snapshot: dict) -> None:
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"  TP={config_snapshot.get('tp_ratio')}  SL_mult={config_snapshot.get('sl_atr_mult')}  "
          f"score>={config_snapshot.get('min_entry_score')}")
    print(f"{'-'*60}")
    m = metrics
    pnl_sign = "+" if m["pnl_net"] >= 0 else ""
    print(f"  Trade: {m['trades']}  |  Win: {m['wins']} ({m['win_rate']}%)  |  "
          f"PF: {m['profit_factor']}  |  Payoff: {m['payoff_ratio']}")
    print(f"  P&L: {pnl_sign}{m['pnl_net']} USDT ({pnl_sign}{m['pnl_pct']}%)  |  "
          f"MaxDD: {m['max_dd']}%  |  Fees: {m['total_fees']} USDT")
    print(f"  TP/SL exits: {m['tp_exits']}/{m['sl_exits']}  |  "
          f"Break-even WR: {m['break_even_wr']}%")
    if m["by_strategy"]:
        print(f"  Strategie: " + "  ".join(
            f"{s}: {v['trades']}t {v['win_rate']}% {'+' if v['pnl_net']>=0 else ''}{v['pnl_net']}"
            for s, v in m["by_strategy"].items()
        ))
    if m["by_direction"]:
        print(f"  Direzioni: " + "  ".join(
            f"{d}: {v['trades']}t {'+' if v['pnl_net']>=0 else ''}{v['pnl_net']}"
            for d, v in m["by_direction"].items()
        ))
    print(f"{'='*60}")


# ─────────────────────────────────────────────────────────────────────────────
# Sweep parametri
# ─────────────────────────────────────────────────────────────────────────────

def run_sweep(candles_1m: list, candles_5m: list, capital: float = 1000.0) -> None:
    tp_values = [2.0, 2.5, 3.0, 3.5, 4.0]
    sl_values = [1.0, 1.2, 1.5, 2.0, 2.5]
    score_values = [65, 75, 80]

    results = []
    total = len(tp_values) * len(sl_values) * len(score_values)
    done = 0
    print(f"\nSweep {total} combinazioni...")

    for tp in tp_values:
        for sl in sl_values:
            for sc in score_values:
                bot.CONFIG["tp_ratio"] = tp
                bot.CONFIG["sl_atr_mult"] = sl
                bot.CONFIG["min_entry_score"] = sc
                result = run_backtest(candles_1m, candles_5m, capital)
                m = compute_metrics(result, capital)
                results.append({
                    "tp_ratio": tp, "sl_atr_mult": sl, "min_entry_score": sc,
                    "trades": m["trades"], "win_rate": m["win_rate"],
                    "profit_factor": m["profit_factor"], "pnl_net": m["pnl_net"],
                    "pnl_pct": m["pnl_pct"], "max_dd": m["max_dd"],
                })
                done += 1
                if done % 10 == 0:
                    print(f"  {done}/{total}...")

    results.sort(key=lambda x: x["profit_factor"], reverse=True)
    print(f"\n{'='*80}")
    print(f"  TOP 10 combinazioni (ordinate per Profit Factor)")
    print(f"{'-'*80}")
    print(f"  {'TP':>5} {'SL':>5} {'Sc':>4} | {'Trade':>6} {'WR%':>6} {'PF':>6} {'P&L':>8} {'DD%':>6}")
    print(f"{'-'*80}")
    for r in results[:10]:
        print(f"  {r['tp_ratio']:>5.1f} {r['sl_atr_mult']:>5.1f} {r['min_entry_score']:>4} | "
              f"{r['trades']:>6} {r['win_rate']:>6.1f} {r['profit_factor']:>6.3f} "
              f"{r['pnl_net']:>+8.2f} {r['max_dd']:>6.1f}%")
    print(f"{'='*80}")
    best = results[0]
    print(f"\n  BEST: TP={best['tp_ratio']} SL={best['sl_atr_mult']} score={best['min_entry_score']}")
    print(f"  Aggiorna .env: TP_RATIO={best['tp_ratio']}  SL_ATR_MULT={best['sl_atr_mult']}  MIN_ENTRY_SCORE={best['min_entry_score']}")

    # Salva sweep completo
    sweep_path = LOG_DIR / "backtest_sweep.json"
    sweep_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  Sweep completo salvato: {sweep_path.name}")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest paper scalping bot")
    parser.add_argument("--symbol", default=bot.CONFIG["symbol"])
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--capital", type=float, default=1000.0)
    parser.add_argument("--warmup", type=int, default=60,
                        help="Candele 1m di warmup prima di tradare (default: 60)")
    parser.add_argument("--baseline", action="store_true",
                        help="Confronto: esegue anche run con config 'old' (TP=2.0, SL=1.2)")
    parser.add_argument("--sweep", action="store_true",
                        help="Grid search TP × SL × min_entry_score")
    parser.add_argument("--strategy", default=None,
                        help="Forza una strategia (disabilita auto selector)")
    parser.add_argument("--fetch", action="store_true",
                        help="Re-scarica klines anche se la cache è fresca")
    args = parser.parse_args()

    # Fetch/verifica cache
    candles_1m = load_cache(args.symbol, "1m")
    candles_5m = load_cache(args.symbol, "5m")
    if not candles_1m or not candles_5m or args.fetch:
        print("Cache assente o --fetch richiesto. Scarico klines...")
        asyncio.run(fetch_main(args.symbol, args.days, force=args.fetch))
        candles_1m = load_cache(args.symbol, "1m")
        candles_5m = load_cache(args.symbol, "5m")

    if not candles_1m:
        print("ERRORE: nessuna candela 1m disponibile. Esegui: python tools/fetch_klines.py")
        sys.exit(1)

    from_dt = datetime.fromtimestamp(candles_1m[0]["t"] / 1000).strftime("%Y-%m-%d")
    to_dt = datetime.fromtimestamp(candles_1m[-1]["T"] / 1000).strftime("%Y-%m-%d")
    print(f"\nDati: {args.symbol} — {len(candles_1m)} candele 1m, {len(candles_5m)} candele 5m")
    print(f"Periodo: {from_dt} -> {to_dt}   Capitale: {args.capital} USDT")

    # Forzatura strategia
    if args.strategy:
        bot.CONFIG["auto_strategy_selector"] = False
        bot.CONFIG["strategy"] = args.strategy
        print(f"Strategia forzata: {args.strategy}")

    if args.sweep:
        run_sweep(candles_1m, candles_5m, args.capital)
        return

    # ── Run corrente ──────────────────────────────────────────────────────────
    config_snap = {k: bot.CONFIG[k] for k in ("tp_ratio", "sl_atr_mult", "min_entry_score",
                                                "auto_strategy_selector", "strategy")}
    result = run_backtest(candles_1m, candles_5m, args.capital, args.warmup)
    metrics = compute_metrics(result, args.capital)
    print_report(f"CONFIG ATTUALE — {args.symbol}", metrics, config_snap)

    # ── Baseline opzionale ────────────────────────────────────────────────────
    if args.baseline:
        saved = deepcopy(bot.CONFIG)
        bot.CONFIG.update({"tp_ratio": 2.0, "sl_atr_mult": 1.2, "min_entry_score": 75})
        result_bl = run_backtest(candles_1m, candles_5m, args.capital, args.warmup)
        metrics_bl = compute_metrics(result_bl, args.capital)
        print_report("BASELINE (vecchia config TP=2.0, SL=1.2)", metrics_bl,
                     {"tp_ratio": 2.0, "sl_atr_mult": 1.2, "min_entry_score": 75})
        bot.CONFIG.update(saved)

    # ── Salva trade su file ───────────────────────────────────────────────────
    output = {
        "symbol": args.symbol, "capital": args.capital,
        "config": config_snap, "metrics": metrics,
        "trades": result["trades"], "equity": result["equity"],
        "saved_at": datetime.now().isoformat(),
    }
    out_path = LOG_DIR / "backtest_trades.json"
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n  Trade salvati: {out_path.name} ({len(result['trades'])} trade)")
    print("  Puoi analizzarli con il subagente strategy-analyst.")


if __name__ == "__main__":
    main()
