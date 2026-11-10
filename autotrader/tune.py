"""The self-improving part: re-test a small menu of settings on real history.

  decide()  the weekly champion-vs-challenger test for the live settings
  replay()  what that exact weekly rule would have done over the years

Settings are only ever judged on days they were not picked on.
"""
from __future__ import annotations

from .engine import hold, metrics, simulate, trend_states, volatility

START = 100.0


def param_grid(cfg: dict) -> list[dict]:
    grid = cfg["tuning"]["grid"]
    return [{"lookback": lb, "buffer": bf} for lb in grid["lookback"] for bf in grid["buffer"]]


def same(a: dict, b: dict) -> bool:
    return a["lookback"] == b["lookback"] and abs(a["buffer"] - b["buffer"]) < 1e-12


def warmup(cfg: dict, *extra: dict) -> int:
    """Days of history needed before the slowest setting has a signal."""
    return max(cfg["risk"]["vol_window"] + 1, *(p["lookback"] for p in param_grid(cfg) + list(extra)))


class Lab:
    """Replays history for any settings; caches the signals so a whole menu stays fast."""

    def __init__(self, series: dict, cfg: dict):
        self.series, self.cfg = series, cfg
        self.days = len(series["days"])
        self.vols = {s: volatility(closes, cfg["risk"]["vol_window"])
                     for s, closes in series["close"].items()}
        self._lights: dict = {}

    def lights(self, params: dict) -> dict:
        key = (params["lookback"], params["buffer"])
        if key not in self._lights:
            self._lights[key] = {s: trend_states(closes, *key)
                                 for s, closes in self.series["close"].items()}
        return self._lights[key]

    def run(self, params: dict, i0: int, i1: int, cash: float = START, units: dict | None = None):
        return simulate(self.series, self.lights(params), self.vols, self.cfg, i0, i1, cash, units)

    def score(self, params: dict, i0: int, i1: int) -> dict:
        return metrics([START] + self.run(params, i0, i1).equity)


def pick(lab: Lab, grid: list[dict], i0: int, i1: int) -> tuple[dict, float]:
    """Best settings on days i0..i1-1 by risk-adjusted return. Ties go to the slower setting."""
    ranked = [((round(lab.score(p, i0, i1)["sharpe"], 6), p["lookback"], p["buffer"]), p) for p in grid]
    key, best = max(ranked, key=lambda item: item[0])
    return best, key[0]


def decide(lab: Lab, cfg: dict, champion: dict, upto: int | None = None) -> dict:
    """Weekly champion-vs-challenger test, using only days before `upto`.

    The challenger is the best setting on OLDER data. It replaces the champion
    only if it also clearly beats it on the most recent stretch, which neither
    was picked on, and stays inside the drawdown limit there. Otherwise nothing
    changes: no evidence, no switch.
    """
    tuning = cfg["tuning"]
    end = lab.days if upto is None else upto
    recent = end - tuning["holdout_days"]
    older = recent - tuning["train_days"]
    verdict = {"adopt": False, "champion": champion, "challenger": None}
    if older < warmup(cfg, champion):
        return dict(verdict, reason="not enough history yet")
    challenger, _ = pick(lab, param_grid(cfg), older, recent)
    verdict["challenger"] = challenger
    if same(challenger, champion):
        return dict(verdict, reason="challenger is the current champion")
    old, new = lab.score(champion, recent, end), lab.score(challenger, recent, end)
    verdict.update(champion_recent=old, challenger_recent=new)
    if new["max_drawdown"] > cfg["risk"]["max_drawdown_halt"]:
        return dict(verdict, reason="challenger broke the drawdown limit on recent data")
    if new["sharpe"] < old["sharpe"] + tuning["min_sharpe_gain"]:
        return dict(verdict, reason="challenger did not clearly beat the champion on recent data")
    return dict(verdict, adopt=True, reason="challenger clearly beat the champion on recent, unseen data")


def first_decision_day(cfg: dict, default: dict) -> int:
    tuning = cfg["tuning"]
    return warmup(cfg, default) + tuning["train_days"] + tuning["holdout_days"]


def replay(lab: Lab, cfg: dict, default: dict) -> dict:
    """Replay exactly what the live bot does: trade daily, re-test settings weekly.

    Every decision uses only prices from before that day. The same money is
    also run with fixed default settings and as a plain buy-and-hold.
    """
    first = first_decision_day(cfg, default)
    if first + 2 > lab.days:
        raise ValueError("not enough history to replay the weekly self-tuning")
    book = {name: {"cash": START, "units": None, "equity": [START], "trades": 0, "fees": 0.0}
            for name in ("tuned", "fixed")}
    champion, switches, i = default, [], first
    while i < lab.days:
        j = min(i + cfg["tuning"]["retune_days"], lab.days)
        verdict = decide(lab, cfg, champion, upto=i)
        if verdict["adopt"]:
            champion = verdict["challenger"]
            switches.append({"day": lab.series["days"][i], "params": champion})
        for name, params in (("tuned", champion), ("fixed", default)):
            b = book[name]
            run = lab.run(params, i, j, b["cash"], b["units"])
            b["cash"], b["units"] = run.cash, run.units
            b["equity"] += run.equity
            b["trades"] += run.trades
            b["fees"] += run.fees
        i = j
    out = {"from": lab.series["days"][first], "to": lab.series["days"][-1], "switches": switches,
           "final_params": champion,
           "hold": metrics([START] + hold(lab.series, cfg["costs"], first, lab.days, START))}
    for name, b in book.items():
        out[name] = dict(metrics(b["equity"]), trades=b["trades"], fees=b["fees"])
    return out
