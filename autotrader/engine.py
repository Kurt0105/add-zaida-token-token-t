"""Pure trading logic: trend lights, orders, fills, replay. No network, no files.

The live tick and the historical replay both go through plan_orders() and
apply_fills(), so what gets tested is what trades.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


def trend_states(closes: list[float], lookback: int, buffer: float) -> list[bool]:
    """For each day, is this asset in an uptrend (its "green light")?

    Turns on when the close is above its `lookback`-day average by more than
    `buffer`, turns off when it is below by more than `buffer`, and otherwise
    keeps yesterday's answer so it does not flip-flop around the average.
    Day i only ever uses closes[0..i].
    """
    states: list[bool] = []
    on, running = False, 0.0
    for i, close in enumerate(closes):
        running += close
        if i >= lookback:
            running -= closes[i - lookback]
        if i + 1 >= lookback:
            average = running / lookback
            if close > average * (1 + buffer):
                on = True
            elif close < average * (1 - buffer):
                on = False
        states.append(on)
    return states


def volatility(closes: list[float], window: int) -> list:
    """Yearly-ised size of an asset's daily swings over the last `window` days.

    None until there is enough data. Day i only ever uses closes[0..i].
    """
    moves = [0.0] + [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    out: list = [None] * len(closes)
    for i in range(window, len(closes)):
        recent = moves[i - window + 1 : i + 1]
        mean = sum(recent) / window
        out[i] = math.sqrt(sum((m - mean) ** 2 for m in recent) / (window - 1) * 365)
    return out


def target_weights(in_trend: dict, vols: dict, risk: dict) -> dict:
    """Each asset owns an equal slot, filled only while its light is green.

    With a `vol_target`, a jumpy asset gets a smaller bet so every slot carries
    about the same risk; bets are only ever scaled down, never up.
    """
    slot = risk["max_total_exposure"] / len(in_trend)
    target = risk.get("vol_target")
    weights = {}
    for symbol, on in in_trend.items():
        size = slot if on else 0.0
        if size and target:
            swing = vols.get(symbol)
            size *= 0.0 if swing is None else min(1.0, target / swing) if swing > 0 else 1.0
        weights[symbol] = size
    return weights


@dataclass(frozen=True)
class Order:
    symbol: str
    side: str        # "buy" or "sell"
    notional: float  # USD at the reference price, before costs
    units: float


def plan_orders(units: dict, cash: float, prices: dict, weights: dict,
                costs: dict, execution: dict) -> list[Order]:
    """Orders that move the portfolio to the target weights. Long-only, never borrows.

    Small drifts are ignored (each trade costs a fee); a slot whose light went
    red is always sold in full. Sells come first so buys can use the cash.
    """
    fee, slip = costs["fee_rate"], costs["slippage"]
    equity = cash + sum(units.get(s, 0.0) * prices[s] for s in prices)
    threshold = max(execution["min_order_usd"], execution["rebalance_band"] * equity)
    sells: list[Order] = []
    wanted: list[tuple[str, float]] = []
    for symbol in sorted(prices):
        held = units.get(symbol, 0.0)
        current = held * prices[symbol]
        target = weights.get(symbol, 0.0) * equity
        if target == 0.0:
            if held > 0.0:
                sells.append(Order(symbol, "sell", current, held))
        elif current - target >= threshold:
            trim = current - target
            sells.append(Order(symbol, "sell", trim, trim / prices[symbol]))
        elif target - current >= threshold:
            wanted.append((symbol, target - current))
    spendable = cash + sum(o.notional * (1 - slip) * (1 - fee) for o in sells)
    orders = list(sells)
    for symbol, want in wanted:
        notional = min(want, spendable / (1 + fee))
        if notional >= execution["min_order_usd"]:
            orders.append(Order(symbol, "buy", notional, notional / (prices[symbol] * (1 + slip))))
            spendable -= notional * (1 + fee)
    return orders


def apply_fills(units: dict, cash: float, orders: list[Order], prices: dict,
                costs: dict) -> tuple[dict, float, float]:
    """Fill orders at `prices`, charging fee and slippage. Returns (units, cash, fees)."""
    fee, slip = costs["fee_rate"], costs["slippage"]
    units, paid = dict(units), 0.0
    for order in orders:
        price = prices[order.symbol]
        if order.side == "sell":
            sold = min(order.units, units.get(order.symbol, 0.0))
            gross = sold * price * (1 - slip)
            cash += gross * (1 - fee)
            paid += gross * fee
            units[order.symbol] = units.get(order.symbol, 0.0) - sold
        else:
            cash -= order.notional * (1 + fee)
            paid += order.notional * fee
            units[order.symbol] = units.get(order.symbol, 0.0) + order.notional / (price * (1 + slip))
    if -1e-9 < cash < 0.0:
        cash = 0.0
    return units, cash, paid


@dataclass
class Result:
    equity: list[float] = field(default_factory=list)  # one mark per traded day
    cash: float = 0.0
    units: dict = field(default_factory=dict)
    trades: int = 0
    fees: float = 0.0
    exposure: float = 0.0                              # average fraction invested


def simulate(series: dict, lights: dict, vols: dict, cfg: dict, i0: int, i1: int,
             cash: float, units: dict | None = None) -> Result:
    """Replay days i0..i1-1. Each day: act at the open on yesterday's close, mark at the close."""
    if i0 < 1:
        raise ValueError("i0 must be >= 1: day i0 trades on the signal from day i0-1")
    symbols = sorted(series["close"])
    units = {s: (units or {}).get(s, 0.0) for s in symbols}
    result = Result()
    invested = 0.0
    for j in range(i0, i1):
        weights = target_weights({s: lights[s][j - 1] for s in symbols},
                                 {s: vols[s][j - 1] for s in symbols}, cfg["risk"])
        opens = {s: series["open"][s][j] for s in symbols}
        orders = plan_orders(units, cash, opens, weights, cfg["costs"], cfg["execution"])
        units, cash, paid = apply_fills(units, cash, orders, opens, cfg["costs"])
        held = sum(units[s] * series["close"][s][j] for s in symbols)
        result.equity.append(cash + held)
        result.trades += len(orders)
        result.fees += paid
        invested += held / (cash + held) if cash + held > 0 else 0.0
    result.cash, result.units = cash, units
    result.exposure = invested / max(1, i1 - i0)
    return result


