"""Daily price bars from public exchange APIs. No API key, read-only."""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timezone

DAY = 86400
COINBASE = "https://api.exchange.coinbase.com"
KRAKEN = "https://api.kraken.com"
COINBASE_MAX_BARS = 300  # the API rejects ranges longer than this
PAUSE = 0.12             # seconds between pages, to stay polite with the public API


class DataError(Exception):
    """Prices are missing or do not look trustworthy; the bot must not trade on them."""


def http_json(url: str, retries: int = 3, timeout: int = 30):
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "autotrader/1.0"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode())
        except Exception as error:  # network hiccup: back off and retry
            if attempt == retries - 1:
                raise DataError(f"could not fetch {url}: {error}") from error
            time.sleep(2 ** attempt)


def day_of(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def ts_of(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def utc_midnight(now: float | None = None) -> int:
    now = time.time() if now is None else now
    return int(now) // DAY * DAY


def _sane(low: float, high: float, open_: float, close: float) -> bool:
    return 0 < low <= min(open_, close) and max(open_, close) <= high


def coinbase_daily(product: str, start: int, end: int, get=http_json, pause: float | None = None) -> dict:
    """{day_ts: (open, close)} for start..end inclusive. Broken bars are dropped."""
    pause = PAUSE if pause is None else pause
    bars: dict[int, tuple[float, float]] = {}
    t = start
    while t <= end:
        last = min(t + (COINBASE_MAX_BARS - 1) * DAY, end)
        rows = get(f"{COINBASE}/products/{product}/candles?granularity={DAY}"
                   f"&start={day_of(t)}T00:00:00Z&end={day_of(last)}T00:00:00Z")
        if not isinstance(rows, list):
            raise DataError(f"coinbase {product}: unexpected reply {str(rows)[:120]}")
        for ts, low, high, open_, close, *_ in rows:
            if _sane(float(low), float(high), float(open_), float(close)):
                bars[int(ts)] = (float(open_), float(close))
        t = last + DAY
        if t <= end and pause:
            time.sleep(pause)
    return bars


def kraken_daily(pair: str, get=http_json) -> dict:
    """{day_ts: (open, close)} for roughly the last 720 days (second opinion on prices)."""
    reply = get(f"{KRAKEN}/0/public/OHLC?pair={pair}&interval=1440")
    if not isinstance(reply, dict) or reply.get("error") or "result" not in reply:
        raise DataError(f"kraken {pair}: unexpected reply {str(reply)[:120]}")
    rows = next(v for k, v in reply["result"].items() if k != "last")
    return {int(r[0]): (float(r[1]), float(r[4])) for r in rows
            if _sane(float(r[3]), float(r[2]), float(r[1]), float(r[4]))}


def align(bars_by_symbol: dict, end: int | None = None) -> dict:
    """Put every asset on one daily timeline. A missing day repeats the previous close."""
    start = max(min(bars) for bars in bars_by_symbol.values())
    stop = min(max(bars) for bars in bars_by_symbol.values()) if end is None else end
    if stop < start:
        raise DataError("no overlapping history")
    days = list(range(start, stop + DAY, DAY))
    series = {"days": days, "open": {}, "close": {}, "filled": 0}
    for symbol, bars in bars_by_symbol.items():
        opens, closes = [], []
        earlier = [d for d in bars if d < start]
        previous = bars[max(earlier)][1] if earlier else None
        for day in days:
            if day in bars:
                open_, close = bars[day]
            elif previous is None:
                raise DataError(f"{symbol}: no price on or before {day_of(day)}")
            else:
                open_ = close = previous
                series["filled"] += 1
            opens.append(open_)
            closes.append(close)
            previous = close
        series["open"][symbol], series["close"][symbol] = opens, closes
    return series


def load_bars(cfg: dict, start: int, end: int, get=http_json) -> dict:
    return {asset["symbol"]: coinbase_daily(asset["coinbase"], start, end, get)
            for asset in cfg["universe"]}


def load_history(cfg: dict, now: float | None = None, get=http_json) -> dict:
    """All completed daily bars from config.history_start to yesterday, aligned."""
    last_done = utc_midnight(now) - DAY
    bars = load_bars(cfg, ts_of(cfg["history_start"]), last_done, get)
    for symbol, b in bars.items():
        if not b:
            raise DataError(f"no price history for {symbol}")
    return align(bars, end=min(last_done, *(max(b) for b in bars.values())))
