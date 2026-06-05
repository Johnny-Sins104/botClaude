"""
Patch bot.py with:
- post-SL same-direction cooldown
- monitor-only logging while a position is open
- anti-late Ichimoku entry filter

Run from repository root:
    python tools/apply_post_sl_cooldown_patch.py
    python -m py_compile bot.py

This does not touch .env and does not enable live trading.
Strategies remain only: ema, bb, macd, ichi.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "bot.py"
text = BOT.read_text(encoding="utf-8")
original = text


def patch(old: str, new: str, label: str) -> None:
    global text
    if new in text:
        print(f"[SKIP] {label}")
        return
    if old not in text:
        raise SystemExit(f"[FAIL] pattern not found: {label}")
    text = text.replace(old, new, 1)
    print(f"[OK] {label}")


patch(
    '    "auto_strategy_fallback": _env_str("AUTO_STRATEGY_FALLBACK", _env_str("STRATEGY", "ema")).lower(),\n}',
    '    "auto_strategy_fallback": _env_str("AUTO_STRATEGY_FALLBACK", _env_str("STRATEGY", "ema")).lower(),\n    "post_sl_cooldown_candles": _env_int("POST_SL_COOLDOWN_CANDLES", 5),\n}',
    "CONFIG post_sl_cooldown_candles",
)

patch(
    '    "auto_strategy_min_score": int, "auto_strategy_cooldown_candles": int, "auto_strategy_fallback": str,\n}',
    '    "auto_strategy_min_score": int, "auto_strategy_cooldown_candles": int, "auto_strategy_fallback": str,\n    "post_sl_cooldown_candles": int,\n}',
    "CONFIG_SCHEMA post_sl_cooldown_candles",
)

patch(
    '    if not 0 <= candidate["auto_strategy_cooldown_candles"] <= 20:\n        raise ValueError("auto_strategy_cooldown_candles deve essere tra 0 e 20")\n',
    '    if not 0 <= candidate["auto_strategy_cooldown_candles"] <= 20:\n        raise ValueError("auto_strategy_cooldown_candles deve essere tra 0 e 20")\n    if not 0 <= candidate["post_sl_cooldown_candles"] <= 60:\n        raise ValueError("post_sl_cooldown_candles deve essere tra 0 e 60")\n',
    "validation post_sl_cooldown_candles",
)

patch(
    '"AUTO_STRATEGY_FALLBACK": CONFIG["auto_strategy_fallback"]}',
    '"AUTO_STRATEGY_FALLBACK": CONFIG["auto_strategy_fallback"], "POST_SL_COOLDOWN_CANDLES": CONFIG["post_sl_cooldown_candles"]}',
    "strategy params expose cooldown",
)

patch(
    '"shutdown_requested": False, "kill_switch_active": False,',
    '"shutdown_requested": False, "entry_cooldowns": {"buy": 0, "sell": 0}, "last_sl_direction": None, "last_sl_time": None, "kill_switch_active": False,',
    "state cooldown fields",
)

patch(
    '"last_closed_candle_time": None, "open_position": None,',
    '"last_closed_candle_time": None, "last_monitor_log_candle_time": None, "open_position": None,',
    "state monitor field",
)

patch(
    '\ndef _entry_block_reason() -> Optional[str]:\n',
    '\ndef _tick_entry_cooldowns() -> None:\n    for side in ("buy", "sell"):\n        state["entry_cooldowns"][side] = max(0, int(state["entry_cooldowns"].get(side, 0)) - 1)\n\n\ndef _entry_block_reason() -> Optional[str]:\n',
    "cooldown tick helper",
)

patch(
    '    if signal_type == "sell" and not CONFIG["allow_short"]:\n        return "short_entries_disabled"\n',
    '    if signal_type in {"buy", "sell"} and state.get("open_position"):\n        return "position_already_open"\n    if signal_type in {"buy", "sell"} and int(state["entry_cooldowns"].get(signal_type, 0)) > 0:\n        return f"post_sl_cooldown_{signal_type}_{state[\'entry_cooldowns\'][signal_type]}_candles"\n    if signal_type == "sell" and not CONFIG["allow_short"]:\n        return "short_entries_disabled"\n',
    "entry block position/cooldown",
)

patch(
    '"entry_block_reason": block_reason})',
    '"entry_block_reason": block_reason, "entry_cooldowns": deepcopy(state.get("entry_cooldowns", {}))})',
    "signal exposes cooldowns",
)

helper = '''\n\ndef _recent_rebound_risk(direction: str) -> tuple[bool, str]:\n    candles = list(closed_candles)[-3:]\n    if len(candles) < 3:\n        return False, "not_enough_recent_candles"\n    closes = [c["c"] for c in candles]\n    rising = closes[0] < closes[1] < closes[2]\n    falling = closes[0] > closes[1] > closes[2]\n    snap = _macd_snapshot(list(closed_prices))\n    if direction == "sell":\n        if rising:\n            return True, "last_3_closes_rebounding_against_short"\n        if snap and snap["hist_cur"] > snap["hist_prev"]:\n            return True, "macd_histogram_improving_against_short"\n    if direction == "buy":\n        if falling:\n            return True, "last_3_closes_dumping_against_long"\n        if snap and snap["hist_cur"] < snap["hist_prev"]:\n            return True, "macd_histogram_worsening_against_long"\n    return False, "ok"\n'''

patch(
    '\ndef signal_ichi() -> dict[str, Any]:\n',
    helper + '\ndef signal_ichi() -> dict[str, Any]:\n',
    "anti-late Ichimoku helper",
)

patch(
    '    if buy >= 2:\n        return {"type": "buy", "score": buy * 30, "detail": "Ichimoku bullish", "conditions": cond}\n    if sell >= 2:\n        return {"type": "sell", "score": sell * 30, "detail": "Ichimoku bearish", "conditions": cond}\n',
    '    if sell >= 2:\n        risk, why = _recent_rebound_risk("sell")\n        if risk:\n            cond.append({"name": "Anti-late short", "ok_buy": False, "ok_sell": False, "value": why})\n            return {"type": "wait", "score": 0, "detail": f"Ichimoku bearish ma entry short in ritardo: {why}", "conditions": cond}\n        return {"type": "sell", "score": sell * 30, "detail": "Ichimoku bearish", "conditions": cond}\n    if buy >= 2:\n        risk, why = _recent_rebound_risk("buy")\n        if risk:\n            cond.append({"name": "Anti-late long", "ok_buy": False, "ok_sell": False, "value": why})\n            return {"type": "wait", "score": 0, "detail": f"Ichimoku bullish ma entry long in ritardo: {why}", "conditions": cond}\n        return {"type": "buy", "score": buy * 30, "detail": "Ichimoku bullish", "conditions": cond}\n',
    "anti-late Ichimoku decision",
)

patch(
    '    pnl = _position_pnl(pos, signal_price); state["capital"] += pnl["pnl_net"]\n    m = state["metrics"];',
    '    pnl = _position_pnl(pos, signal_price); state["capital"] += pnl["pnl_net"]\n    if reason == "SL":\n        side = "buy" if pos["dir"] == 1 else "sell"\n        state["entry_cooldowns"][side] = CONFIG["post_sl_cooldown_candles"]\n        state["last_sl_direction"] = pos["direction"]\n        state["last_sl_time"] = _now_iso()\n    m = state["metrics"];',
    "post-SL cooldown set",
)

patch(
    '"strategy_scores": state["strategy_scores"], "capital": state["capital"],',
    '"strategy_scores": state["strategy_scores"], "entry_cooldowns": state["entry_cooldowns"], "last_sl_direction": state["last_sl_direction"], "last_sl_time": state["last_sl_time"], "capital": state["capital"],',
    "persist cooldowns",
)

patch(
    'state["strategy_scores"] = data.get("strategy_scores", state["strategy_scores"])\n        saved = data.get("open_position")',
    'state["strategy_scores"] = data.get("strategy_scores", state["strategy_scores"]); state["entry_cooldowns"].update(data.get("entry_cooldowns", {})); state["last_sl_direction"] = data.get("last_sl_direction"); state["last_sl_time"] = data.get("last_sl_time")\n        saved = data.get("open_position")',
    "load cooldowns",
)

old_handle = '''async def _handle_closed_1m_candle(k: dict[str, Any]) -> None:\n    candle = {"t": k.get("t"), "T": k.get("T"), "o": float(k["o"]), "h": float(k["h"]), "l": float(k["l"]), "c": float(k["c"]), "v": float(k["v"])}\n    closed_candles.append(candle); closed_prices.append(candle["c"]); closed_volumes.append(candle["v"]); state["candles"] = list(closed_candles); state["last_closed_candle_time"] = candle["T"]\n    if state["last_signal_candle_time"] == candle["T"]:\n        return\n    raw = get_signal(candle["T"], candle["c"]); dec = _decorate_signal(raw, candle["T"], candle["c"]); state["signal"] = dec; state["last_signal_candle_time"] = candle["T"]\n    _add_log("signal", {"direction": dec["type"].upper(), "entry": candle["c"], "strategy": dec.get("strategy")}, dec.get("score", 0), dec.get("entry_block_reason") or dec.get("detail", ""))\n    if dec["entry_allowed"]:\n        await open_position(dec, candle["c"], candle["T"])\n    _save_paper_state()\n'''

new_handle = '''async def _handle_closed_1m_candle(k: dict[str, Any]) -> None:\n    candle = {"t": k.get("t"), "T": k.get("T"), "o": float(k["o"]), "h": float(k["h"]), "l": float(k["l"]), "c": float(k["c"]), "v": float(k["v"])}\n    closed_candles.append(candle); closed_prices.append(candle["c"]); closed_volumes.append(candle["v"]); state["candles"] = list(closed_candles); state["last_closed_candle_time"] = candle["T"]\n    if state["last_signal_candle_time"] == candle["T"]:\n        return\n    _tick_entry_cooldowns()\n    raw = get_signal(candle["T"], candle["c"]); dec = _decorate_signal(raw, candle["T"], candle["c"]); state["last_signal_candle_time"] = candle["T"]\n    if state["open_position"]:\n        dec["entry_allowed"] = False\n        dec["entry_block_reason"] = "position_already_open"\n        dec["detail"] = f"{dec.get('detail', '')} | monitor only: posizione aperta"\n        state["signal"] = dec\n        minute = datetime.fromtimestamp(candle["T"] / 1000).minute\n        if minute % 5 == 0 and state.get("last_monitor_log_candle_time") != candle["T"]:\n            pos = state["open_position"]\n            _add_log("monitor", {"direction": pos.get("direction", "OPEN"), "entry": candle["c"], "strategy": pos.get("strategy", state.get("active_strategy"))}, pos.get("unrealized", 0.0), "position_already_open")\n            state["last_monitor_log_candle_time"] = candle["T"]\n        _save_paper_state()\n        return\n    state["signal"] = dec\n    if dec.get("type") in {"buy", "sell"} or dec.get("entry_block_reason"):\n        _add_log("signal", {"direction": dec["type"].upper(), "entry": candle["c"], "strategy": dec.get("strategy")}, dec.get("score", 0), dec.get("entry_block_reason") or dec.get("detail", ""))\n    if dec["entry_allowed"]:\n        await open_position(dec, candle["c"], candle["T"])\n    _save_paper_state()\n'''
patch(old_handle, new_handle, "monitor-only closed-candle handler")

patch(
    '"max_notional_pct", "allow_short"}',
    '"max_notional_pct", "allow_short", "post_sl_cooldown_candles"}',
    "block cooldown config while position open",
)

if text == original:
    raise SystemExit("[FAIL] no changes applied")

BOT.write_text(text, encoding="utf-8")
print("[DONE] bot.py patched")
