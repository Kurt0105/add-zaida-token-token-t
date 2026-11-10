"""The daily tick: prices -> trend lights -> safety checks -> orders -> journal.

Paper mode fills orders on pretend money with the same fee and slippage the
replay uses. Live mode is locked until a real broker adapter is added.
"""
from __future__ import annotations

import csv
import json
import os
import time
from datetime import datetime, timezone

from . import data
from .data import DAY, DataError, day_of
from .engine import Order, apply_fills, plan_orders, target_weights, trend_states, volatility

OK, SKIPPED, HALTED = 0, 1, 2
JOURNAL_FIELDS = ["run_utc", "signal_day", "mode", "equity", "just_hold", "cash", "holdings",
                  "lights", "orders", "fees", "lookback", "buffer", "note"]


class LiveTradingLocked(Exception):
    pass


class PaperBroker:
    """Pretend fills: same math as the replay, no real orders anywhere."""

    def __init__(self, costs: dict):
        self.costs = costs

    def execute(self, units: dict, cash: float, orders: list[Order], prices: dict):
        return apply_fills(units, cash, orders, prices, self.costs)


def broker_for(cfg: dict):
    if cfg["mode"] == "paper":
        return PaperBroker(cfg["costs"])
    raise LiveTradingLocked("Live trading is locked: no broker adapter is installed. "
                            "Set mode back to 'paper' in config.json.")


def load_json(path: str, default=None):
    if not os.path.exists(path):
        return default
    with open(path) as handle:
        return json.load(handle)


def save_json(path: str, value) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(path + ".tmp", path)


def load_params(cfg: dict, state_dir: str) -> dict:
    saved = load_json(os.path.join(state_dir, "params.json"))
    return {"lookback": saved["lookback"], "buffer": saved["buffer"]} if saved else dict(cfg["defaults"])


