"""
Paper-only Binance scalping bot with FastAPI dashboard.

This build is intentionally unable to place live exchange orders. It uses
Binance public market data for paper trading and dashboard updates only.
"""

import asyncio
import json
import os
import time
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


CONFIG: dict[str, Any] = {
    "symbol": _env_str("SYMBOL", "BTCUSDT").upper(),
    "strategy": _env_str("STRATEGY", "ema").lower(),
    "capital": _env_float("CAPITAL", 1000.0),
    "risk_pct": _env_float("RISK_PCT", 1.0),
    "tp_ratio": _env_float("TP_RATIO", 2.0),
    "sl_atr_mult": _env_float("SL_ATR_MULT", 1.0),
    "ema_fast": _env_int("EMA_FAST", 9),
    "ema_slow": _env_int("EMA_SLOW", 21),
    "rsi_period": _env_int("RSI_PERIOD", 14),
    "bb_period": _env_int("BB_PERIOD", 20),
    "bb_mult": _env_float("BB_MULT", 2.0, aliases=("BB_DEV",)),
    "macd_fast": _env_int("MACD_FAST", 12),
    "macd_slow": _env_int("MACD_SLOW", 26),
    "macd_signal": _env_int("MACD_SIGNAL", 9, aliases=("MACD_SIG",)),
    "ichi_tenkan": _env_int("ICHI_TENKAN", 9, aliases=("ICHI_T",)),
    "ichi_kijun": _env_int("ICHI_KIJUN", 26, aliases=("ICHI_K",)),
    "ichi_senkou_b": _env_int("ICHI_SENKOU_B", 52, aliases=("ICHI_S",)),
    "fee_pct": _env_float("FEE_PCT", 0.001),
    "slippage_pct": _env_float("SLIPPAGE_PCT", 0.0002),
    "max_notional_pct": _env_float("MAX_NOTIONAL_PCT", 0.95),
    "daily_loss_limit_pct": _env_float("DAILY_LOSS_LIMIT_PCT", 3.0),
    "daily_loss_limit_enabled": _env_bool("DAILY_LOSS_LIMIT_ENABLED", "true"),
    "reconnect_initial_delay_sec": _env_float("RECONNECT_INITIAL_DELAY_SEC", 2.0),
    "reconnect_max_delay_sec": _env_float("RECONNECT_MAX_DELAY_SEC", 60.0),
    "trailing_stop": _env_bool("TRAILING_STOP", "true"),
    "whipsaw_filter": _env_bool("WHIPSAW_FILTER", "true"),
    "mtf_filter": _env_bool("MTF_FILTER", "true"),
}


CONFIG_SCHEMA: dict[str, type] = {
    "strategy": str,
    "capital": float,
    "risk_pct": float,
    "tp_ratio": float,
    "sl_atr_mult": float,
    "ema_fast": int,
    "ema_slow": int,
    "rsi_period": int,
    "bb_period": int,
    "bb_mult": float,
    "macd_fast": int,
    "macd_slow": int,
    "macd_signal": int,
    "ichi_tenkan": int,
    "ichi_kijun": int,
    "ichi_senkou_b": int,
    "fee_pct": float,
    "slippage_pct": float,
    "max_notional_pct": float,
    "daily_loss_limit_pct": float,
    "daily_loss_limit_enabled": bool,
    "reconnect_initial_delay_sec": float,
    "reconnect_max_delay_sec": float,
    "trailing_stop": bool,
    "whipsaw_filter": bool,
    "mtf_filter": bool,
}


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
    if candidate["strategy"] not in {"ema", "bb", "macd", "ichi"}:
        raise ValueError("strategy consentite: ema, bb, macd, ichi")
    if not 0.1 <= candidate["risk_pct"] <= 2.0:
        raise ValueError("risk_pct deve essere tra 0.1 e 2.0")
    if not 10 <= candidate["capital"] <= 1_000_000:
        raise ValueError("capital deve essere tra 10 e 1_000_000")
    if not 0.5 <= candidate["tp_ratio"] <= 10:
        raise ValueError("tp_ratio deve essere tra 0.5 e 10")
    if not 0.2 <= candidate["sl_atr_mult"] <= 10:
        raise ValueError("sl_atr_mult deve essere tra 0.2 e 10")
    if not (candidate["ema_fast"] >= 2 and candidate["ema_fast"] < candidate["ema_slow"]):
        raise ValueError("ema_fast deve essere >= 2 e minore di ema_slow")
    if not candidate["ema_slow"] <= 300:
        raise ValueError("ema_slow deve essere <= 300")
    if not 2 <= candidate["rsi_period"] <= 100:
        raise ValueError("rsi_period deve essere tra 2 e 100")
    if not 5 <= candidate["bb_period"] <= 300:
        raise ValueError("bb_period deve essere tra 5 e 300")
    if not 0.5 <= candidate["bb_mult"] <= 5:
        raise ValueError("bb_mult deve essere tra 0.5 e 5")
    if not candidate["macd_fast"] >= 2:
        raise ValueError("macd_fast deve essere >= 2")
    if not candidate["macd_slow"] > candidate["macd_fast"]:
        raise ValueError("macd_slow deve essere maggiore di macd_fast")
    if not 2 <= candidate["macd_signal"] <= 100:
        raise ValueError("macd_signal deve essere tra 2 e 100")
    if not candidate["ichi_tenkan"] < candidate["ichi_kijun"] < candidate["ichi_senkou_b"]:
        raise ValueError("parametri Ichimoku non coerenti: tenkan < kijun < senkou_b")
    if not 0 <= candidate["fee_pct"] <= 0.01:
        raise ValueError("fee_pct deve essere tra 0 e 0.01")
    if not 0 <= candidate["slippage_pct"] <= 0.01:
        raise ValueError("slippage_pct deve essere tra 0 e 0.01")
    if not 0.01 <= candidate["max_notional_pct"] <= 1.0:
        raise ValueError("max_notional_pct deve essere tra 0.01 e 1.0")
    if not 0.1 <= candidate["daily_loss_limit_pct"] <= 50:
        raise ValueError("daily_loss_limit_pct deve essere tra 0.1 e 50")
    if not 1 <= candidate["reconnect_initial_delay_sec"] <= 60:
        raise ValueError("reconnect_initial_delay_sec deve essere tra 1 e 60")
    if not 2 <= candidate["reconnect_max_delay_sec"] <= 300:
        raise ValueError("reconnect_max_delay_sec deve essere tra 2 e 300")
    if candidate["reconnect_max_delay_sec"] < candidate["reconnect_initial_delay_sec"]:
        raise ValueError("reconnect_max_delay_sec deve essere >= reconnect_initial_delay_sec")


