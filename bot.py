"""
Paper-only Binance scalping bot with FastAPI dashboard.

This build cannot place live exchange orders. It uses Binance public market data
for paper trading and dashboard updates only.

Strategies remain limited to: ema, bb, macd, ichi.
AUTO_STRATEGY_SELECTOR is only a regime selector that chooses between those four.
"""

import asyncio
import json
import os
import urllib.parse
import urllib.request
from collections import deque
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import uvicorn
from binance import AsyncClient, BinanceSocketManager
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from loguru import logger

BOT_DIR = Path(__file__).parent.resolve()
LOG_DIR = BOT_DIR / "logs"
TRADES_FILE = LOG_DIR / "trades.json"
STATE_FILE = LOG_DIR / "state.json"
LOG_DIR.mkdir(exist_ok=True)
load_dotenv()

LIVE_DISABLED_MESSAGE = "LIVE TRADING DISABLED: this build is paper-only"
ALLOWED_STRATEGIES = {"ema", "bb", "macd", "ichi"}


def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "y", "on"}


PAPER_MODE = _env_bool("PAPER_MODE", "true")
if not PAPER_MODE:
    raise RuntimeError(LIVE_DISABLED_MESSAGE)


def _env_float(name: str, default: float, aliases: tuple[str, ...] = ()) -> float:
    for key in (name, *aliases):
        raw = os.getenv(key)
        if raw not in (None, ""):
            try:
                return float(raw)
            except ValueError:
                logger.warning(f"Valore non valido per {key}; uso default sicuro")
                return default
    return default


def _env_int(name: str, default: int, aliases: tuple[str, ...] = ()) -> int:
    for key in (name, *aliases):
        raw = os.getenv(key)
        if raw not in (None, ""):
            try:
                return int(raw)
            except ValueError:
                logger.warning(f"Valore non valido per {key}; uso default sicuro")
                return default
    return default


def _env_str(name: str, default: str, aliases: tuple[str, ...] = ()) -> str:
    for key in (name, *aliases):
        raw = os.getenv(key)
        if raw not in (None, ""):
            return raw.strip()
    return default


HOST = _env_str("HOST", "127.0.0.1")
PORT = _env_int("PORT", 8000)
MAX_CANDLES = 300
MIN_NOTIONAL_USDT = 5.0
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

CONFIG: dict[str, Any] = {
    "symbol": _env_str("SYMBOL", "BTCUSDT").upper(),
    "strategy": _env_str("STRATEGY", "ema").lower(),
    "capital": _env_float("CAPITAL", 1000.0),
    "risk_pct": _env_float("RISK_PCT", 0.3),
    "tp_ratio": _env_float("TP_RATIO", 2.0),
    "sl_atr_mult": _env_float("SL_ATR_MULT", 1.2),
    "ema_fast": _env_int("EMA_FAST", 9),
    "ema_slow": _env_int("EMA_SLOW", 21),
    "rsi_period": _env_int("RSI_PERIOD", 14),
    "bb_period": _env_int("BB_PERIOD", 20),
    "bb_dev": _env_float("BB_DEV", 2.0, aliases=("BB_MULT",)),
    "macd_fast": _env_int("MACD_FAST", 12),
    "macd_slow": _env_int("MACD_SLOW", 26),
    "macd_sig": _env_int("MACD_SIG", 9, aliases=("MACD_SIGNAL",)),
    "ichi_t": _env_int("ICHI_T", 9, aliases=("ICHI_TENKAN",)),
    "ichi_k": _env_int("ICHI_K", 26, aliases=("ICHI_KIJUN",)),
    "ichi_s": _env_int("ICHI_S", 52, aliases=("ICHI_SENKOU_B",)),
    "fee_pct": _env_float("FEE_PCT", 0.001),
    "slippage_pct": _env_float("SLIPPAGE_PCT", 0.0002),
    "max_notional_pct": _env_float("MAX_NOTIONAL_PCT", 0.35),
    "allow_short": _env_bool("ALLOW_SHORT", "false"),
    "min_entry_score": _env_int("MIN_ENTRY_SCORE", 75),
    "daily_loss_limit_pct": _env_float("DAILY_LOSS_LIMIT_PCT", 3.0),
    "daily_loss_limit_enabled": _env_bool("DAILY_LOSS_LIMIT_ENABLED", "true"),
    "reconnect_initial_delay_sec": _env_float("RECONNECT_INITIAL_DELAY_SEC", 2.0),
    "reconnect_max_delay_sec": _env_float("RECONNECT_MAX_DELAY_SEC", 60.0),
    "trailing_stop": _env_bool("TRAILING_STOP", "false"),
    "whipsaw_filter": _env_bool("WHIPSAW_FILTER", "true"),
    "mtf_filter": _env_bool("MTF_FILTER", "true"),
    "htf_trend_filter": _env_bool("HTF_TREND_FILTER", "true"),
    "htf_trend_fast": _env_int("HTF_TREND_FAST", 50),
    "htf_trend_slow": _env_int("HTF_TREND_SLOW", 100),
    "htf_trend_min_gap_pct": _env_float("HTF_TREND_MIN_GAP_PCT", 0.02),
    "auto_strategy_selector": _env_bool("AUTO_STRATEGY_SELECTOR", "true"),
    "auto_strategy_min_score": _env_int("AUTO_STRATEGY_MIN_SCORE", 60),
    "auto_strategy_cooldown_candles": _env_int("AUTO_STRATEGY_COOLDOWN_CANDLES", 3),
    "auto_strategy_fallback": _env_str("AUTO_STRATEGY_FALLBACK", _env_str("STRATEGY", "ema")).lower(),
    "post_sl_cooldown_candles": _env_int("POST_SL_COOLDOWN_CANDLES", 5),
    "min_capital_alert_pct": _env_float("MIN_CAPITAL_ALERT_PCT", 10.0),
}

CONFIG_SCHEMA: dict[str, type] = {
    "strategy": str, "capital": float, "risk_pct": float, "tp_ratio": float, "sl_atr_mult": float,
    "ema_fast": int, "ema_slow": int, "rsi_period": int, "bb_period": int, "bb_dev": float,
    "macd_fast": int, "macd_slow": int, "macd_sig": int, "ichi_t": int, "ichi_k": int, "ichi_s": int,
    "fee_pct": float, "slippage_pct": float, "max_notional_pct": float, "allow_short": bool,
    "min_entry_score": int, "daily_loss_limit_pct": float, "daily_loss_limit_enabled": bool,
    "reconnect_initial_delay_sec": float, "reconnect_max_delay_sec": float, "trailing_stop": bool,
    "whipsaw_filter": bool, "mtf_filter": bool, "htf_trend_filter": bool,
    "htf_trend_fast": int, "htf_trend_slow": int, "htf_trend_min_gap_pct": float,
    "auto_strategy_selector": bool,
    "auto_strategy_min_score": int, "auto_strategy_cooldown_candles": int, "auto_strategy_fallback": str,
    "post_sl_cooldown_candles": int,
    "min_capital_alert_pct": float,
}
CONFIG_KEY_ALIASES = {"bb_mult": "bb_dev", "macd_signal": "macd_sig", "ichi_tenkan": "ichi_t", "ichi_kijun": "ichi_k", "ichi_senkou_b": "ichi_s"}


def _now_iso() -> str:
    return datetime.now().isoformat()


def _today_str() -> str:
    return datetime.now().date().isoformat()


def _coerce_value(key: str, value: Any) -> Any:
    target = CONFIG_SCHEMA[key]
    if target is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
            return value.strip().lower() in {"true", "1", "yes", "on"}
        raise ValueError(f"{key} deve essere booleano")
    if target is int:
        if isinstance(value, bool):
            raise ValueError(f"{key} deve essere intero")
        coerced = int(value)
        if float(value) != coerced:
            raise ValueError(f"{key} deve essere intero")
        return coerced
    if target is float:
        if isinstance(value, bool):
            raise ValueError(f"{key} deve essere numerico")
        return float(value)
    if target is str:
        if not isinstance(value, str):
            raise ValueError(f"{key} deve essere stringa")
        return value.strip().lower()
    return value


