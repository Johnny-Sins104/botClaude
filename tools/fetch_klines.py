"""
Scarica e cachea klines storiche da Binance (1m e 5m).

Uso:
    python tools/fetch_klines.py [SYMBOL] [GIORNI]
    python tools/fetch_klines.py BTCUSDT 7

Output:
    logs/klines_cache_BTCUSDT_1m.json
    logs/klines_cache_BTCUSDT_5m.json

Regole:
- Paginazione: max 1000 candele per richiesta, loop su startTime.
- L'ultima candela REST può essere ancora aperta: viene scartata.
- Valida continuità: logga warning se ci sono gap temporali tra candele.
- Idempotente: se la cache esiste e copre il periodo richiesto, non ri-scarica.
"""

import asyncio
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from binance import AsyncClient
from dotenv import load_dotenv

BOT_DIR = Path(__file__).parent.parent.resolve()
LOG_DIR = BOT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
load_dotenv(BOT_DIR / ".env")


def _kline_row_to_candle(row: list) -> dict:
    """Stesso formato di bot._kline_row_to_candle per coerenza."""
    return {
        "t": int(row[0]),   # open time ms
        "T": int(row[6]),   # close time ms
        "o": float(row[1]),
        "h": float(row[2]),
        "l": float(row[3]),
        "c": float(row[4]),
        "v": float(row[5]),
    }


def _validate_continuity(candles: list, interval_ms: int, label: str) -> None:
    """Controlla che non ci siano gap tra candele consecutive."""
    gaps = []
    for i in range(1, len(candles)):
        expected = candles[i - 1]["T"] + 1
        actual = candles[i]["t"]
        if actual > expected + interval_ms:
            gaps.append((i, candles[i - 1]["T"], candles[i]["t"]))
    if gaps:
        print(f"  [WARN] {label}: {len(gaps)} gap trovati:")
        for idx, prev_T, next_t in gaps[:5]:
            print(f"    candela {idx}: gap {(next_t - prev_T) / 1000:.0f}s")
    else:
        print(f"  [OK] {label}: nessun gap")


async def fetch_klines(symbol: str, interval: str, days: int) -> list[dict]:
    """Scarica klines con paginazione, scarta l'ultima (aperta), valida."""
    interval_ms = {"1m": 60_000, "5m": 300_000}.get(interval, 60_000)
    limit = 1000
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 60 * 60 * 1000

    print(f"  Scarico {symbol} {interval} — {days} giorni (da {datetime.fromtimestamp(start_ms/1000):%Y-%m-%d})")

    client = await AsyncClient.create()
    all_rows = []
    cursor = start_ms
    try:
        while cursor < end_ms:
            rows = await client.get_klines(
                symbol=symbol, interval=interval,
                startTime=cursor, endTime=end_ms, limit=limit,
            )
            if not rows:
                break
            all_rows.extend(rows)
            last_open = int(rows[-1][0])
            cursor = last_open + interval_ms
            if len(rows) < limit:
                break
            await asyncio.sleep(0.1)  # rate limit cortese
    finally:
        await client.close_connection()

    if not all_rows:
        return []

    # Scarta candela aperta finale (l'ultima REST può non essere chiusa)
    closed_rows = all_rows[:-1] if len(all_rows) > 1 else all_rows
    candles = [_kline_row_to_candle(r) for r in closed_rows]

    _validate_continuity(candles, interval_ms, f"{symbol} {interval}")
    print(f"  Scaricate {len(candles)} candele chiuse {interval}")
    return candles


def _cache_path(symbol: str, interval: str) -> Path:
    return LOG_DIR / f"klines_cache_{symbol}_{interval}.json"


def _is_cache_fresh(path: Path, days: int) -> bool:
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        candles = data.get("candles", [])
        if not candles:
            return False
        first_t = candles[0]["t"] / 1000
        last_t = candles[-1]["T"] / 1000
        required_start = time.time() - days * 86400
        # Accetta se copre almeno il 90% del periodo richiesto
        coverage = (last_t - first_t) / (days * 86400)
        return first_t <= required_start * 1.01 and coverage >= 0.9
    except Exception:
        return False


def save_cache(symbol: str, interval: str, candles: list) -> Path:
    path = _cache_path(symbol, interval)
    data = {
        "symbol": symbol,
        "interval": interval,
        "count": len(candles),
        "from": candles[0]["t"] if candles else None,
        "to": candles[-1]["T"] if candles else None,
        "saved_at": datetime.now().isoformat(),
        "candles": candles,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    print(f"  Cache salvata: {path.name} ({len(candles)} candele)")
    return path


def load_cache(symbol: str, interval: str) -> list[dict]:
    path = _cache_path(symbol, interval)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("candles", [])


async def main(symbol: str = "BTCUSDT", days: int = 7, force: bool = False) -> None:
    for interval in ("1m", "5m"):
        path = _cache_path(symbol, interval)
        if not force and _is_cache_fresh(path, days):
            existing = load_cache(symbol, interval)
            print(f"  Cache {interval} già fresca ({len(existing)} candele) — skip (usa --force per ri-scaricare)")
            continue
        candles = await fetch_klines(symbol, interval, days)
        if candles:
            save_cache(symbol, interval, candles)
        else:
            print(f"  [WARN] Nessuna candela scaricata per {interval}")


if __name__ == "__main__":
    args = sys.argv[1:]
    symbol_arg = args[0].upper() if len(args) > 0 else "BTCUSDT"
    days_arg = int(args[1]) if len(args) > 1 else 7
    force_arg = "--force" in args

    print(f"Fetch klines: {symbol_arg}, {days_arg} giorni")
    asyncio.run(main(symbol_arg, days_arg, force_arg))
    print("Done.")