def _validated_config_update(body: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(body) - set(CONFIG_SCHEMA))
    if unknown:
        raise ValueError(f"Parametri sconosciuti: {', '.join(unknown)}")

    candidate = deepcopy(CONFIG)
    for key, value in body.items():
        candidate[key] = _coerce_value(key, value)
    _validate_config(candidate)
    return candidate


try:
    _validate_config(CONFIG)
except ValueError as exc:
    raise RuntimeError(f"Invalid startup config: {exc}") from exc


def _public_config() -> dict[str, Any]:
    return deepcopy(CONFIG)


state: dict[str, Any] = {
    "running": False,
    "paper_mode": True,
    "paper_only_build": True,
    "live_trading_enabled": False,
    "exchange_order_blocked": True,
    "paper_short_simulation": True,
    "spot_short_live_supported": False,
    "symbol": CONFIG["symbol"],
    "strategy": CONFIG["strategy"],
    "config": _public_config(),
    "capital": CONFIG["capital"],
    "init_capital": CONFIG["capital"],
    "price": 0.0,
    "last_live_price": 0.0,
    "price_change": 0.0,
    "last_price_update_time": None,
    "price_source": "binance_websocket_live",
    "strategy_source": "closed_candles_only",
    "websocket_status": "stopped",
    "websocket_connected_at": None,
    "reconnect_count": 0,
    "reconnect_delay_sec": 0,
    "last_reconnect_time": None,
    "next_reconnect_time": None,
    "last_websocket_error": None,
    "shutdown_requested": False,
    "kill_switch_active": False,
    "kill_switch_reason": None,
    "kill_switch_time": None,
    "trading_halted": False,
    "trading_halt_reason": None,
    "risk_guard": {
        "daily_date": _today_str(),
        "daily_start_capital": CONFIG["capital"],
        "daily_realized_pnl": 0.0,
        "daily_unrealized_pnl": 0.0,
        "daily_total_pnl": 0.0,
        "daily_loss_limit_pct": CONFIG["daily_loss_limit_pct"],
        "daily_loss_limit_amount": round(CONFIG["capital"] * CONFIG["daily_loss_limit_pct"] / 100, 6),
        "daily_loss_limit_enabled": CONFIG["daily_loss_limit_enabled"],
        "daily_loss_limit_hit": False,
        "daily_loss_limit_hit_time": None,
    },
    "last_signal_candle_time": None,
    "last_closed_candle_time": None,
    "open_position": None,
    "trades": [],
    "metrics": {
        "total_pnl": 0.0,
        "total_pnl_pct": 0.0,
        "trades": 0,
        "wins": 0,
        "win_rate": 0.0,
        "max_dd": 0.0,
        "peak_capital": CONFIG["capital"],
    },
    "indicators": {},
    "signal": {
        "type": "wait",
        "score": 0,
        "detail": "Bot in attesa di candele chiuse",
        "conditions": [],
    },
    "candles": [],
    "equity_curve": [CONFIG["capital"]],
    "log": [],
    "errors": [],
    "last_update": None,
    "filters": {"whipsaw": False, "mtf_trend": "neutral"},
}

closed_prices: deque[float] = deque(maxlen=MAX_CANDLES)
closed_volumes: deque[float] = deque(maxlen=MAX_CANDLES)
closed_candles: deque[dict[str, Any]] = deque(maxlen=MAX_CANDLES)
closed_prices_5m: deque[float] = deque(maxlen=200)

client: Optional[AsyncClient] = None
websocket_task: Optional[asyncio.Task] = None


def _append_error(msg: str) -> None:
    state["errors"].append({"time": _now_iso(), "msg": msg})
    if len(state["errors"]) > 50:
        state["errors"] = state["errors"][-50:]


def _sync_state_config() -> None:
    state["strategy"] = CONFIG["strategy"]
    state["symbol"] = CONFIG["symbol"]
    state["config"] = _public_config()
    _refresh_risk_guard()


def _sync_trading_halt_state() -> None:
    risk_guard = state["risk_guard"]
    if state["kill_switch_active"]:
        state["trading_halted"] = True
        state["trading_halt_reason"] = state["kill_switch_reason"] or "kill_switch"
    elif risk_guard.get("daily_loss_limit_hit"):
        state["trading_halted"] = True
        state["trading_halt_reason"] = "daily_loss_limit"
    else:
        state["trading_halted"] = False
        state["trading_halt_reason"] = None


def _reset_daily_guard(today: Optional[str] = None) -> None:
    date_value = today or _today_str()
    start_capital = float(state["capital"])
    state["risk_guard"].update({
        "daily_date": date_value,
        "daily_start_capital": start_capital,
        "daily_realized_pnl": 0.0,
        "daily_unrealized_pnl": 0.0,
        "daily_total_pnl": 0.0,
        "daily_loss_limit_pct": CONFIG["daily_loss_limit_pct"],
        "daily_loss_limit_amount": round(start_capital * CONFIG["daily_loss_limit_pct"] / 100, 6),
        "daily_loss_limit_enabled": CONFIG["daily_loss_limit_enabled"],
        "daily_loss_limit_hit": False,
        "daily_loss_limit_hit_time": None,
    })
    _sync_trading_halt_state()


def _refresh_risk_guard() -> bool:
    guard = state["risk_guard"]
    changed = False
    today = _today_str()
    if guard.get("daily_date") != today or guard.get("daily_start_capital") is None:
        _reset_daily_guard(today)
        return True

    start_capital = float(guard.get("daily_start_capital") or state["capital"])
    unrealized = 0.0
    if state["open_position"]:
        unrealized = float(state["open_position"].get("unrealized", 0.0) or 0.0)
    realized = float(state["capital"]) - start_capital
    total = realized + unrealized
    limit_pct = CONFIG["daily_loss_limit_pct"]
    limit_amount = start_capital * limit_pct / 100

    updates = {
        "daily_realized_pnl": round(realized, 6),
        "daily_unrealized_pnl": round(unrealized, 6),
        "daily_total_pnl": round(total, 6),
        "daily_loss_limit_pct": limit_pct,
        "daily_loss_limit_amount": round(limit_amount, 6),
        "daily_loss_limit_enabled": CONFIG["daily_loss_limit_enabled"],
    }
    for key, value in updates.items():
        if guard.get(key) != value:
            guard[key] = value
            changed = True

    if CONFIG["daily_loss_limit_enabled"] and not guard.get("daily_loss_limit_hit") and total <= -limit_amount:
        guard["daily_loss_limit_hit"] = True
        guard["daily_loss_limit_hit_time"] = _now_iso()
        state["signal"] = {
            "type": "wait",
            "score": 0,
            "detail": "Daily loss limit raggiunto: nuove entry bloccate",
            "conditions": [],
        }
        _add_log("halt", {"direction": "SYSTEM", "entry": 0}, total, "DAILY_LOSS")
        logger.warning(f"Daily loss limit raggiunto: {total:.4f} USDT <= -{limit_amount:.4f} USDT")
        changed = True

    previous_halted = state["trading_halted"]
    previous_reason = state["trading_halt_reason"]
    _sync_trading_halt_state()
    return changed or previous_halted != state["trading_halted"] or previous_reason != state["trading_halt_reason"]