def _validate_config(candidate: dict[str, Any]) -> None:
    if candidate["strategy"] not in ALLOWED_STRATEGIES:
        raise ValueError("strategy consentite: ema, bb, macd, ichi")
    if candidate["auto_strategy_fallback"] not in ALLOWED_STRATEGIES:
        raise ValueError("auto_strategy_fallback consentite: ema, bb, macd, ichi")
    if not 0.1 <= candidate["risk_pct"] <= 2.0:
        raise ValueError("risk_pct deve essere tra 0.1 e 2.0")
    if not 10 <= candidate["capital"] <= 1_000_000:
        raise ValueError("capital deve essere tra 10 e 1_000_000")
    if not 0.5 <= candidate["tp_ratio"] <= 10:
        raise ValueError("tp_ratio deve essere tra 0.5 e 10")
    if not 0.2 <= candidate["sl_atr_mult"] <= 10:
        raise ValueError("sl_atr_mult deve essere tra 0.2 e 10")
    if not (candidate["ema_fast"] >= 2 and candidate["ema_fast"] < candidate["ema_slow"] and candidate["ema_slow"] <= 300):
        raise ValueError("ema_fast deve essere >=2, minore di ema_slow, ema_slow <=300")
    if not 2 <= candidate["rsi_period"] <= 100:
        raise ValueError("rsi_period deve essere tra 2 e 100")
    if not 5 <= candidate["bb_period"] <= 300:
        raise ValueError("bb_period deve essere tra 5 e 300")
    if not 0.5 <= candidate["bb_dev"] <= 5:
        raise ValueError("bb_dev deve essere tra 0.5 e 5")
    if not candidate["macd_fast"] >= 2 or not candidate["macd_slow"] > candidate["macd_fast"]:
        raise ValueError("macd_fast >=2 e macd_slow > macd_fast")
    if not 2 <= candidate["macd_sig"] <= 100:
        raise ValueError("macd_sig deve essere tra 2 e 100")
    if not candidate["ichi_t"] < candidate["ichi_k"] < candidate["ichi_s"]:
        raise ValueError("parametri Ichimoku non coerenti: tenkan < kijun < senkou_b")
    if not 0 <= candidate["fee_pct"] <= 0.01 or not 0 <= candidate["slippage_pct"] <= 0.01:
        raise ValueError("fee_pct/slippage_pct devono essere tra 0 e 0.01")
    if not 0.01 <= candidate["max_notional_pct"] <= 1.0:
        raise ValueError("max_notional_pct deve essere tra 0.01 e 1.0")
    if not 60 <= candidate["min_entry_score"] <= 100:
        raise ValueError("min_entry_score deve essere tra 60 e 100")
    if not 0 <= candidate["auto_strategy_min_score"] <= 100:
        raise ValueError("auto_strategy_min_score deve essere tra 0 e 100")
    if not 0 <= candidate["auto_strategy_cooldown_candles"] <= 20:
        raise ValueError("auto_strategy_cooldown_candles deve essere tra 0 e 20")
    if not 0 <= candidate["post_sl_cooldown_candles"] <= 60:
        raise ValueError("post_sl_cooldown_candles deve essere tra 0 e 60")
    if not 1.0 <= candidate["min_capital_alert_pct"] <= 90:
        raise ValueError("min_capital_alert_pct deve essere tra 1.0 e 90")
    if not 2 <= candidate["htf_trend_fast"] < candidate["htf_trend_slow"] <= 300:
        raise ValueError("htf_trend_fast deve essere >=2, minore di htf_trend_slow, htf_trend_slow <=300")
    if not 0 <= candidate["htf_trend_min_gap_pct"] <= 2:
        raise ValueError("htf_trend_min_gap_pct deve essere tra 0 e 2")
    if not 0.1 <= candidate["daily_loss_limit_pct"] <= 50:
        raise ValueError("daily_loss_limit_pct deve essere tra 0.1 e 50")
    if not 1 <= candidate["reconnect_initial_delay_sec"] <= 60 or not 2 <= candidate["reconnect_max_delay_sec"] <= 300:
        raise ValueError("reconnect delay fuori range")
    if candidate["reconnect_max_delay_sec"] < candidate["reconnect_initial_delay_sec"]:
        raise ValueError("reconnect_max_delay_sec deve essere >= reconnect_initial_delay_sec")


def _validated_config_update(body: dict[str, Any]) -> dict[str, Any]:
    normalized_body: dict[str, Any] = {}
    for key, value in body.items():
        canonical_key = CONFIG_KEY_ALIASES.get(key, key)
        if canonical_key in normalized_body:
            raise ValueError(f"Parametro duplicato dopo alias: {key}")
        normalized_body[canonical_key] = value
    unknown = sorted(set(normalized_body) - set(CONFIG_SCHEMA))
    if unknown:
        raise ValueError(f"Parametri sconosciuti: {', '.join(unknown)}")
    candidate = deepcopy(CONFIG)
    for key, value in normalized_body.items():
        candidate[key] = _coerce_value(key, value)
    _validate_config(candidate)
    return candidate


_validate_config(CONFIG)


def _public_config() -> dict[str, Any]:
    return deepcopy(CONFIG)


def _strategy_env_params() -> dict[str, Any]:
    return {"EMA_FAST": CONFIG["ema_fast"], "EMA_SLOW": CONFIG["ema_slow"], "BB_PERIOD": CONFIG["bb_period"], "BB_DEV": CONFIG["bb_dev"], "MACD_FAST": CONFIG["macd_fast"], "MACD_SLOW": CONFIG["macd_slow"], "MACD_SIG": CONFIG["macd_sig"], "ICHI_T": CONFIG["ichi_t"], "ICHI_K": CONFIG["ichi_k"], "ICHI_S": CONFIG["ichi_s"], "AUTO_STRATEGY_SELECTOR": CONFIG["auto_strategy_selector"], "AUTO_STRATEGY_MIN_SCORE": CONFIG["auto_strategy_min_score"], "AUTO_STRATEGY_COOLDOWN_CANDLES": CONFIG["auto_strategy_cooldown_candles"], "AUTO_STRATEGY_FALLBACK": CONFIG["auto_strategy_fallback"], "POST_SL_COOLDOWN_CANDLES": CONFIG["post_sl_cooldown_candles"]}


def _selector_state(active: str) -> dict[str, Any]:
    return {"auto_strategy_selector": CONFIG["auto_strategy_selector"], "active_strategy": active, "fallback_strategy": CONFIG["auto_strategy_fallback"], "market_regime": "manual" if not CONFIG["auto_strategy_selector"] else "initializing", "strategy_selector_reason": "manual_strategy" if not CONFIG["auto_strategy_selector"] else "waiting_for_historical_bootstrap", "strategy_scores": {"ema": 0, "bb": 0, "macd": 0, "ichi": 0}, "strategy_cooldown_remaining": 0, "last_strategy_switch_candle_time": None}


initial_active_strategy = CONFIG["strategy"]
state: dict[str, Any] = {
    "running": False, "paper_mode": True, "paper_only_build": True, "live_trading_enabled": False, "exchange_order_blocked": True, "paper_short_simulation": True, "spot_short_live_supported": False, "short_entries_enabled": CONFIG["allow_short"], "symbol": CONFIG["symbol"], "strategy": initial_active_strategy, "config": _public_config(), "capital": CONFIG["capital"], "init_capital": CONFIG["capital"], "price": 0.0, "last_live_price": 0.0, "price_change": 0.0, "last_price_update_time": None, "price_source": "binance_websocket_live", "signal_data_source": "closed_candles_only", "strategy_source": "manual_or_auto_selector", "strategy_params": _strategy_env_params(), "websocket_status": "stopped", "websocket_connected_at": None, "reconnect_count": 0, "reconnect_delay_sec": 0, "last_reconnect_time": None, "next_reconnect_time": None, "last_websocket_error": None, "historical_bootstrap_status": "pending", "historical_candles_loaded_1m": 0, "historical_candles_loaded_5m": 0, "historical_bootstrap_error": None, "shutdown_requested": False, "entry_cooldowns": {"buy": 0, "sell": 0}, "last_sl_direction": None, "last_sl_time": None, "kill_switch_active": False, "kill_switch_reason": None, "kill_switch_time": None, "trading_halted": False, "trading_halt_reason": None, "low_capital_alert": False, "low_capital_alert_time": None,
    "risk_guard": {"daily_date": _today_str(), "daily_start_capital": CONFIG["capital"], "daily_realized_pnl": 0.0, "daily_unrealized_pnl": 0.0, "daily_total_pnl": 0.0, "daily_loss_limit_pct": CONFIG["daily_loss_limit_pct"], "daily_loss_limit_amount": round(CONFIG["capital"] * CONFIG["daily_loss_limit_pct"] / 100, 6), "daily_loss_limit_enabled": CONFIG["daily_loss_limit_enabled"], "daily_loss_limit_hit": False, "daily_loss_limit_hit_time": None},
    "last_signal_candle_time": None, "last_signal_update_time": None, "last_signal_close_price": None, "last_closed_candle_time": None, "last_monitor_log_candle_time": None, "open_position": None, "trades": [], "metrics": {"total_pnl": 0.0, "total_pnl_pct": 0.0, "trades": 0, "wins": 0, "win_rate": 0.0, "max_dd": 0.0, "peak_capital": CONFIG["capital"]}, "indicators": {}, "signal": {"type": "wait", "score": 0, "detail": "Bot in attesa di bootstrap candele storiche", "conditions": []}, "candles": [], "equity_curve": [CONFIG["capital"]], "log": [], "errors": [], "last_update": None, "filters": {"whipsaw": False, "mtf_trend": "neutral"}, **_selector_state(initial_active_strategy),
}