def _journal(state_dir: str, row: dict) -> None:
    path = os.path.join(state_dir, "journal.csv")
    os.makedirs(state_dir, exist_ok=True)
    is_new = not os.path.exists(path)
    with open(path, "a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=JOURNAL_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in JOURNAL_FIELDS})


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _dial(risk: dict) -> str:
    target = risk.get("vol_target")
    return f"bets sized to a {target:.0%} yearly swing" if target else "full-size bets"


def _status(state_dir: str, cfg: dict, book: dict, params: dict, lights: dict,
            values: dict, equity: float, just_hold: float, run_utc: str) -> None:
    start = book["start_capital"]
    gain = equity - start
    holding = " · ".join(f"{s} {_money(v)}" for s, v in sorted(values.items()))
    lamps = " · ".join(f"{s} {'GREEN' if on else 'red'}" for s, on in sorted(lights.items()))
    state = f"STOPPED - {book['halt_reason']}" if book["halted"] else "running"
    lines = [
        f"# Autotrader status ({cfg['mode'].upper()} money)",
        "",
        f"Updated {run_utc} UTC · prices through {book['last_signal_day']}",
        "",
        "| | |",
        "|---|---|",
        f"| Stack now | **{_money(equity)}** |",
        f"| Started with | {_money(start)} on {book['started']} |",
        f"| Profit / loss | {'+' if gain >= 0 else '-'}{_money(abs(gain))} ({gain / start:+.1%}) |",
        f"| If you had just held all {len(values)} | {_money(just_hold)} |",
        f"| Worst dip so far | {book['worst_drawdown']:.1%} (auto-stop at {cfg['risk']['max_drawdown_halt']:.0%}) |",
        f"| Holding | {holding} · cash {_money(book['cash'])} |",
        f"| Trend lights | {lamps} |",
        f"| Fees paid so far | {_money(book['fees_paid'])} |",
        f"| Daily runs | {book['ticks']} done, {book['missed_days']} missed |",
        f"| Settings | {params['lookback']}-day trend, {params['buffer']:.0%} buffer, "
        f"{_dial(cfg['risk'])} |",
        f"| Bot | {state} |",
        "",
    ]
    with open(os.path.join(state_dir, "STATUS.md"), "w") as handle:
        handle.write("\n".join(lines))


def resume(state_dir: str, now: float | None = None, log=print) -> int:
    """A human has reviewed the auto-stop: clear it and measure the next dip from here."""
    path = os.path.join(state_dir, "portfolio.json")
    book = load_json(path)
    if not book or not book["halted"]:
        log("Nothing to resume: the bot is not stopped.")
        return OK
    stamp = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
    book.update(halted=False, halt_reason="", peak_equity=book["cash"])  # stopped means all cash
    save_json(path, book)
    _journal(state_dir, {"run_utc": stamp.strftime("%Y-%m-%d %H:%M"), "mode": book["mode"],
                         "equity": f"{book['cash']:.2f}", "cash": f"{book['cash']:.2f}",
                         "note": "RESUMED by a human"})
    log("Resumed. The next daily run can trade again.")
    return OK


def tick(cfg: dict, state_dir: str, now: float | None = None, get=data.http_json, log=print) -> int:
    """One daily run. Returns OK, SKIPPED (prices not trustworthy) or HALTED (auto-stop fired)."""
    now = time.time() if now is None else now
    run_utc = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M")
    today = data.utc_midnight(now)
    signal_day = today - DAY  # the last fully finished daily bar
    broker = broker_for(cfg)
    symbols = [asset["symbol"] for asset in cfg["universe"]]
    book_path = os.path.join(state_dir, "portfolio.json")
    book = load_json(book_path) or {
        "mode": cfg["mode"], "start_capital": cfg["start_capital_usd"], "started": day_of(today),
        "cash": cfg["start_capital_usd"], "units": {s: 0.0 for s in symbols},
        "peak_equity": cfg["start_capital_usd"], "worst_drawdown": 0.0, "fees_paid": 0.0,
        "halted": False, "halt_reason": "", "last_signal_day": "", "ticks": 0, "missed_days": 0,
        "hold_units": None,
    }
    if book["mode"] != cfg["mode"]:
        raise LiveTradingLocked("portfolio.json was started in a different mode; refusing to mix them")
    if set(book["units"]) != set(symbols):
        raise ValueError("config universe no longer matches portfolio.json; migrate the state first")
    if book["halted"]:
        log(f"STOPPED earlier ({book['halt_reason']}). A human must review and reset. No trades.")
        return OK
    if book["last_signal_day"] == day_of(signal_day):
        log(f"Already ran for {day_of(signal_day)}. Nothing to do.")
        return OK
    params = load_params(cfg, state_dir)
    base = {"run_utc": run_utc, "signal_day": day_of(signal_day), "mode": cfg["mode"],
            "lookback": params["lookback"], "buffer": params["buffer"]}

    # 1. Prices, with a second opinion. Any doubt means no trading today.
    try:
        longest = max(params["lookback"], *cfg["tuning"]["grid"]["lookback"])
        bars = data.load_bars(cfg, signal_day - (longest + 400) * DAY, today, get)
        for asset in cfg["universe"]:
            symbol = asset["symbol"]
            if signal_day not in bars[symbol]:
                raise DataError(f"{symbol}: no finished bar for {day_of(signal_day)}")
            second = data.kraken_daily(asset["kraken"], get)
            if signal_day not in second:
                raise DataError(f"{symbol}: second price source has no bar for {day_of(signal_day)}")
            for day in (signal_day, today):  # the finished bar, and the live price if both have it
                if day in bars[symbol] and day in second:
                    gap = abs(bars[symbol][day][1] / second[day][1] - 1)
                    if gap > cfg["risk"]["max_price_gap"]:
                        raise DataError(f"{symbol}: price sources disagree by {gap:.1%} on {day_of(day)}")
        prices = {s: bars[s].get(today, bars[s][signal_day])[1] for s in symbols}
        series = data.align({s: {d: b for d, b in bars[s].items() if d <= signal_day} for s in symbols},
                            end=signal_day)
    except DataError as problem:
        log(f"SKIPPED: {problem}")
        _journal(state_dir, dict(base, note=f"SKIPPED: {problem}"))
        return SKIPPED

    # 2. Trend lights -> target weights (jumpier coins get smaller bets).
    lights = {s: trend_states(series["close"][s], params["lookback"], params["buffer"])[-1]
              for s in symbols}
    vols = {s: volatility(series["close"][s], cfg["risk"]["vol_window"])[-1] for s in symbols}
    weights = target_weights(lights, vols, cfg["risk"])

    # 3. Own up to any days the bot did not run.
    notes = []
    if book["last_signal_day"]:
        missed = (signal_day - data.ts_of(book["last_signal_day"])) // DAY - 1
        if missed > 0:
            book["missed_days"] += missed
            notes.append(f"missed {missed} daily run(s) before this one")

    # 4. Auto-stop: too deep a dip from the best level means sell everything and stop.
    units, cash = book["units"], book["cash"]
    equity = cash + sum(units[s] * prices[s] for s in symbols)
    book["peak_equity"] = max(book["peak_equity"], equity)
    dip = 1 - equity / book["peak_equity"]
    book["worst_drawdown"] = max(book["worst_drawdown"], dip)
    if dip >= cfg["risk"]["max_drawdown_halt"]:
        weights = {s: 0.0 for s in symbols}
        book["halted"] = True
        book["halt_reason"] = f"dipped {dip:.1%} from the peak on {day_of(today)}"
        notes.append(f"AUTO-STOP: {book['halt_reason']}")
    note = "; ".join(notes)

    # 5. Orders (the same planner the replay uses), then fills.
    orders = plan_orders(units, cash, prices, weights, cfg["costs"], cfg["execution"])
    units, cash, fees = broker.execute(units, cash, orders, prices)
    values = {s: units[s] * prices[s] for s in symbols}
    equity = cash + sum(values.values())

    # 6. "What if you had just held?" yardstick, bought once on day one.
    if book["hold_units"] is None:
        per = book["start_capital"] / len(symbols) / (1 + cfg["costs"]["fee_rate"])
        book["hold_units"] = {s: per / (prices[s] * (1 + cfg["costs"]["slippage"])) for s in symbols}
    just_hold = sum(book["hold_units"][s] * prices[s] for s in symbols)

    # 7. Save everything.
    book.update(units=units, cash=cash, last_signal_day=day_of(signal_day),
                ticks=book["ticks"] + 1, fees_paid=book["fees_paid"] + fees)
    save_json(book_path, book)
    order_text = "; ".join(f"{o.side} {o.symbol} {_money(o.notional)}" for o in orders) or "none"
    _journal(state_dir, dict(
        base, equity=f"{equity:.2f}", just_hold=f"{just_hold:.2f}", cash=f"{cash:.2f}",
        holdings=" ".join(f"{s}={values[s]:.2f}" for s in symbols),
        lights=" ".join(f"{s}={'on' if lights[s] else 'off'}" for s in symbols),
        orders=order_text, fees=f"{fees:.4f}", note=note))
    _status(state_dir, cfg, book, params, lights, values, equity, just_hold, run_utc)
    log(f"{run_utc} UTC | stack {_money(equity)} | just-hold {_money(just_hold)} | orders: {order_text}"
        + (f" | {note}" if note else ""))
    return HALTED if book["halted"] else OK