def hold(series: dict, costs: dict, i0: int, i1: int, cash: float) -> list[float]:
    """Benchmark: split the cash equally at the open of day i0, then never touch it."""
    symbols = sorted(series["close"])
    per = cash / len(symbols) / (1 + costs["fee_rate"])
    units = {s: per / (series["open"][s][i0] * (1 + costs["slippage"])) for s in symbols}
    return [sum(units[s] * series["close"][s][j] for s in symbols) for j in range(i0, i1)]


def metrics(equity: list[float]) -> dict:
    """Summary of an equity curve whose first point is the starting capital."""
    n = len(equity) - 1
    if n < 1:
        return {"days": 0, "total_return": 0.0, "cagr": 0.0, "max_drawdown": 0.0, "sharpe": 0.0}
    returns = [equity[i] / equity[i - 1] - 1 for i in range(1, len(equity))]
    mean = sum(returns) / n
    spread = math.sqrt(sum((r - mean) ** 2 for r in returns) / (n - 1)) if n > 1 else 0.0
    peak, worst = equity[0], 0.0
    for value in equity:
        peak = max(peak, value)
        worst = max(worst, 1 - value / peak)
    growth = equity[-1] / equity[0]
    return {
        "days": n,
        "total_return": growth - 1,
        "cagr": growth ** (365 / n) - 1 if growth > 0 else -1.0,
        "max_drawdown": worst,
        "sharpe": mean / spread * math.sqrt(365) if spread > 1e-12 else 0.0,
    }