closed_prices: deque[float] = deque(maxlen=MAX_CANDLES)
closed_volumes: deque[float] = deque(maxlen=MAX_CANDLES)
closed_candles: deque[dict[str, Any]] = deque(maxlen=MAX_CANDLES)
closed_prices_5m: deque[float] = deque(maxlen=200)
closed_candles_5m: deque[dict[str, Any]] = deque(maxlen=200)
client: Optional[AsyncClient] = None
websocket_task: Optional[asyncio.Task] = None


def _append_error(msg: str) -> None:
    state["errors"].append({"time": _now_iso(), "msg": msg})
    state["errors"] = state["errors"][-50:]


def _add_log(action: str, pos: dict[str, Any], pnl: float = 0.0, reason: str = "") -> None:
    state["log"].insert(0, {"time": datetime.now().strftime("%H:%M:%S"), "action": action, "direction": pos.get("direction", pos.get("type", "SYSTEM")), "price": pos.get("entry", pos.get("exit", pos.get("price", 0))), "pnl": round(pnl, 4), "reason": reason, "strategy": pos.get("strategy", state.get("active_strategy"))})
    state["log"] = state["log"][:100]


def _sync_trading_halt_state() -> None:
    guard = state["risk_guard"]
    if state["kill_switch_active"]:
        state["trading_halted"] = True
        state["trading_halt_reason"] = state["kill_switch_reason"] or "kill_switch"
    elif guard.get("daily_loss_limit_hit"):
        state["trading_halted"] = True
        state["trading_halt_reason"] = "daily_loss_limit"
    else:
        state["trading_halted"] = False
        state["trading_halt_reason"] = None


def _reset_daily_guard(today: Optional[str] = None) -> None:
    start = float(state["capital"])
    state["risk_guard"].update({"daily_date": today or _today_str(), "daily_start_capital": start, "daily_realized_pnl": 0.0, "daily_unrealized_pnl": 0.0, "daily_total_pnl": 0.0, "daily_loss_limit_pct": CONFIG["daily_loss_limit_pct"], "daily_loss_limit_amount": round(start * CONFIG["daily_loss_limit_pct"] / 100, 6), "daily_loss_limit_enabled": CONFIG["daily_loss_limit_enabled"], "daily_loss_limit_hit": False, "daily_loss_limit_hit_time": None})
    _sync_trading_halt_state()