def _entry_block_reason() -> Optional[str]:
    _refresh_risk_guard()
    if state["kill_switch_active"]:
        return state["kill_switch_reason"] or "kill_switch"
    if state["risk_guard"].get("daily_loss_limit_hit"):
        return "daily_loss_limit"
    return None


def _coerce_bool_input(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
        return value.strip().lower() in {"true", "1", "yes", "on"}
    raise ValueError(f"{field} deve essere booleano")


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
    if len(data) < period:
        return None
    return sum(data[-period:]) / period


def std_dev(data: list[float], period: int) -> Optional[float]:
    if len(data) < period:
        return None
    items = data[-period:]
    mean = sum(items) / period
    return (sum((item - mean) ** 2 for item in items) / period) ** 0.5


def rsi(data: list[float], period: int) -> float:
    if len(data) < period + 1:
        return 50.0
    gains = 0.0
    losses = 0.0
    for i in range(len(data) - period, len(data)):
        diff = data[i] - data[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses += abs(diff)
    rs = gains / (losses or 1e-9)
    return 100 - 100 / (1 + rs)


def vwap(prices_list: list[float], volumes_list: list[float], period: int = 30) -> float:
    price_slice = prices_list[-period:]
    volume_slice = volumes_list[-period:]
    price_volume = sum(price * volume for price, volume in zip(price_slice, volume_slice))
    total_volume = sum(volume_slice) or 1e-9
    return price_volume / total_volume


def atr_approx(price: float) -> float:
    if len(closed_candles) < 20:
        return price * 0.002
    recent = list(closed_candles)[-20:]
    ranges = [candle["h"] - candle["l"] for candle in recent]
    return sum(ranges) / len(ranges)


def is_whipsaw(prices_list: list[float]) -> bool:
    if not CONFIG["whipsaw_filter"]:
        return False
    fast = ema(prices_list, CONFIG["ema_fast"])
    slow = ema(prices_list, CONFIG["ema_slow"])
    if not fast or not slow:
        return True
    gap_pct = abs(fast - slow) / slow * 100
    return gap_pct < 0.05


def mtf_trend() -> str:
    if not CONFIG["mtf_filter"]:
        return "neutral"
    prices_5m = list(closed_prices_5m)
    if len(prices_5m) < CONFIG["ema_slow"] + 5:
        return "neutral"
    fast = ema(prices_5m, CONFIG["ema_fast"])
    slow = ema(prices_5m, CONFIG["ema_slow"])
    if not fast or not slow:
        return "neutral"
    gap_pct = abs(fast - slow) / slow * 100
    if gap_pct < 0.03:
        return "neutral"
    return "bull" if fast > slow else "bear"


def signal_ema() -> dict[str, Any]:
    prices_list = list(closed_prices)
    volumes_list = list(closed_volumes)
    if len(prices_list) < CONFIG["ema_slow"] + 5:
        return {"type": "wait", "score": 0, "detail": "Dati insufficienti su candele chiuse", "conditions": []}

    fast = ema(prices_list, CONFIG["ema_fast"])
    slow = ema(prices_list, CONFIG["ema_slow"])
    vw = vwap(prices_list, volumes_list)
    rsi_value = rsi(prices_list, CONFIG["rsi_period"])
    current = prices_list[-1]

    cond_buy = [fast > slow, current > vw, 20 < rsi_value < 45]
    cond_sell = [fast < slow, current < vw, 55 < rsi_value < 80]

    conditions = [
        {
            "name": f"EMA{CONFIG['ema_fast']} vs EMA{CONFIG['ema_slow']}",
            "value": f"{fast:.2f} {'>' if fast > slow else '<'} {slow:.2f}",
            "ok_buy": cond_buy[0],
            "ok_sell": cond_sell[0],
        },
        {
            "name": "Prezzo close vs VWAP",
            "value": f"{current:.2f} {'sopra' if current > vw else 'sotto'} {vw:.2f}",
            "ok_buy": cond_buy[1],
            "ok_sell": cond_sell[1],
        },
        {
            "name": f"RSI({CONFIG['rsi_period']})",
            "value": f"{rsi_value:.1f}",
            "ok_buy": cond_buy[2],
            "ok_sell": cond_sell[2],
        },
    ]

    state["indicators"] = {
        "ema_fast": round(fast, 4),
        "ema_slow": round(slow, 4),
        "vwap": round(vw, 4),
        "rsi": round(rsi_value, 2),
    }

    score_buy = sum(cond_buy)
    score_sell = sum(cond_sell)
    if score_buy >= 2:
        return {"type": "buy", "score": score_buy * 30, "detail": "EMA+RSI+VWAP allineati al rialzo su candela chiusa", "conditions": conditions}
    if score_sell >= 2:
        return {"type": "sell", "score": score_sell * 30, "detail": "EMA+RSI+VWAP allineati al ribasso su candela chiusa", "conditions": conditions}
    return {"type": "wait", "score": 0, "detail": "Nessuna confluenza sufficiente su candela chiusa", "conditions": conditions}


def signal_bb() -> dict[str, Any]:
    prices_list = list(closed_prices)
    if len(prices_list) < CONFIG["bb_period"] + 2:
        return {"type": "wait", "score": 0, "detail": "Dati insufficienti su candele chiuse", "conditions": []}

    mid = sma(prices_list, CONFIG["bb_period"])
    deviation = std_dev(prices_list, CONFIG["bb_period"])
    upper = mid + CONFIG["bb_mult"] * deviation
    lower = mid - CONFIG["bb_mult"] * deviation
    current = prices_list[-1]
    previous = prices_list[-2]
    width_pct = (upper - lower) / mid * 100

    cross_down = current < lower and previous >= lower
    cross_up = current > upper and previous <= upper

    conditions = [
        {"name": f"Banda sup ({CONFIG['bb_mult']}x)", "value": f"{upper:.4f}", "ok_buy": False, "ok_sell": cross_up},
        {"name": "Banda inferiore", "value": f"{lower:.4f}", "ok_buy": cross_down, "ok_sell": False},
        {"name": "Larghezza bande", "value": f"{width_pct:.2f}%", "ok_buy": width_pct > 1.5, "ok_sell": width_pct > 1.5},
    ]

    state["indicators"] = {
        "bb_upper": round(upper, 4),
        "bb_mid": round(mid, 4),
        "bb_lower": round(lower, 4),
        "bb_width": round(width_pct, 3),
    }

    if cross_down:
        return {"type": "buy", "score": 80, "detail": "Close sotto banda inferiore; mean reversion attesa", "conditions": conditions}
    if cross_up:
        return {"type": "sell", "score": 80, "detail": "Close sopra banda superiore; mean reversion attesa", "conditions": conditions}
    return {"type": "wait", "score": 0, "detail": "Close dentro le bande", "conditions": conditions}


def signal_macd() -> dict[str, Any]:
    prices_list = list(closed_prices)
    slow_period = CONFIG["macd_slow"]
    signal_period = CONFIG["macd_signal"]
    if len(prices_list) < slow_period + signal_period + 2:
        return {"type": "wait", "score": 0, "detail": "Dati insufficienti su candele chiuse", "conditions": []}

    macd_line = []
    for end in range(slow_period, len(prices_list) + 1):
        segment = prices_list[:end]
        fast = ema(segment, CONFIG["macd_fast"])
        slow = ema(segment, slow_period)
        if fast is not None and slow is not None:
            macd_line.append(fast - slow)

    signal_line = ema_values(macd_line, signal_period)
    if len(macd_line) < signal_period + 2 or len(signal_line) < 2:
        return {"type": "wait", "score": 0, "detail": "Dati insufficienti su candele chiuse", "conditions": []}

    macd_prev = macd_line[-2]
    macd_cur = macd_line[-1]
    signal_prev = signal_line[-2]
    signal_cur = signal_line[-1]
    hist_prev = macd_prev - signal_prev
    hist_cur = macd_cur - signal_cur

    cross_up = macd_prev <= signal_prev and macd_cur > signal_cur
    cross_down = macd_prev >= signal_prev and macd_cur < signal_cur
    above_zero = macd_cur > 0

    conditions = [
        {
            "name": f"MACD({CONFIG['macd_fast']},{CONFIG['macd_slow']})",
            "value": f"{macd_cur:.4f}",
            "ok_buy": above_zero,
            "ok_sell": not above_zero,
        },
        {
            "name": f"Segnale({signal_period})",
            "value": f"{signal_cur:.4f}",
            "ok_buy": hist_cur > 0,
            "ok_sell": hist_cur < 0,
        },
        {
            "name": "Cross",
            "value": "bullish" if cross_up else "bearish" if cross_down else "nessuno",
            "ok_buy": cross_up,
            "ok_sell": cross_down,
        },
    ]

    state["indicators"] = {
        "macd": round(macd_cur, 6),
        "signal": round(signal_cur, 6),
        "signal_prev": round(signal_prev, 6),
        "histogram": round(hist_cur, 6),
        "histogram_prev": round(hist_prev, 6),
    }

    if cross_up and above_zero:
        return {"type": "buy", "score": 85, "detail": "MACD cross bullish sopra zero su candela chiusa", "conditions": conditions}
    if cross_up:
        return {"type": "buy", "score": 60, "detail": "MACD cross bullish sotto zero su candela chiusa", "conditions": conditions}
    if cross_down and not above_zero:
        return {"type": "sell", "score": 85, "detail": "MACD cross bearish sotto zero su candela chiusa", "conditions": conditions}
    if cross_down:
        return {"type": "sell", "score": 60, "detail": "MACD cross bearish sopra zero su candela chiusa", "conditions": conditions}
    return {"type": "wait", "score": 0, "detail": "Nessun cross MACD su candela chiusa", "conditions": conditions}


def signal_ichi() -> dict[str, Any]:
    prices_list = list(closed_prices)
    need = CONFIG["ichi_senkou_b"] + 5
    if len(prices_list) < need:
        return {"type": "wait", "score": 0, "detail": f"Servono {need} candele chiuse (hai {len(prices_list)})", "conditions": []}

    tenkan = (max(prices_list[-CONFIG["ichi_tenkan"]:]) + min(prices_list[-CONFIG["ichi_tenkan"]:])) / 2
    kijun = (max(prices_list[-CONFIG["ichi_kijun"]:]) + min(prices_list[-CONFIG["ichi_kijun"]:])) / 2
    span_a = (tenkan + kijun) / 2
    span_b = (max(prices_list[-CONFIG["ichi_senkou_b"]:]) + min(prices_list[-CONFIG["ichi_senkou_b"]:])) / 2
    current = prices_list[-1]

    above_cloud = current > max(span_a, span_b)
    below_cloud = current < min(span_a, span_b)
    tk_bull = tenkan > kijun
    cloud_bull = span_a > span_b

    conditions = [
        {"name": "Close vs Kumo", "value": "sopra" if above_cloud else "sotto" if below_cloud else "dentro", "ok_buy": above_cloud, "ok_sell": below_cloud},
        {"name": "Tenkan vs Kijun", "value": f"{tenkan:.2f} {'>' if tk_bull else '<'} {kijun:.2f}", "ok_buy": tk_bull, "ok_sell": not tk_bull},
        {"name": "Nuvola Kumo", "value": "bullish" if cloud_bull else "bearish", "ok_buy": cloud_bull, "ok_sell": not cloud_bull},
    ]

    state["indicators"] = {
        "tenkan": round(tenkan, 4),
        "kijun": round(kijun, 4),
        "span_a": round(span_a, 4),
        "span_b": round(span_b, 4),
    }

    score_buy = sum([above_cloud, tk_bull, cloud_bull])
    score_sell = sum([below_cloud, not tk_bull, not cloud_bull])
    if score_buy >= 2:
        return {"type": "buy", "score": score_buy * 30, "detail": "Ichimoku bullish su candela chiusa", "conditions": conditions}
    if score_sell >= 2:
        return {"type": "sell", "score": score_sell * 30, "detail": "Ichimoku bearish su candela chiusa", "conditions": conditions}
    return {"type": "wait", "score": 0, "detail": "Segnale Ichimoku non chiaro su candela chiusa", "conditions": conditions}


def get_signal() -> dict[str, Any]:
    strategy_map = {
        "ema": signal_ema,
        "bb": signal_bb,
        "macd": signal_macd,
        "ichi": signal_ichi,
    }
    try:
        return strategy_map.get(CONFIG["strategy"], signal_ema)()
    except Exception as exc:
        logger.error(f"Errore nel calcolo segnale: {exc}")
        _append_error(f"Errore segnale: {exc}")
        return {"type": "wait", "score": 0, "detail": f"Errore: {exc}", "conditions": []}


def _entry_fill(signal_price: float, direction: int) -> float:
    slip = CONFIG["slippage_pct"]
    return signal_price * (1 + slip) if direction == 1 else signal_price * (1 - slip)


def _exit_fill(signal_price: float, direction: int) -> float:
    slip = CONFIG["slippage_pct"]
    return signal_price * (1 - slip) if direction == 1 else signal_price * (1 + slip)


def _position_pnl(pos: dict[str, Any], live_price: float) -> dict[str, float]:
    exit_fill = _exit_fill(live_price, pos["dir"])
    pnl_gross = (exit_fill - pos["entry_price_fill"]) * pos["size"] * pos["dir"]
    fee_exit = abs(exit_fill * pos["size"]) * pos["fee_pct"]
    fee_total = pos["fee_entry"] + fee_exit
    pnl_net = pnl_gross - fee_total
    return {
        "exit_fill": exit_fill,
        "pnl_gross": pnl_gross,
        "fee_exit": fee_exit,
        "fee_total": fee_total,
        "pnl_net": pnl_net,
    }


async def open_position(signal: dict[str, Any], signal_price: float, candle_time: Any = None) -> None:
    if state["open_position"]:
        return
    if signal.get("type") not in {"buy", "sell"}:
        return
    block_reason = _entry_block_reason()
    if block_reason:
        logger.warning(f"Entry bloccata: {block_reason}")
        state["signal"] = {
            "type": "wait",
            "score": 0,
            "detail": f"Entry bloccata: {block_reason}",
            "conditions": [],
        }
        return

    direction = 1 if signal["type"] == "buy" else -1
    entry_fill = _entry_fill(signal_price, direction)
    sl_distance = atr_approx(entry_fill) * CONFIG["sl_atr_mult"]
    if sl_distance <= 0:
        logger.warning("Trade ignorato: distanza stop non valida")
        return

    stop_loss = entry_fill - sl_distance * direction
    take_profit = entry_fill + (sl_distance * CONFIG["tp_ratio"]) * direction
    per_unit_risk = abs(entry_fill - stop_loss)
    risk_amount = state["capital"] * CONFIG["risk_pct"] / 100
    max_notional = state["capital"] * CONFIG["max_notional_pct"]
    size_by_risk = risk_amount / per_unit_risk if per_unit_risk > 0 else 0
    size_by_capital = max_notional / entry_fill if entry_fill > 0 else 0
    final_size = min(size_by_risk, size_by_capital)
    notional = final_size * entry_fill

    if (
        final_size <= 0
        or per_unit_risk <= 0
        or max_notional < MIN_NOTIONAL_USDT
        or notional < MIN_NOTIONAL_USDT
        or state["capital"] <= 0
    ):
        logger.warning("Trade ignorato: sizing non valido o notional troppo piccolo")
        return

    fee_entry = abs(entry_fill * final_size) * CONFIG["fee_pct"]
    position = {
        "direction": "LONG" if direction == 1 else "SHORT",
        "dir": direction,
        "entry": round(entry_fill, 6),
        "entry_price_signal": round(signal_price, 6),
        "entry_price_fill": round(entry_fill, 6),
        "sl": round(stop_loss, 6),
        "sl_initial": round(stop_loss, 6),
        "tp": round(take_profit, 6),
        "size": round(final_size, 8),
        "open_time": _now_iso(),
        "signal_candle_time": candle_time,
        "strategy": CONFIG["strategy"],
        "unrealized": 0.0,
        "unrealized_gross": 0.0,
        "trailing": CONFIG["trailing_stop"],
        "fee_entry": round(fee_entry, 8),
        "fee_pct": CONFIG["fee_pct"],
        "slippage_pct": CONFIG["slippage_pct"],
        "risk_amount": round(risk_amount, 6),
        "notional": round(notional, 6),
        "size_by_risk": round(size_by_risk, 8),
        "size_by_capital": round(size_by_capital, 8),
        "final_size": round(final_size, 8),
        "max_notional_pct": CONFIG["max_notional_pct"],
        "paper_short_simulation": direction == -1,
    }

    state["open_position"] = position
    _add_log("open", position)
    _save_paper_state()
    logger.info(
        f"[PAPER] {position['direction']} @ {entry_fill:.4f} "
        f"(signal {signal_price:.4f}) | SL {stop_loss:.4f} | TP {take_profit:.4f} | Size {final_size:.6f}"
    )


async def check_position(live_price: float) -> None:
    pos = state["open_position"]
    if not pos:
        return

    pnl = _position_pnl(pos, live_price)
    pos["unrealized"] = round(pnl["pnl_net"], 4)
    pos["unrealized_gross"] = round(pnl["pnl_gross"], 4)
    pos["exit_price_live_est"] = round(pnl["exit_fill"], 6)
    pos["fee_exit_est"] = round(pnl["fee_exit"], 8)
    if _refresh_risk_guard():
        _save_paper_state()

    save_needed = False
    if pos.get("trailing", False):
        sl_distance = abs(pos["entry_price_fill"] - pos["sl_initial"])
        if pos["dir"] == 1:
            new_sl = round(live_price - sl_distance, 6)
            if new_sl > pos["sl"]:
                pos["sl"] = new_sl
                save_needed = True
        else:
            new_sl = round(live_price + sl_distance, 6)
            if new_sl < pos["sl"]:
                pos["sl"] = new_sl
                save_needed = True

    hit_sl = (pos["dir"] == 1 and live_price <= pos["sl"]) or (pos["dir"] == -1 and live_price >= pos["sl"])
    hit_tp = (pos["dir"] == 1 and live_price >= pos["tp"]) or (pos["dir"] == -1 and live_price <= pos["tp"])

    if hit_sl or hit_tp:
        await close_position(live_price, "TP" if hit_tp else "SL")
    elif save_needed:
        _save_paper_state()


async def close_position(signal_price: float, reason: str) -> None:
    pos = state["open_position"]
    if not pos:
        return

    pnl = _position_pnl(pos, signal_price)
    state["capital"] += pnl["pnl_net"]

    metrics = state["metrics"]
    metrics["trades"] += 1
    if pnl["pnl_net"] > 0:
        metrics["wins"] += 1
    metrics["total_pnl"] = state["capital"] - state["init_capital"]
    metrics["total_pnl_pct"] = metrics["total_pnl"] / state["init_capital"] * 100
    metrics["win_rate"] = metrics["wins"] / metrics["trades"] * 100 if metrics["trades"] else 0
    if state["capital"] > metrics["peak_capital"]:
        metrics["peak_capital"] = state["capital"]
    drawdown = (metrics["peak_capital"] - state["capital"]) / metrics["peak_capital"] * 100
    if drawdown > metrics["max_dd"]:
        metrics["max_dd"] = drawdown

    state["equity_curve"].append(round(state["capital"], 2))
    if len(state["equity_curve"]) > 500:
        state["equity_curve"] = state["equity_curve"][-500:]

    trade_record = {
        "time": _now_iso(),
        "direction": pos["direction"],
        "entry": pos["entry_price_fill"],
        "exit": round(pnl["exit_fill"], 6),
        "size": pos["size"],
        "reason": reason,
        "strategy": pos["strategy"],
        "entry_price_signal": pos["entry_price_signal"],
        "entry_price_fill": pos["entry_price_fill"],
        "exit_price_signal": round(signal_price, 6),
        "exit_price_fill": round(pnl["exit_fill"], 6),
        "fee_entry": round(pos["fee_entry"], 8),
        "fee_exit": round(pnl["fee_exit"], 8),
        "fee_total": round(pnl["fee_total"], 8),
        "slippage_pct": pos["slippage_pct"],
        "fee_pct": pos["fee_pct"],
        "pnl_gross": round(pnl["pnl_gross"], 4),
        "pnl_net": round(pnl["pnl_net"], 4),
        "pnl": round(pnl["pnl_net"], 4),
        "risk_amount": pos["risk_amount"],
        "notional": pos["notional"],
        "size_by_risk": pos["size_by_risk"],
        "size_by_capital": pos["size_by_capital"],
        "final_size": pos["final_size"],
        "max_notional_pct": pos["max_notional_pct"],
    }
    state["trades"].insert(0, trade_record)
    if len(state["trades"]) > 100:
        state["trades"] = state["trades"][:100]

    log_pos = deepcopy(pos)
    log_pos["exit"] = trade_record["exit"]
    _add_log("close", log_pos, pnl["pnl_net"], reason)
    logger.info(f"[{reason}] Chiuso {pos['direction']} | P&L netto: {pnl['pnl_net']:+.4f} USDT")
    state["open_position"] = None
    _refresh_risk_guard()
    _save_trades_json()
    _save_paper_state()


def _save_trades_json() -> None:
    try:
        data = {
            "symbol": state["symbol"],
            "strategy": state["strategy"],
            "paper_mode": state["paper_mode"],
            "paper_only_build": True,
            "capital": state["capital"],
            "metrics": state["metrics"],
            "trades": state["trades"],
            "equity_curve": state["equity_curve"],
            "saved_at": _now_iso(),
        }
        tmp = TRADES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(TRADES_FILE)
    except Exception as exc:
        logger.warning(f"Errore salvataggio trades.json: {exc}")


def _save_paper_state() -> None:
    try:
        data = {
            "paper_only_build": True,
            "live_trading_enabled": False,
            "exchange_order_blocked": True,
            "symbol": state["symbol"],
            "strategy": state["strategy"],
            "capital": state["capital"],
            "last_update": _now_iso(),
            "kill_switch_active": state["kill_switch_active"],
            "kill_switch_reason": state["kill_switch_reason"],
            "kill_switch_time": state["kill_switch_time"],
            "trading_halted": state["trading_halted"],
            "trading_halt_reason": state["trading_halt_reason"],
            "risk_guard": state["risk_guard"],
            "config": {
                "strategy": CONFIG["strategy"],
                "risk_pct": CONFIG["risk_pct"],
                "tp_ratio": CONFIG["tp_ratio"],
                "sl_atr_mult": CONFIG["sl_atr_mult"],
                "fee_pct": CONFIG["fee_pct"],
                "slippage_pct": CONFIG["slippage_pct"],
                "max_notional_pct": CONFIG["max_notional_pct"],
                "daily_loss_limit_pct": CONFIG["daily_loss_limit_pct"],
                "daily_loss_limit_enabled": CONFIG["daily_loss_limit_enabled"],
            },
            "open_position": state["open_position"],
        }
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception as exc:
        logger.warning(f"Errore salvataggio state.json: {exc}")


def _load_existing_trades() -> None:
    if not TRADES_FILE.exists():
        return
    try:
        data = json.loads(TRADES_FILE.read_text(encoding="utf-8"))
        if data.get("symbol") == state["symbol"]:
            state["trades"] = data.get("trades", [])
            state["equity_curve"] = data.get("equity_curve", [state["init_capital"]])
            saved_metrics = data.get("metrics", {})
            state["metrics"].update({k: v for k, v in saved_metrics.items() if k in state["metrics"]})
            state["capital"] = float(data.get("capital", state["capital"]))
            logger.success(f"Storico ripristinato: {len(state['trades'])} trade precedenti")
        else:
            logger.info("Storico ignorato: simbolo diverso")
    except Exception as exc:
        logger.warning(f"Impossibile caricare trades.json: {exc}")
        _append_error("trades.json corrotto o non leggibile; storico ignorato")


def _load_paper_state() -> None:
    if not STATE_FILE.exists():
        _reset_daily_guard()
        return
    has_saved_guard = False
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if data.get("symbol") != state["symbol"]:
            logger.info("state.json ignorato: simbolo diverso")
            _reset_daily_guard()
            return
        state["capital"] = float(data.get("capital", state["capital"]))
        saved_guard = data.get("risk_guard")
        if isinstance(saved_guard, dict):
            has_saved_guard = True
            state["risk_guard"].update(saved_guard)
        state["kill_switch_active"] = bool(data.get("kill_switch_active", state["kill_switch_active"]))
        state["kill_switch_reason"] = data.get("kill_switch_reason", state["kill_switch_reason"])
        state["kill_switch_time"] = data.get("kill_switch_time", state["kill_switch_time"])
        position = data.get("open_position")
        if isinstance(position, dict) and position.get("direction") in {"LONG", "SHORT"}:
            state["open_position"] = position
            state["log"].insert(0, {
                "time": datetime.now().strftime("%H:%M:%S"),
                "action": "restore",
                "direction": position["direction"],
                "price": position.get("entry_price_fill", position.get("entry", 0)),
                "pnl": 0,
                "reason": "RESTORED",
            })
            logger.success(f"Posizione paper ripristinata: {position['direction']} @ {position.get('entry_price_fill')}")
    except Exception as exc:
        logger.warning(f"state.json corrotto o non leggibile: {exc}")
        _append_error("state.json corrotto; riparto flat")
        state["open_position"] = None
    finally:
        if has_saved_guard:
            _refresh_risk_guard()
        else:
            _reset_daily_guard()


def _add_log(action: str, pos: dict[str, Any], pnl: float = 0.0, reason: str = "") -> None:
    price = pos.get("entry_price_fill", pos.get("entry", 0)) if action == "open" else pos.get("exit", 0)
    entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "action": action,
        "direction": pos["direction"],
        "price": price,
        "pnl": round(pnl, 4),
        "reason": reason,
    }
    state["log"].insert(0, entry)
    if len(state["log"]) > 50:
        state["log"] = state["log"][:50]


def _update_closed_chart() -> None:
    state["candles"] = [
        {"t": candle["t"], "o": candle["o"], "h": candle["h"], "l": candle["l"], "c": candle["c"], "v": candle["v"]}
        for candle in list(closed_candles)[-60:]
    ]


async def _handle_closed_candle(kline: dict[str, Any]) -> None:
    candle_time = int(kline["t"])
    if state["last_closed_candle_time"] == candle_time:
        return

    close_price = float(kline["c"])
    close_volume = float(kline["v"])
    candle = {
        "t": candle_time,
        "o": float(kline["o"]),
        "h": float(kline["h"]),
        "l": float(kline["l"]),
        "c": close_price,
        "v": close_volume,
    }

    closed_prices.append(close_price)
    closed_volumes.append(close_volume)
    closed_candles.append(candle)
    state["last_closed_candle_time"] = candle_time
    _update_closed_chart()
    if _refresh_risk_guard():
        _save_paper_state()

    if state["last_signal_candle_time"] == candle_time:
        return
    state["last_signal_candle_time"] = candle_time

    signal = get_signal()
    state["signal"] = signal

    prices_list = list(closed_prices)
    mtf = mtf_trend()
    whipsaw = is_whipsaw(prices_list)
    state["filters"] = {"whipsaw": whipsaw, "mtf_trend": mtf}

    can_enter = (
        not state["open_position"]
        and not state["trading_halted"]
        and signal["type"] in {"buy", "sell"}
        and signal["score"] >= 60
        and not whipsaw
        and (
            mtf == "neutral"
            or (signal["type"] == "buy" and mtf == "bull")
            or (signal["type"] == "sell" and mtf == "bear")
        )
    )
    if can_enter:
        await open_position(signal, close_price, candle_time)


def _update_live_price(price: float) -> None:
    previous = state["last_live_price"] or state["price"] or price
    state["last_live_price"] = round(price, 6)
    state["price"] = round(price, 6)
    state["price_change"] = round((price - previous) / previous * 100, 4) if previous else 0.0
    now = _now_iso()
    state["last_price_update_time"] = now
    state["last_update"] = now
    state["price_source"] = "binance_websocket_live"
    state["strategy_source"] = "closed_candles_only"
    if _refresh_risk_guard():
        _save_paper_state()


async def _stream_klines_once() -> None:
    global client
    try:
        client = await AsyncClient.create()
        socket_manager = BinanceSocketManager(client)

        logger.info(f"Caricamento candele storiche chiuse per {state['symbol']}...")
        now_ms = int(time.time() * 1000)
        klines = await client.get_klines(symbol=state["symbol"], interval="1m", limit=200)
        for item in klines:
            if int(item[6]) > now_ms:
                continue
            close_price = float(item[4])
            closed_prices.append(close_price)
            closed_volumes.append(float(item[5]))
            closed_candles.append({
                "t": int(item[0]),
                "o": float(item[1]),
                "h": float(item[2]),
                "l": float(item[3]),
                "c": close_price,
                "v": float(item[5]),
            })

        klines_5m = await client.get_klines(symbol=state["symbol"], interval="5m", limit=100)
        for item in klines_5m:
            if int(item[6]) <= now_ms:
                closed_prices_5m.append(float(item[4]))

        if closed_prices:
            _update_live_price(closed_prices[-1])
            state["last_closed_candle_time"] = closed_candles[-1]["t"]
            state["signal"] = get_signal()
            _update_closed_chart()

        kline_1m = socket_manager.kline_socket(symbol=state["symbol"], interval="1m")
        kline_5m = socket_manager.kline_socket(symbol=state["symbol"], interval="5m")
        await kline_1m.__aenter__()
        await kline_5m.__aenter__()
        state["running"] = True
        state["websocket_status"] = "connected"
        state["websocket_connected_at"] = _now_iso()
        state["next_reconnect_time"] = None
        state["reconnect_delay_sec"] = 0
        state["last_websocket_error"] = None
        logger.success(f"WebSocket connesso: {state['symbol']} 1m live + 5m closed")

        async def read_5m() -> None:
            while state["running"]:
                try:
                    msg_5m = await asyncio.wait_for(kline_5m.recv(), timeout=60)
                    kline = (msg_5m or {}).get("k", {})
                    if kline.get("x"):
                        closed_prices_5m.append(float(kline["c"]))
                except Exception:
                    continue

        read_5m_task = asyncio.create_task(read_5m())

        try:
            while state["running"]:
                try:
                    message = await asyncio.wait_for(kline_1m.recv(), timeout=30)
                except asyncio.TimeoutError:
                    logger.warning("WebSocket timeout; attendo nuovo tick")
                    continue

                kline = (message or {}).get("k", {})
                if not kline:
                    continue

                live_price = float(kline["c"])
                _update_live_price(live_price)
                await check_position(live_price)

                if kline.get("x") is True:
                    await _handle_closed_candle(kline)
        finally:
            state["running"] = False
            read_5m_task.cancel()
            with suppress(asyncio.CancelledError):
                await read_5m_task
            await kline_1m.__aexit__(None, None, None)
            await kline_5m.__aexit__(None, None, None)

    except Exception as exc:
        logger.error(f"Errore WebSocket: {exc}")
        _append_error(f"Errore WebSocket: {exc}")
        state["running"] = False
        state["websocket_status"] = "error"
        raise
    finally:
        if client:
            await client.close_connection()


async def stream_klines() -> None:
    delay = CONFIG["reconnect_initial_delay_sec"]
    while not state.get("shutdown_requested"):
        try:
            state["running"] = False
            state["websocket_status"] = "connecting"
            state["next_reconnect_time"] = None
            await _stream_klines_once()
            if state.get("shutdown_requested"):
                break
            raise RuntimeError("WebSocket chiuso in modo inatteso")
        except asyncio.CancelledError:
            state["running"] = False
            state["websocket_status"] = "stopped"
            raise
        except Exception as exc:
            if state.get("shutdown_requested"):
                break
            state["running"] = False
            state["websocket_status"] = "reconnecting"
            state["last_websocket_error"] = str(exc)
            state["reconnect_count"] += 1
            state["reconnect_delay_sec"] = delay
            state["last_reconnect_time"] = _now_iso()
            state["next_reconnect_time"] = (datetime.now() + timedelta(seconds=delay)).isoformat()
            logger.warning(f"Reconnect WebSocket tra {delay:.1f}s: {exc}")
            await asyncio.sleep(delay)
            delay = min(delay * 2, CONFIG["reconnect_max_delay_sec"])
    state["running"] = False
    state["websocket_status"] = "stopped"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global websocket_task
    state["shutdown_requested"] = False
    _load_existing_trades()
    _load_paper_state()
    websocket_task = asyncio.create_task(stream_klines())
    logger.info("Bot avviato [PAPER-ONLY]")
    logger.info(f"Log paper salvati in: {LOG_DIR}")
    try:
        yield
    finally:
        state["shutdown_requested"] = True
        state["running"] = False
        if websocket_task:
            websocket_task.cancel()
            with suppress(asyncio.CancelledError):
                await websocket_task
            websocket_task = None


app = FastAPI(title="Scalping Bot API - Paper Only", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8000", "http://localhost:8000"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.get("/api/state")
async def get_state() -> JSONResponse:
    state["config"] = _public_config()
    return JSONResponse(content=state)


@app.post("/api/config")
async def update_config(body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON body non valido")
    if state["open_position"] and "capital" in body:
        raise HTTPException(status_code=400, detail="Non modificare capital mentre una posizione e' aperta")

    try:
        new_config = _validated_config_update(body)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    CONFIG.clear()
    CONFIG.update(new_config)
    _sync_state_config()

    if "capital" in body:
        state["capital"] = CONFIG["capital"]
        if state["metrics"]["trades"] == 0:
            state["init_capital"] = CONFIG["capital"]
            state["equity_curve"] = [CONFIG["capital"]]
            state["metrics"]["peak_capital"] = CONFIG["capital"]
            state["metrics"]["total_pnl"] = 0.0
            state["metrics"]["total_pnl_pct"] = 0.0
        _reset_daily_guard()
    else:
        _refresh_risk_guard()

    _save_paper_state()
    return {"ok": True, "config": _public_config()}


@app.post("/api/kill-switch")
async def update_kill_switch(body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    payload = body or {}
    unknown = sorted(set(payload) - {"enabled", "reason", "close_position"})
    if unknown:
        raise HTTPException(status_code=400, detail=f"Parametri sconosciuti: {', '.join(unknown)}")

    try:
        enabled = _coerce_bool_input(payload.get("enabled", True), "enabled")
        should_close_position = _coerce_bool_input(payload.get("close_position", False), "close_position")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    reason = str(payload.get("reason") or "manual_kill_switch").strip()[:120]
    closed_position = False

    if enabled:
        state["kill_switch_active"] = True
        state["kill_switch_reason"] = reason
        state["kill_switch_time"] = _now_iso()
        state["signal"] = {
            "type": "wait",
            "score": 0,
            "detail": f"Kill switch attivo: {reason}",
            "conditions": [],
        }
        _add_log("halt", {"direction": "SYSTEM", "entry": 0}, 0, "KILL_SWITCH")
        if should_close_position and state["open_position"]:
            live_price = state["last_live_price"] or state["price"]
            if live_price <= 0:
                raise HTTPException(status_code=409, detail="Prezzo live non disponibile per chiudere la posizione paper")
            await close_position(float(live_price), "KILL_SWITCH")
            closed_position = True
    else:
        state["kill_switch_active"] = False
        state["kill_switch_reason"] = None
        state["kill_switch_time"] = None
        _add_log("resume", {"direction": "SYSTEM", "entry": 0}, 0, "KILL_SWITCH_RESET")

    _refresh_risk_guard()
    _save_paper_state()
    return {
        "ok": True,
        "kill_switch_active": state["kill_switch_active"],
        "trading_halted": state["trading_halted"],
        "trading_halt_reason": state["trading_halt_reason"],
        "closed_position": closed_position,
        "risk_guard": state["risk_guard"],
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    candidates = [
        BOT_DIR.parent / "dashboard" / "index.html",
        BOT_DIR / "dashboard" / "index.html",
        BOT_DIR / "index.html",
    ]
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8")
    return HTMLResponse("<h2>index.html non trovato</h2>", status_code=404)


@app.get("/api/trades")
async def get_trades() -> JSONResponse:
    return JSONResponse(content={
        "symbol": state["symbol"],
        "strategy": state["strategy"],
        "paper_mode": state["paper_mode"],
        "paper_only_build": True,
        "capital": state["capital"],
        "metrics": state["metrics"],
        "trades": state["trades"],
        "saved_at": _now_iso(),
    })


@app.get("/api/trades/download")
async def download_trades():
    from fastapi.responses import FileResponse

    if TRADES_FILE.exists():
        return FileResponse(
            path=str(TRADES_FILE),
            filename=f"trades_{state['symbol']}_{datetime.now().strftime('%Y%m%d_%H%M')}.json",
            media_type="application/json",
        )
    return JSONResponse({"error": "Nessun trade salvato ancora"}, status_code=404)


if __name__ == "__main__":
    uvicorn.run("bot:app", host=HOST, port=PORT, reload=False, log_level="info")