async def _notify_telegram(message: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": message}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=5))
    except Exception as exc:
        logger.debug(f"Telegram notify: {exc}")


def _tg(message: str) -> None:
    """Fire-and-forget Telegram notification. Silente se credenziali assenti o no event loop."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        asyncio.get_running_loop().create_task(_notify_telegram(message))
    except RuntimeError:
        pass


def _check_capital_alert() -> None:
    threshold = state["init_capital"] * (1 - CONFIG["min_capital_alert_pct"] / 100)
    was_alert = bool(state.get("low_capital_alert"))
    if state["capital"] < threshold:
        if not was_alert:
            state["low_capital_alert"] = True
            state["low_capital_alert_time"] = _now_iso()
            pct_drop = (state["init_capital"] - state["capital"]) / state["init_capital"] * 100
            _add_log("alert", {"direction": "SYSTEM", "entry": 0, "strategy": ""}, 0.0, "LOW_CAPITAL")
            logger.warning(f"[ALERT] Capitale basso: {state['capital']:.2f} USDT (-{pct_drop:.1f}%)")
            _tg(f"[ALERT] Capitale basso: {state['capital']:.2f} USDT (-{pct_drop:.1f}%)")
    elif was_alert:
        state["low_capital_alert"] = False


def _refresh_risk_guard() -> bool:
    guard = state["risk_guard"]
    if guard.get("daily_date") != _today_str():
        _reset_daily_guard()
        return True
    start = float(guard.get("daily_start_capital") or state["capital"])
    unrealized = float(state["open_position"].get("unrealized", 0.0) or 0.0) if state["open_position"] else 0.0
    realized = float(state["capital"]) - start
    total = realized + unrealized
    limit = start * CONFIG["daily_loss_limit_pct"] / 100
    guard.update({"daily_realized_pnl": round(realized, 6), "daily_unrealized_pnl": round(unrealized, 6), "daily_total_pnl": round(total, 6), "daily_loss_limit_pct": CONFIG["daily_loss_limit_pct"], "daily_loss_limit_amount": round(limit, 6), "daily_loss_limit_enabled": CONFIG["daily_loss_limit_enabled"]})
    if CONFIG["daily_loss_limit_enabled"] and not guard.get("daily_loss_limit_hit") and total <= -limit:
        guard["daily_loss_limit_hit"] = True
        guard["daily_loss_limit_hit_time"] = _now_iso()
        _add_log("halt", {"direction": "SYSTEM", "entry": 0}, total, "DAILY_LOSS")
        _tg(f"[HALT] Daily loss limit raggiunto: P&L giornaliero {total:+.2f} USDT")
    _sync_trading_halt_state()
    _check_capital_alert()
    return True


def _sync_state_config() -> None:
    state["symbol"] = CONFIG["symbol"]
    state["short_entries_enabled"] = CONFIG["allow_short"]
    state["config"] = _public_config()
    state["strategy_params"] = _strategy_env_params()
    state["auto_strategy_selector"] = CONFIG["auto_strategy_selector"]
    state["fallback_strategy"] = CONFIG["auto_strategy_fallback"]
    if not CONFIG["auto_strategy_selector"]:
        state["active_strategy"] = CONFIG["strategy"]
        state["strategy"] = CONFIG["strategy"]
        state["market_regime"] = "manual"
        state["strategy_selector_reason"] = "manual_strategy"
    _refresh_risk_guard()


def _tick_entry_cooldowns() -> None:
    for side in ("buy", "sell"):
        state["entry_cooldowns"][side] = max(0, int(state["entry_cooldowns"].get(side, 0)) - 1)


def _entry_block_reason() -> Optional[str]:
    _refresh_risk_guard()
    if state["kill_switch_active"]:
        return state["kill_switch_reason"] or "kill_switch"
    if state["risk_guard"].get("daily_loss_limit_hit"):
        return "daily_loss_limit"
    return None


def _entry_block_reason_for_signal(signal_type: str, score: int | float = 0) -> Optional[str]:
    block = _entry_block_reason()
    if block:
        return block
    if signal_type in {"buy", "sell"} and state.get("open_position"):
        return "position_already_open"
    if signal_type in {"buy", "sell"} and int(state["entry_cooldowns"].get(signal_type, 0)) > 0:
        return f"post_sl_cooldown_{signal_type}_{state['entry_cooldowns'][signal_type]}_candles"
    if signal_type == "sell" and not CONFIG["allow_short"]:
        return "short_entries_disabled"
    if signal_type in {"buy", "sell"} and score < CONFIG["min_entry_score"]:
        return f"score_below_min_entry_score_{CONFIG['min_entry_score']}"
    htf_block = htf_entry_block_reason(signal_type)
    if htf_block:
        return htf_block
    return None


def _decorate_signal(signal: dict[str, Any], candle_time: Any = None, close_price: Optional[float] = None) -> dict[str, Any]:
    decorated = deepcopy(signal)
    active = state.get("active_strategy") or CONFIG["strategy"]
    score = decorated.get("score", 0) or 0
    signal_type = decorated.get("type", "wait")
    block_reason = _entry_block_reason_for_signal(signal_type, score)
    decorated.update({"strategy": active, "active_strategy": active, "fallback_strategy": CONFIG["auto_strategy_fallback"], "market_regime": state.get("market_regime"), "strategy_selector_reason": state.get("strategy_selector_reason"), "strategy_scores": state.get("strategy_scores"), "source": "closed_candles_only", "evaluated_at": _now_iso(), "candle_time": candle_time, "close_price": round(close_price, 6) if close_price is not None else None, "min_entry_score": CONFIG["min_entry_score"], "allow_short": CONFIG["allow_short"], "entry_allowed": signal_type in {"buy", "sell"} and block_reason is None, "entry_block_reason": block_reason, "entry_cooldowns": deepcopy(state.get("entry_cooldowns", {}))})
    state["last_signal_update_time"] = decorated["evaluated_at"]
    state["last_signal_close_price"] = decorated["close_price"]
    return decorated


def ema(data: list[float], period: int) -> Optional[float]:
    if len(data) < period:
        return None
    k = 2 / (period + 1)
    value = sum(data[:period]) / period
    for item in data[period:]:
        value = item * k + value * (1 - k)
    return value


def ema_values(data: list[float], period: int) -> list[float]:
    if len(data) < period:
        return []
    k = 2 / (period + 1)
    value = sum(data[:period]) / period
    values = [value]
    for item in data[period:]:
        value = item * k + value * (1 - k)
        values.append(value)
    return values


def sma(data: list[float], period: int) -> Optional[float]:
    return None if len(data) < period else sum(data[-period:]) / period


def std_dev(data: list[float], period: int) -> Optional[float]:
    if len(data) < period:
        return None
    items = data[-period:]
    mean = sum(items) / period
    return (sum((x - mean) ** 2 for x in items) / period) ** 0.5


def rsi(data: list[float], period: int) -> float:
    if len(data) < period + 1:
        return 50.0
    gains = losses = 0.0
    for i in range(len(data) - period, len(data)):
        diff = data[i] - data[i - 1]
        gains += max(diff, 0)
        losses += abs(min(diff, 0))
    rs = gains / (losses or 1e-9)
    return 100 - 100 / (1 + rs)


def vwap(prices: list[float], volumes: list[float], period: int = 30) -> float:
    ps, vs = prices[-period:], volumes[-period:]
    return sum(p * v for p, v in zip(ps, vs)) / (sum(vs) or 1e-9)


def atr_approx(price: float, period: int = 60) -> float:
    """True Range ATR (Wilder) su 60 candele 1m (≈1h).
    Period lungo stabilizza la stima: evita SL microscopici nei momenti quieti
    che vengono spazzati dal primo tick di volatilità reale.
    Floor 0.2% impedisce stop sotto la soglia di break-even vs commissioni."""
    candles = list(closed_candles)
    if len(candles) < period + 1:
        return price * 0.003
    true_ranges = []
    for i in range(len(candles) - period, len(candles)):
        high = candles[i]["h"]
        low = candles[i]["l"]
        prev_close = candles[i - 1]["c"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
    atr = sum(true_ranges) / len(true_ranges) if true_ranges else price * 0.003
    return max(atr, price * 0.002)


def atr_5m(price: float, period: int = 14) -> float:
    """ATR (Wilder) su candele 5m: misura la volatilità su scala ~70 minuti.
    Molto più stabile dell'ATR 1m: le soglie SL/TP riflettono movimenti reali
    e non il rumore tick-by-tick. Fallback su atr_approx se il buffer non è pronto."""
    candles = list(closed_candles_5m)
    if len(candles) < period + 1:
        return atr_approx(price)
    true_ranges = []
    for i in range(len(candles) - period, len(candles)):
        high = candles[i]["h"]
        low = candles[i]["l"]
        prev_close = candles[i - 1]["c"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
    atr = sum(true_ranges) / len(true_ranges) if true_ranges else price * 0.003
    return max(atr, price * 0.003)


def mtf_trend() -> str:
    if not CONFIG["mtf_filter"]:
        return "neutral"
    prices = list(closed_prices_5m)
    if len(prices) < CONFIG["ema_slow"] + 5:
        return "neutral"
    fast, slow = ema(prices, CONFIG["ema_fast"]), ema(prices, CONFIG["ema_slow"])
    if not fast or not slow:
        return "neutral"
    gap = abs(fast - slow) / slow * 100
    # Soglia abbassata da 0.03 a 0.015: il 5m resta "neutral" solo se davvero piatto.
    # Cosi il filtro direzionale blocca piu spesso gli ingressi controtendenza.
    if gap < 0.015:
        return "neutral"
    return "bull" if fast > slow else "bear"


def htf_entry_block_reason(signal_type: str) -> Optional[str]:
    """Filtro trend HTF su 5m (EMA htf_trend_fast/htf_trend_slow).
    Aggiorna state["filters"] con lo stato HTF (trend/gap/fast/slow) e ritorna il
    motivo di blocco per ingressi controtendenza. Ritorna None se il filtro e'
    disattivo, i dati 5m non bastano, oppure il segnale e' allineato al trend."""
    if not CONFIG["htf_trend_filter"]:
        return None
    prices = list(closed_prices_5m)
    slow_p = CONFIG["htf_trend_slow"]
    if len(prices) < slow_p + 5:
        return None
    fast, slow = ema(prices, CONFIG["htf_trend_fast"]), ema(prices, slow_p)
    if not fast or not slow:
        return None
    gap = abs(fast - slow) / slow * 100
    trend = "neutral" if gap < CONFIG["htf_trend_min_gap_pct"] else ("bull" if fast > slow else "bear")
    state["filters"].update({"htf_trend": trend, "htf_gap_pct": round(gap, 4), "htf_fast": round(fast, 6), "htf_slow": round(slow, 6)})
    if signal_type not in {"buy", "sell"}:
        return None
    if trend == "neutral":
        return "htf_trend_flat"
    if signal_type == "buy" and fast <= slow:
        return "htf_trend_not_aligned_buy"
    if signal_type == "sell" and fast >= slow:
        return "htf_trend_not_aligned_sell"
    return None


def _pv() -> tuple[list[float], list[float]]:
    return list(closed_prices), list(closed_volumes)


def signal_ema() -> dict[str, Any]:
    prices, volumes = _pv()
    needed = max(CONFIG["ema_slow"], CONFIG["rsi_period"] + 1, 30)
    if len(prices) < needed:
        return {"type": "wait", "score": 0, "detail": "Dati insufficienti per EMA", "conditions": []}
    fast, slow = ema(prices, CONFIG["ema_fast"]), ema(prices, CONFIG["ema_slow"])
    rr, vv, close = rsi(prices, CONFIG["rsi_period"]), vwap(prices, volumes), prices[-1]
    trend = mtf_trend()
    gap = abs(fast - slow) / slow * 100 if fast and slow else 0
    buy_ema, sell_ema = fast > slow, fast < slow
    if CONFIG["mtf_filter"] and trend == "bear":
        buy_ema = False
    if CONFIG["mtf_filter"] and trend == "bull":
        sell_ema = False
    if CONFIG["whipsaw_filter"] and gap < 0.05:
        buy_ema = sell_ema = False
    state["filters"] = {"whipsaw": CONFIG["whipsaw_filter"] and gap < 0.05, "mtf_trend": trend}
    state["indicators"] = {"ema_fast": fast, "ema_slow": slow, "vwap": vv, "rsi": rr, "ema_gap_pct": gap}
    cond = [{"name": f"EMA{CONFIG['ema_fast']} vs EMA{CONFIG['ema_slow']}", "ok_buy": buy_ema, "ok_sell": sell_ema, "value": f"{fast:.2f} {'>' if fast > slow else '<'} {slow:.2f}"}, {"name": "Prezzo close vs VWAP", "ok_buy": close > vv, "ok_sell": close < vv, "value": f"{close:.2f} {'sopra' if close > vv else 'sotto'} {vv:.2f}"}, {"name": f"RSI({CONFIG['rsi_period']})", "ok_buy": 20 <= rr <= 45, "ok_sell": 55 <= rr <= 80, "value": f"{rr:.1f}"}]
    buy = sum(1 for c in cond if c["ok_buy"]); sell = sum(1 for c in cond if c["ok_sell"])
    if buy >= 2:
        return {"type": "buy", "score": buy * 30, "detail": "EMA/VWAP/RSI bullish", "conditions": cond}
    if sell >= 2:
        return {"type": "sell", "score": sell * 30, "detail": "EMA/VWAP/RSI bearish", "conditions": cond}
    return {"type": "wait", "score": 0, "detail": "EMA senza conferma 2/3", "conditions": cond}


def signal_bb() -> dict[str, Any]:
    prices, _ = _pv(); period = CONFIG["bb_period"]
    if len(prices) < period + 2:
        return {"type": "wait", "score": 0, "detail": "Dati insufficienti per Bollinger", "conditions": []}
    mid, sd = sma(prices, period), std_dev(prices, period)
    upper, lower, close, prev = mid + CONFIG["bb_dev"] * sd, mid - CONFIG["bb_dev"] * sd, prices[-1], prices[-2]
    width = (upper - lower) / mid * 100 if mid else 0
    state["indicators"] = {"bb_upper": upper, "bb_mid": mid, "bb_lower": lower, "bb_width": width}
    cross_down, cross_up = prev >= lower and close < lower, prev <= upper and close > upper
    cond = [{"name": "Close sotto banda inferiore", "ok_buy": cross_down, "ok_sell": False, "value": f"{close:.2f} < {lower:.2f}"}, {"name": "Close sopra banda superiore", "ok_buy": False, "ok_sell": cross_up, "value": f"{close:.2f} > {upper:.2f}"}, {"name": "Larghezza bande", "ok_buy": width > 1.0, "ok_sell": width > 1.0, "value": f"{width:.2f}%"}]
    if cross_down:
        return {"type": "buy", "score": 80, "detail": "Bollinger mean reversion BUY", "conditions": cond}
    if cross_up:
        return {"type": "sell", "score": 80, "detail": "Bollinger mean reversion SELL", "conditions": cond}
    return {"type": "wait", "score": 0, "detail": "Prezzo dentro le Bollinger", "conditions": cond}


def _macd_snapshot(prices: list[float]) -> Optional[dict[str, float]]:
    if len(prices) < CONFIG["macd_slow"] + CONFIG["macd_sig"] + 3:
        return None
    fs, ss = ema_values(prices, CONFIG["macd_fast"]), ema_values(prices, CONFIG["macd_slow"])
    offset = len(fs) - len(ss)
    macd_line = [f - s for f, s in zip(fs[offset:] if offset > 0 else fs, ss)]
    sig = ema_values(macd_line, CONFIG["macd_sig"])
    if len(sig) < 2:
        return None
    m = macd_line[-len(sig):]
    return {"macd_prev": m[-2], "macd_cur": m[-1], "signal_prev": sig[-2], "signal_cur": sig[-1], "hist_prev": m[-2] - sig[-2], "hist_cur": m[-1] - sig[-1]}


def signal_macd() -> dict[str, Any]:
    prices, _ = _pv(); snap = _macd_snapshot(prices)
    if not snap:
        return {"type": "wait", "score": 0, "detail": "Dati insufficienti per MACD", "conditions": []}
    bull = snap["macd_prev"] <= snap["signal_prev"] and snap["macd_cur"] > snap["signal_cur"]
    bear = snap["macd_prev"] >= snap["signal_prev"] and snap["macd_cur"] < snap["signal_cur"]
    state["indicators"] = {"macd": snap["macd_cur"], "signal": snap["signal_cur"], "histogram": snap["hist_cur"]}
    cond = [{"name": "MACD cross bullish", "ok_buy": bull, "ok_sell": False, "value": f"{snap['macd_cur']:.4f} vs {snap['signal_cur']:.4f}"}, {"name": "MACD cross bearish", "ok_buy": False, "ok_sell": bear, "value": f"{snap['macd_cur']:.4f} vs {snap['signal_cur']:.4f}"}, {"name": "Istogramma direzione", "ok_buy": snap["hist_cur"] > snap["hist_prev"], "ok_sell": snap["hist_cur"] < snap["hist_prev"], "value": f"{snap['hist_cur']:.4f}"}]
    if bull:
        return {"type": "buy", "score": 85 if snap["macd_cur"] > 0 else 60, "detail": "MACD bullish cross", "conditions": cond}
    if bear:
        return {"type": "sell", "score": 85 if snap["macd_cur"] < 0 else 60, "detail": "MACD bearish cross", "conditions": cond}
    return {"type": "wait", "score": 0, "detail": "MACD senza cross", "conditions": cond}


def _ichimoku_values() -> Optional[dict[str, float]]:
    candles = list(closed_candles)
    if len(candles) < CONFIG["ichi_s"]:
        return None
    def mid(period: int) -> float:
        recent = candles[-period:]
        return (max(c["h"] for c in recent) + min(c["l"] for c in recent)) / 2
    tenkan, kijun = mid(CONFIG["ichi_t"]), mid(CONFIG["ichi_k"])
    return {"tenkan": tenkan, "kijun": kijun, "span_a": (tenkan + kijun) / 2, "span_b": mid(CONFIG["ichi_s"])}



def _recent_rebound_risk(direction: str) -> tuple[bool, str]:
    candles = list(closed_candles)[-3:]
    if len(candles) < 3:
        return False, "not_enough_recent_candles"
    closes = [c["c"] for c in candles]
    rising = closes[0] < closes[1] < closes[2]
    falling = closes[0] > closes[1] > closes[2]
    snap = _macd_snapshot(list(closed_prices))
    if direction == "sell":
        if rising:
            return True, "last_3_closes_rebounding_against_short"
        if snap and snap["hist_cur"] > snap["hist_prev"]:
            return True, "macd_histogram_improving_against_short"
    if direction == "buy":
        if falling:
            return True, "last_3_closes_dumping_against_long"
        if snap and snap["hist_cur"] < snap["hist_prev"]:
            return True, "macd_histogram_worsening_against_long"
    return False, "ok"

def signal_ichi() -> dict[str, Any]:
    vals = _ichimoku_values(); prices, _ = _pv()
    if not vals or not prices:
        return {"type": "wait", "score": 0, "detail": "Dati insufficienti per Ichimoku", "conditions": []}
    close = prices[-1]; top, bottom = max(vals["span_a"], vals["span_b"]), min(vals["span_a"], vals["span_b"])
    cond = [{"name": "Prezzo sopra/sotto Kumo", "ok_buy": close > top, "ok_sell": close < bottom, "value": f"{close:.2f} / cloud {bottom:.2f}-{top:.2f}"}, {"name": "Tenkan/Kijun", "ok_buy": vals["tenkan"] > vals["kijun"], "ok_sell": vals["tenkan"] < vals["kijun"], "value": f"{vals['tenkan']:.2f}/{vals['kijun']:.2f}"}, {"name": "Colore cloud", "ok_buy": vals["span_a"] > vals["span_b"], "ok_sell": vals["span_a"] < vals["span_b"], "value": f"{vals['span_a']:.2f}/{vals['span_b']:.2f}"}]
    state["indicators"] = vals
    buy = sum(1 for c in cond if c["ok_buy"]); sell = sum(1 for c in cond if c["ok_sell"])
    if sell >= 2:
        risk, why = _recent_rebound_risk("sell")
        if risk:
            cond.append({"name": "Anti-late short", "ok_buy": False, "ok_sell": False, "value": why})
            return {"type": "wait", "score": 0, "detail": f"Ichimoku bearish ma entry short in ritardo: {why}", "conditions": cond}
        return {"type": "sell", "score": sell * 30, "detail": "Ichimoku bearish", "conditions": cond}
    if buy >= 2:
        risk, why = _recent_rebound_risk("buy")
        if risk:
            cond.append({"name": "Anti-late long", "ok_buy": False, "ok_sell": False, "value": why})
            return {"type": "wait", "score": 0, "detail": f"Ichimoku bullish ma entry long in ritardo: {why}", "conditions": cond}
        return {"type": "buy", "score": buy * 30, "detail": "Ichimoku bullish", "conditions": cond}
    return {"type": "wait", "score": 0, "detail": "Ichimoku non chiaro", "conditions": cond}


def _strategy_suitability_scores() -> tuple[dict[str, int], str, str]:
    prices, volumes = _pv(); scores = {"ema": 0, "bb": 0, "macd": 0, "ichi": 0}
    if len(prices) < 30:
        return scores, "warmup", "not_enough_closed_candles"
    close = prices[-1]; fast, slow = ema(prices, CONFIG["ema_fast"]), ema(prices, CONFIG["ema_slow"]); vv = vwap(prices, volumes); rr = rsi(prices, CONFIG["rsi_period"])
    gap = abs(fast - slow) / slow * 100 if fast and slow else 0
    if fast and slow:
        align = (fast > slow and close > vv and 20 <= rr <= 55) or (fast < slow and close < vv and 45 <= rr <= 80)
        scores["ema"] = (55 if align else 25) + min(35, int(gap * 300))
    mid, sd = sma(prices, CONFIG["bb_period"]), std_dev(prices, CONFIG["bb_period"])
    if mid and sd:
        upper, lower = mid + CONFIG["bb_dev"] * sd, mid - CONFIG["bb_dev"] * sd
        near = close <= lower * 1.002 or close >= upper * 0.998
        scores["bb"] = (65 if gap < 0.08 else 35) + (25 if near else 0)
    snap = _macd_snapshot(prices)
    if snap:
        cross = (snap["macd_prev"] <= snap["signal_prev"] and snap["macd_cur"] > snap["signal_cur"]) or (snap["macd_prev"] >= snap["signal_prev"] and snap["macd_cur"] < snap["signal_cur"])
        scores["macd"] = 45 + (30 if cross else 0) + (15 if abs(snap["hist_cur"]) > abs(snap["hist_prev"]) else 0)
    vals = _ichimoku_values()
    if vals:
        top, bottom = max(vals["span_a"], vals["span_b"]), min(vals["span_a"], vals["span_b"])
        clean = (close > top and vals["tenkan"] > vals["kijun"] and vals["span_a"] > vals["span_b"]) or (close < bottom and vals["tenkan"] < vals["kijun"] and vals["span_a"] < vals["span_b"])
        scores["ichi"] = 70 if clean else 30
    best = max(scores, key=scores.get); best_score = scores[best]
    if best_score < CONFIG["auto_strategy_min_score"]:
        return scores, "unclear", f"best_{best}_{best_score}_below_min_{CONFIG['auto_strategy_min_score']}_fallback_{CONFIG['auto_strategy_fallback']}"
    regime = {"bb": "range", "macd": "momentum", "ichi": "trend_clean", "ema": "trend_moderate"}[best]
    return scores, regime, f"{regime}_{best}_score_{best_score}"


def select_active_strategy(candle_time: Any = None) -> str:
    if state["open_position"]:
        return state["open_position"].get("strategy", state.get("active_strategy", CONFIG["strategy"]))
    if not CONFIG["auto_strategy_selector"]:
        state.update({"active_strategy": CONFIG["strategy"], "strategy": CONFIG["strategy"], "market_regime": "manual", "strategy_selector_reason": "manual_strategy", "strategy_scores": {"ema": 0, "bb": 0, "macd": 0, "ichi": 0}})
        return CONFIG["strategy"]
    if state.get("strategy_cooldown_remaining", 0) > 0:
        state["strategy_cooldown_remaining"] -= 1
        state["strategy_selector_reason"] = f"cooldown_keep_{state['active_strategy']}"
        return state["active_strategy"]
    scores, regime, reason = _strategy_suitability_scores()
    chosen = max(scores, key=scores.get)
    if scores[chosen] < CONFIG["auto_strategy_min_score"]:
        chosen = CONFIG["auto_strategy_fallback"]
    if chosen != state.get("active_strategy"):
        state["last_strategy_switch_candle_time"] = candle_time
        state["strategy_cooldown_remaining"] = CONFIG["auto_strategy_cooldown_candles"]
    state.update({"active_strategy": chosen, "strategy": chosen, "market_regime": regime, "strategy_selector_reason": reason, "strategy_scores": scores})
    return chosen


def get_signal(active: str, candle_time: Any = None, close_price: Optional[float] = None) -> dict[str, Any]:
    """Calcola il segnale per la strategia `active`. Pura: non muta lo stato.
    Il chiamante deve invocare select_active_strategy() separatamente."""
    strategy_map = {"ema": signal_ema, "bb": signal_bb, "macd": signal_macd, "ichi": signal_ichi}
    try:
        signal = strategy_map.get(active, signal_ema)()
        signal["selected_strategy"] = active
        return signal
    except Exception as exc:
        logger.error(f"Errore nel calcolo segnale: {exc}")
        _append_error(f"Errore segnale: {exc}")
        return {"type": "wait", "score": 0, "detail": f"Errore: {exc}", "conditions": [], "selected_strategy": active}


def _entry_fill(price: float, direction: int) -> float:
    return price * (1 + CONFIG["slippage_pct"]) if direction == 1 else price * (1 - CONFIG["slippage_pct"])


def _exit_fill(price: float, direction: int) -> float:
    return price * (1 - CONFIG["slippage_pct"]) if direction == 1 else price * (1 + CONFIG["slippage_pct"])


def _position_pnl(pos: dict[str, Any], live_price: float) -> dict[str, float]:
    exit_fill = _exit_fill(live_price, pos["dir"])
    pnl_gross = (exit_fill - pos["entry_price_fill"]) * pos["size"] * pos["dir"]
    fee_exit = abs(exit_fill * pos["size"]) * pos["fee_pct"]
    fee_total = pos["fee_entry"] + fee_exit
    return {"exit_fill": exit_fill, "pnl_gross": pnl_gross, "fee_exit": fee_exit, "fee_total": fee_total, "pnl_net": pnl_gross - fee_total}


def _build_position_params(
    signal_type: str,
    signal_price: float,
    capital: float,
    active: str,
    candle_time: Any = None,
    market_regime: Optional[str] = None,
    strategy_selector_reason: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Calcola i parametri di sizing di una posizione — PURA, nessun side effect.
    Riusata da open_position (live) e dal backtest per parità garantita.
    Ritorna None se la posizione non è apribile (size<=0 o notional troppo piccolo)."""
    direction = 1 if signal_type == "buy" else -1
    entry_fill = _entry_fill(signal_price, direction)
    sl_distance = atr_5m(entry_fill) * CONFIG["sl_atr_mult"]
    stop_loss = entry_fill - sl_distance * direction
    take_profit = entry_fill + sl_distance * CONFIG["tp_ratio"] * direction
    risk_amount = capital * CONFIG["risk_pct"] / 100
    max_notional = capital * CONFIG["max_notional_pct"]
    per_unit_risk = abs(entry_fill - stop_loss)
    size_by_risk = risk_amount / per_unit_risk if per_unit_risk > 0 else 0
    size_by_capital = max_notional / entry_fill if entry_fill > 0 else 0
    final_size = min(size_by_risk, size_by_capital)
    notional = final_size * entry_fill
    if final_size <= 0 or notional < MIN_NOTIONAL_USDT:
        return None
    fee_entry = abs(entry_fill * final_size) * CONFIG["fee_pct"]
    return {
        "direction": "LONG" if direction == 1 else "SHORT", "dir": direction,
        "entry": round(entry_fill, 6), "entry_price_signal": round(signal_price, 6),
        "entry_price_fill": round(entry_fill, 6), "sl": round(stop_loss, 6),
        "sl_initial": round(stop_loss, 6), "tp": round(take_profit, 6),
        "size": round(final_size, 8), "open_time": None, "signal_candle_time": candle_time,
        "strategy": active, "market_regime": market_regime,
        "strategy_selector_reason": strategy_selector_reason,
        "unrealized": 0.0, "unrealized_gross": 0.0, "trailing": CONFIG["trailing_stop"],
        "fee_entry": round(fee_entry, 8), "fee_pct": CONFIG["fee_pct"],
        "slippage_pct": CONFIG["slippage_pct"], "risk_amount": round(risk_amount, 6),
        "notional": round(notional, 6), "size_by_risk": round(size_by_risk, 8),
        "size_by_capital": round(size_by_capital, 8), "final_size": round(final_size, 8),
        "max_notional_pct": CONFIG["max_notional_pct"], "paper_short_simulation": direction == -1,
    }


async def open_position(signal: dict[str, Any], signal_price: float, candle_time: Any = None) -> None:
    if state["open_position"] or signal.get("type") not in {"buy", "sell"}:
        return
    block = _entry_block_reason_for_signal(signal.get("type", "wait"), signal.get("score", 0) or 0)
    if block:
        current = deepcopy(state.get("signal") or signal); current.update({"entry_allowed": False, "entry_block_reason": block, "detail": f"{current.get('detail', 'Segnale rilevato')} | Entry bloccata: {block}"}); state["signal"] = current; return
    active = state.get("active_strategy", CONFIG["strategy"])
    # Filtro anti-late-entry solo per strategie trend/momentum (ema, macd).
    # BB è mean-reversion: le ultime 3 candele in direzione opposta sono la condizione di entry.
    # Ichi già filtra internamente in signal_ichi(); evita doppio blocco.
    if active in {"ema", "macd"}:
        rebound_risk, rebound_why = _recent_rebound_risk(signal["type"])
        if rebound_risk:
            current = deepcopy(state.get("signal") or signal)
            current.update({"entry_allowed": False, "entry_block_reason": f"late_entry_{rebound_why}", "detail": f"{current.get('detail', 'Segnale rilevato')} | Entry bloccata: ingresso in ritardo ({rebound_why})"})
            state["signal"] = current
            return
    position = _build_position_params(
        signal["type"], signal_price, state["capital"], active,
        candle_time, state.get("market_regime"), state.get("strategy_selector_reason"),
    )
    if position is None:
        return
    position["open_time"] = _now_iso()
    state["open_position"] = position; _add_log("open", position); _save_paper_state()
    logger.info(f"[PAPER] {position['direction']} {active} @ {position['entry']:.4f} | SL {position['sl']:.4f} | TP {position['tp']:.4f}")
    _tg(f"[OPEN] {position['direction']} {active} @ {position['entry']:.2f} | SL {position['sl']:.2f} | TP {position['tp']:.2f}")


async def check_position(live_price: float) -> None:
    pos = state["open_position"]
    if not pos:
        return
    pnl = _position_pnl(pos, live_price); pos["unrealized"] = round(pnl["pnl_net"], 4); pos["unrealized_gross"] = round(pnl["pnl_gross"], 4); pos["exit_price_live_est"] = round(pnl["exit_fill"], 6); pos["fee_exit_est"] = round(pnl["fee_exit"], 8); _refresh_risk_guard()
    if pos.get("trailing", False):
        dist = abs(pos["entry_price_fill"] - pos["sl_initial"])
        if pos["dir"] == 1:
            pos["sl"] = max(pos["sl"], round(live_price - dist, 6))
        else:
            pos["sl"] = min(pos["sl"], round(live_price + dist, 6))
    hit_sl = (pos["dir"] == 1 and live_price <= pos["sl"]) or (pos["dir"] == -1 and live_price >= pos["sl"])
    hit_tp = (pos["dir"] == 1 and live_price >= pos["tp"]) or (pos["dir"] == -1 and live_price <= pos["tp"])
    if hit_sl or hit_tp:
        await close_position(live_price, "TP" if hit_tp else "SL")


async def close_position(signal_price: float, reason: str) -> None:
    pos = state["open_position"]
    if not pos:
        return
    pnl = _position_pnl(pos, signal_price); state["capital"] += pnl["pnl_net"]
    if reason == "SL":
        side = "buy" if pos["dir"] == 1 else "sell"
        state["entry_cooldowns"][side] = CONFIG["post_sl_cooldown_candles"]
        state["last_sl_direction"] = pos["direction"]
        state["last_sl_time"] = _now_iso()
    m = state["metrics"]; m["trades"] += 1; m["wins"] += 1 if pnl["pnl_net"] > 0 else 0; m["total_pnl"] = state["capital"] - state["init_capital"]; m["total_pnl_pct"] = m["total_pnl"] / state["init_capital"] * 100; m["win_rate"] = m["wins"] / m["trades"] * 100 if m["trades"] else 0; m["peak_capital"] = max(m["peak_capital"], state["capital"]); m["max_dd"] = max(m["max_dd"], (m["peak_capital"] - state["capital"]) / m["peak_capital"] * 100)
    state["equity_curve"].append(round(state["capital"], 2)); state["equity_curve"] = state["equity_curve"][-500:]
    trade = {"time": _now_iso(), "direction": pos["direction"], "entry": pos["entry_price_fill"], "exit": round(pnl["exit_fill"], 6), "size": pos["size"], "reason": reason, "strategy": pos["strategy"], "market_regime": pos.get("market_regime"), "strategy_selector_reason": pos.get("strategy_selector_reason"), "entry_price_signal": pos["entry_price_signal"], "entry_price_fill": pos["entry_price_fill"], "exit_price_signal": round(signal_price, 6), "exit_price_fill": round(pnl["exit_fill"], 6), "fee_entry": round(pos["fee_entry"], 8), "fee_exit": round(pnl["fee_exit"], 8), "fee_total": round(pnl["fee_total"], 8), "slippage_pct": pos["slippage_pct"], "fee_pct": pos["fee_pct"], "pnl_gross": round(pnl["pnl_gross"], 4), "pnl_net": round(pnl["pnl_net"], 4), "pnl": round(pnl["pnl_net"], 4), "risk_amount": pos["risk_amount"], "notional": pos["notional"], "size_by_risk": pos["size_by_risk"], "size_by_capital": pos["size_by_capital"], "final_size": pos["final_size"], "max_notional_pct": pos["max_notional_pct"]}
    state["trades"].insert(0, trade); state["trades"] = state["trades"][:100]
    log_pos = deepcopy(pos); log_pos["exit"] = trade["exit"]; _add_log("close", log_pos, pnl["pnl_net"], reason)
    state["open_position"] = None; _refresh_risk_guard(); _save_trades_json(); _save_paper_state(); logger.info(f"[{reason}] Chiuso {pos['direction']} {pos['strategy']} | P&L netto: {pnl['pnl_net']:+.4f} USDT")
    _tg(f"[{reason}] {pos['direction']} {pos['strategy']} | P&L: {pnl['pnl_net']:+.2f} USDT | Cap: {state['capital']:.2f}")


def _save_trades_json() -> None:
    try:
        data = {"symbol": state["symbol"], "strategy": state["strategy"], "active_strategy": state["active_strategy"], "paper_mode": state["paper_mode"], "paper_only_build": True, "capital": state["capital"], "metrics": state["metrics"], "trades": state["trades"], "equity_curve": state["equity_curve"], "saved_at": _now_iso()}
        tmp = TRADES_FILE.with_suffix(".tmp"); tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"); tmp.replace(TRADES_FILE)
    except Exception as exc:
        logger.warning(f"Errore salvataggio trades.json: {exc}")


def _save_paper_state() -> None:
    try:
        data = {"paper_only_build": True, "live_trading_enabled": False, "exchange_order_blocked": True, "symbol": state["symbol"], "strategy": state["strategy"], "active_strategy": state["active_strategy"], "fallback_strategy": state["fallback_strategy"], "market_regime": state["market_regime"], "strategy_selector_reason": state["strategy_selector_reason"], "strategy_scores": state["strategy_scores"], "entry_cooldowns": state["entry_cooldowns"], "last_sl_direction": state["last_sl_direction"], "last_sl_time": state["last_sl_time"], "capital": state["capital"], "last_update": _now_iso(), "kill_switch_active": state["kill_switch_active"], "kill_switch_reason": state["kill_switch_reason"], "kill_switch_time": state["kill_switch_time"], "trading_halted": state["trading_halted"], "trading_halt_reason": state["trading_halt_reason"], "low_capital_alert": state["low_capital_alert"], "low_capital_alert_time": state["low_capital_alert_time"], "risk_guard": state["risk_guard"], "strategy_params": _strategy_env_params(), "config": _public_config(), "open_position": state["open_position"]}
        tmp = STATE_FILE.with_suffix(".tmp"); tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"); tmp.replace(STATE_FILE)
    except Exception as exc:
        logger.warning(f"Errore salvataggio state.json: {exc}")


def _load_trades_json() -> None:
    if not TRADES_FILE.exists():
        return
    try:
        data = json.loads(TRADES_FILE.read_text(encoding="utf-8"))
        if data.get("symbol") == CONFIG["symbol"]:
            state["trades"] = data.get("trades", [])[:100]; state["equity_curve"] = data.get("equity_curve", [state["capital"]])[-500:] or [state["capital"]]
            if isinstance(data.get("metrics"), dict):
                state["metrics"].update(data["metrics"])
    except Exception as exc:
        logger.warning(f"Impossibile caricare trades.json: {exc}")


def _load_paper_state() -> None:
    if not STATE_FILE.exists():
        return
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not data.get("paper_only_build") or data.get("symbol") != CONFIG["symbol"]:
            return
        state["capital"] = float(data.get("capital", state["capital"])); state["kill_switch_active"] = bool(data.get("kill_switch_active", False)); state["kill_switch_reason"] = data.get("kill_switch_reason"); state["kill_switch_time"] = data.get("kill_switch_time"); state["active_strategy"] = data.get("active_strategy", state["active_strategy"]); state["strategy"] = state["active_strategy"]; state["fallback_strategy"] = data.get("fallback_strategy", CONFIG["auto_strategy_fallback"]); state["market_regime"] = data.get("market_regime", state["market_regime"]); state["strategy_selector_reason"] = data.get("strategy_selector_reason", state["strategy_selector_reason"]); state["strategy_scores"] = data.get("strategy_scores", state["strategy_scores"]); state["entry_cooldowns"].update(data.get("entry_cooldowns", {})); state["last_sl_direction"] = data.get("last_sl_direction"); state["last_sl_time"] = data.get("last_sl_time")
        saved = data.get("open_position")
        if saved and saved.get("strategy") in ALLOWED_STRATEGIES:
            state["open_position"] = saved
        if isinstance(data.get("risk_guard"), dict):
            state["risk_guard"].update(data["risk_guard"])
        _sync_trading_halt_state()
    except Exception as exc:
        logger.warning(f"Impossibile caricare state.json; parto flat: {exc}"); state["open_position"] = None


def _snapshot_state() -> dict[str, Any]:
    _sync_state_config(); snap = deepcopy(state); snap["config"] = _public_config(); snap["strategy_params"] = _strategy_env_params(); snap["last_update"] = _now_iso(); return snap


def _kline_row_to_candle(row: list[Any]) -> dict[str, Any]:
    return {"t": int(row[0]), "T": int(row[6]), "o": float(row[1]), "h": float(row[2]), "l": float(row[3]), "c": float(row[4]), "v": float(row[5])}


async def _bootstrap_historical_candles(public_client: AsyncClient) -> None:
    """Load closed historical candles so strategies are ready immediately at startup."""
    if closed_candles and closed_prices_5m:
        return
    state["historical_bootstrap_status"] = "loading"
    try:
        rows_1m = await public_client.get_klines(symbol=CONFIG["symbol"], interval="1m", limit=MAX_CANDLES)
        rows_5m = await public_client.get_klines(symbol=CONFIG["symbol"], interval="5m", limit=200)
        # The last REST kline can still be open; drop it and let websocket handle live updates.
        closed_rows_1m = rows_1m[:-1] if len(rows_1m) > 1 else rows_1m
        closed_rows_5m = rows_5m[:-1] if len(rows_5m) > 1 else rows_5m
        closed_candles.clear(); closed_prices.clear(); closed_volumes.clear(); closed_prices_5m.clear(); closed_candles_5m.clear()
        for row in closed_rows_1m[-MAX_CANDLES:]:
            candle = _kline_row_to_candle(row)
            closed_candles.append(candle); closed_prices.append(candle["c"]); closed_volumes.append(candle["v"])
        for row in closed_rows_5m[-200:]:
            c5m = {"t": int(row[0]), "T": int(row[6]), "h": float(row[2]), "l": float(row[3]), "c": float(row[4])}
            closed_candles_5m.append(c5m)
            closed_prices_5m.append(float(row[4]))
        state["candles"] = list(closed_candles)
        state["historical_candles_loaded_1m"] = len(closed_candles)
        state["historical_candles_loaded_5m"] = len(closed_prices_5m)
        state["historical_bootstrap_status"] = "ready"
        state["historical_bootstrap_error"] = None
        if closed_candles:
            last = closed_candles[-1]
            state["last_closed_candle_time"] = last["T"]
            state["last_signal_candle_time"] = last["T"]
            state["last_live_price"] = last["c"]
            state["price"] = last["c"]
            bootstrap_active = select_active_strategy(last["T"])
            raw = get_signal(bootstrap_active, last["T"], last["c"])
            dec = _decorate_signal(raw, last["T"], last["c"])
            dec["entry_allowed"] = False
            dec["entry_block_reason"] = "historical_bootstrap_signal_only"
            dec["detail"] = f"{dec.get('detail', 'Storico caricato')} | attendo nuova candela live"
            state["signal"] = dec
        _add_log("bootstrap", {"direction": "SYSTEM", "entry": state.get("price", 0), "strategy": state.get("active_strategy")}, 0, f"loaded_1m={len(closed_candles)} loaded_5m={len(closed_prices_5m)}")
        logger.info(f"Bootstrap storico caricato: 1m={len(closed_candles)} 5m={len(closed_prices_5m)}")
    except Exception as exc:
        state["historical_bootstrap_status"] = "error"
        state["historical_bootstrap_error"] = str(exc)
        _append_error(f"Bootstrap storico fallito: {exc}")
        logger.warning(f"Bootstrap storico fallito: {exc}")


async def _handle_closed_1m_candle(k: dict[str, Any]) -> None:
    candle = {"t": k.get("t"), "T": k.get("T"), "o": float(k["o"]), "h": float(k["h"]), "l": float(k["l"]), "c": float(k["c"]), "v": float(k["v"])}
    closed_candles.append(candle); closed_prices.append(candle["c"]); closed_volumes.append(candle["v"]); state["candles"] = list(closed_candles); state["last_closed_candle_time"] = candle["T"]
    if state["last_signal_candle_time"] == candle["T"]:
        return
    _tick_entry_cooldowns()
    active = select_active_strategy(candle["T"])
    raw = get_signal(active, candle["T"], candle["c"]); dec = _decorate_signal(raw, candle["T"], candle["c"]); state["last_signal_candle_time"] = candle["T"]
    if state["open_position"]:
        dec["entry_allowed"] = False
        dec["entry_block_reason"] = "position_already_open"
        dec["detail"] = f"{dec.get('detail', '')} | monitor only: posizione aperta"
        state["signal"] = dec
        minute = datetime.fromtimestamp(candle["T"] / 1000).minute
        if minute % 5 == 0 and state.get("last_monitor_log_candle_time") != candle["T"]:
            pos = state["open_position"]
            _add_log("monitor", {"direction": pos.get("direction", "OPEN"), "entry": candle["c"], "strategy": pos.get("strategy", state.get("active_strategy"))}, pos.get("unrealized", 0.0), "position_already_open")
            state["last_monitor_log_candle_time"] = candle["T"]
        _save_paper_state()
        return
    state["signal"] = dec
    if dec.get("type") in {"buy", "sell"} or dec.get("entry_block_reason"):
        _add_log("signal", {"direction": dec["type"].upper(), "entry": candle["c"], "strategy": dec.get("strategy")}, dec.get("score", 0), dec.get("entry_block_reason") or dec.get("detail", ""))
    if dec["entry_allowed"]:
        await open_position(dec, candle["c"], candle["T"])
    _save_paper_state()


async def _handle_kline_message(k: dict[str, Any]) -> None:
    interval = k.get("i"); close = float(k["c"]); prev = state.get("last_live_price") or close
    state["last_live_price"] = close; state["price"] = close; state["price_change"] = (close - prev) / prev * 100 if prev else 0.0; state["last_price_update_time"] = _now_iso(); state["last_update"] = state["last_price_update_time"]
    if state["open_position"]:
        await check_position(close)
    if k.get("x"):
        if interval == "1m":
            await _handle_closed_1m_candle(k)
        elif interval == "5m":
            c5m = {"t": k.get("t"), "T": k.get("T"), "h": float(k["h"]), "l": float(k["l"]), "c": close}
            closed_candles_5m.append(c5m)
            closed_prices_5m.append(close)
            state["historical_candles_loaded_5m"] = len(closed_prices_5m)


async def websocket_loop() -> None:
    global client
    streams = [f"{CONFIG['symbol'].lower()}@kline_1m", f"{CONFIG['symbol'].lower()}@kline_5m"]
    while not state["shutdown_requested"]:
        try:
            state["websocket_status"] = "connecting"; client = await AsyncClient.create(); await _bootstrap_historical_candles(client); manager = BinanceSocketManager(client)
            async with manager.multiplex_socket(streams) as stream:
                state["running"] = True; state["websocket_status"] = "connected"; state["websocket_connected_at"] = _now_iso(); state["next_reconnect_time"] = None; state["reconnect_count"] = 0; state["reconnect_delay_sec"] = 0; logger.info(f"WebSocket connesso: {', '.join(streams)}")
                while not state["shutdown_requested"]:
                    # Timeout di 90s: se Binance smette di inviare dati senza chiudere
                    # il socket (connessione zombie), forziamo il reconnect.
                    try:
                        msg = await asyncio.wait_for(stream.recv(), timeout=90)
                    except asyncio.TimeoutError:
                        raise ConnectionError("Nessun dato dal WebSocket per 90s (connessione zombie)")
                    data = msg.get("data", msg); k = data.get("k") if isinstance(data, dict) else None
                    if k:
                        await _handle_kline_message(k)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state["running"] = False; state["websocket_status"] = "reconnecting"; state["last_websocket_error"] = str(exc); state["reconnect_count"] += 1
            delay = min(CONFIG["reconnect_max_delay_sec"], CONFIG["reconnect_initial_delay_sec"] * (2 ** min(state["reconnect_count"], 5)))
            state["reconnect_delay_sec"] = delay; state["last_reconnect_time"] = _now_iso(); state["next_reconnect_time"] = (datetime.now() + timedelta(seconds=delay)).isoformat(); _append_error(f"WebSocket reconnect: {exc}"); logger.warning(f"WebSocket error: {exc}; reconnect tra {delay:.1f}s"); await asyncio.sleep(delay)
        finally:
            if client:
                with suppress(Exception):
                    await client.close_connection()
                client = None
    state["running"] = False; state["websocket_status"] = "stopped"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global websocket_task
    _load_trades_json(); _load_paper_state(); _sync_state_config(); websocket_task = asyncio.create_task(websocket_loop())
    try:
        yield
    finally:
        state["shutdown_requested"] = True
        if websocket_task:
            websocket_task.cancel()
            with suppress(asyncio.CancelledError):
                await websocket_task
        if client:
            with suppress(Exception):
                await client.close_connection()
        _save_paper_state()


app = FastAPI(title="Paper Scalping Bot", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[f"http://{HOST}:{PORT}", "http://127.0.0.1:8000", "http://localhost:8000"], allow_credentials=False, allow_methods=["GET", "POST"], allow_headers=["Content-Type"])


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    path = BOT_DIR / "index.html"
    return path.read_text(encoding="utf-8") if path.exists() else "<h1>index.html non trovato</h1>"


@app.get("/api/state")
async def api_state() -> JSONResponse:
    return JSONResponse(_snapshot_state())


@app.get("/api/trades")
async def api_trades() -> JSONResponse:
    return JSONResponse({"trades": state["trades"], "metrics": state["metrics"]})


@app.get("/api/trades/download")
async def api_trades_download() -> JSONResponse:
    return JSONResponse({"file": str(TRADES_FILE), "data": json.loads(TRADES_FILE.read_text(encoding="utf-8")) if TRADES_FILE.exists() else {}})


@app.post("/api/config")
async def api_config(body: dict[str, Any]) -> JSONResponse:
    try:
        candidate = _validated_config_update(body)
        if state["open_position"]:
            blocked = {"strategy", "auto_strategy_selector", "auto_strategy_fallback", "capital", "risk_pct", "tp_ratio", "sl_atr_mult", "max_notional_pct", "allow_short", "post_sl_cooldown_candles"}
            if any(k in body for k in blocked):
                raise ValueError("Posizione aperta: modifiche operative bloccate fino alla chiusura")
        capital_changed = candidate["capital"] != CONFIG["capital"]
        CONFIG.clear(); CONFIG.update(candidate)
        if capital_changed and not state["open_position"]:
            state["capital"] = CONFIG["capital"]; state["init_capital"] = CONFIG["capital"]; state["metrics"]["peak_capital"] = CONFIG["capital"]; state["equity_curve"] = [CONFIG["capital"]]; _reset_daily_guard()
        _sync_state_config(); _save_paper_state(); return JSONResponse({"ok": True, "config": _public_config(), "active_strategy": state["active_strategy"]})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/kill-switch")
async def api_kill_switch(body: dict[str, Any]) -> JSONResponse:
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail="enabled deve essere booleano")
    if enabled:
        state["kill_switch_active"] = True; state["kill_switch_reason"] = str(body.get("reason") or "dashboard_kill_switch"); state["kill_switch_time"] = _now_iso()
        _tg(f"[HALT] Kill switch attivato: {state['kill_switch_reason']}")
        if body.get("close_position") is True and state["open_position"] and state["last_live_price"]:
            await close_position(state["last_live_price"], "KILL")
    else:
        state["kill_switch_active"] = False; state["kill_switch_reason"] = None; state["kill_switch_time"] = None; state["signal"] = {"type": "wait", "score": 0, "detail": "Kill switch resettato: attendo nuova candela chiusa", "conditions": []}
    _sync_trading_halt_state(); _save_paper_state(); return JSONResponse({"ok": True, "kill_switch_active": state["kill_switch_active"], "trading_halted": state["trading_halted"]})


if __name__ == "__main__":
    logger.info(f"Bot avviato [PAPER-ONLY] su http://{HOST}:{PORT}")
    uvicorn.run("bot:app", host=HOST, port=PORT, reload=False)
