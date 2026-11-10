"""Command line: python -m autotrader {tick,tune,backtest,status,resume}"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from datetime import datetime, timezone

from . import bot, data, tune
from .data import day_of
from .engine import hold, metrics

HERE = os.path.dirname(os.path.abspath(__file__))
HEAD = "| $100 run as | Ends at | Per year | Worst dip | Sharpe | Trades |\n|---|---|---|---|---|---|"


def _end(m: dict) -> str:
    return f"${tune.START * (1 + m['total_return']):,.2f}"


def _row(label: str, m: dict, trades="") -> str:
    return f"| {label} | {_end(m)} | {m['cagr']:+.1%} | {m['max_drawdown']:.1%} | {m['sharpe']:.2f} | {trades} |"


def _name(params: dict) -> str:
    return f"{params['lookback']}-day trend, {params['buffer']:.0%} buffer"


def report(cfg: dict, series: dict, params: dict) -> str:
    """The evidence page: how the rules did on real history, after costs."""
    lab = tune.Lab(series, cfg)
    days, coins = series["days"], len(series["close"])
    side = cfg["costs"]["fee_rate"] + cfg["costs"]["slippage"]
    out = [
        "# Autotrader evidence report",
        "",
        f"Real daily prices {day_of(days[0])} to {day_of(days[-1])} ({len(days)} days, "
        f"{series['filled']} gap-filled). Costs charged: {side:.2%} per trade side. Start: $100.",
        "",
        "Read this first: past results do not promise future results, these coins were picked "
        "knowing they survived, and a few years is too short to prove an edge.",
        "",
    ]
    try:
        live = tune.replay(lab, cfg, cfg["defaults"])
        out += [
            "## 1. The honest test: the bot exactly as it runs",
            "",
            f"{day_of(live['from'])} to {day_of(live['to'])}. Trades daily and re-tests its settings "
            f"every {cfg['tuning']['retune_days']} days, each time using only earlier prices. "
            f"It switched settings {len(live['switches'])} time(s).",
            "",
            HEAD,
            _row("Self-tuning bot", live["tuned"], live["tuned"]["trades"]),
            _row(f"Same bot, settings never changed ({_name(cfg['defaults'])})", live["fixed"],
                 live["fixed"]["trades"]),
            _row("Just holding all coins", live["hold"], coins),
            "",
        ]
    except ValueError as short:
        out += ["## 1. The honest test", "", f"Skipped: {short}.", ""]
    first = tune.warmup(cfg, params)
    run = lab.run(params, first, lab.days)
    out += [
        "## 2. Whole history with the current settings",
        "",
        f"{day_of(days[first])} to {day_of(days[-1])}. Invested {run.exposure:.0%} of the stack on "
        f"average, ${run.fees:,.2f} paid in fees.",
        "",
        HEAD,
        _row(f"Bot ({_name(params)})", metrics([tune.START] + run.equity), run.trades),
        _row("Just holding all coins",
             metrics([tune.START] + hold(series, cfg["costs"], first, lab.days, tune.START)), coins),
        "",
        "## 3. What if trading costs are worse than assumed?",
        "",
        "| Cost per trade side | Bot ends at | Worst dip |",
        "|---|---|---|",
    ]
    for scale in (0.5, 1, 2, 4):
        harsher = copy.deepcopy(cfg)
        harsher["costs"] = {k: v * scale for k, v in cfg["costs"].items()}
        m = tune.Lab(series, harsher).score(params, first, lab.days)
        out.append(f"| {side * scale:.2%}{' (assumed)' if scale == 1 else ''} | {_end(m)} "
                   f"| {m['max_drawdown']:.1%} |")
    out += [
        "",
        "## 4. The risk dial (`risk.vol_target` in config.json)",
        "",
        "| Bet sizing | Bot ends at | Per year | Worst dip |",
        "|---|---|---|---|",
    ]
    for label, target in (("Careful: 30% yearly swing per coin", 0.3),
                          ("Medium: 50% yearly swing per coin", 0.5),
                          ("Full size: no scaling", None)):
        dial = copy.deepcopy(cfg)
        dial["risk"]["vol_target"] = target
        m = tune.Lab(series, dial).score(params, first, lab.days)
        mark = " (current)" if target == cfg["risk"].get("vol_target") else ""
        out.append(f"| {label}{mark} | {_end(m)} | {m['cagr']:+.1%} | {m['max_drawdown']:.1%} |")
    out += [
        "",
        "## 5. Why this bot trades rarely",
        "",
        f"Buying and selling once a day at {side:.2%} per side leaves "
        f"${tune.START * (1 - side) ** 730:,.2f} of $100 after a year from costs alone. "
        f"This bot made {run.trades} trades in {(lab.days - first) / 365:.1f} years.",
        "",
    ]
    return "\n".join(out)


def cmd_tune(cfg: dict, state_dir: str) -> int:
    series = data.load_history(cfg)
    champion = bot.load_params(cfg, state_dir)
    verdict = tune.decide(tune.Lab(series, cfg), cfg, champion)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    os.makedirs(state_dir, exist_ok=True)
    with open(os.path.join(state_dir, "tuning_log.jsonl"), "a") as handle:
        handle.write(json.dumps(dict(verdict, run_utc=stamp, prices_through=day_of(series["days"][-1])),
                                sort_keys=True) + "\n")
    current = champion
    if verdict["adopt"]:
        current = verdict["challenger"]
        bot.save_json(os.path.join(state_dir, "params.json"),
                      dict(current, since=stamp, reason=verdict["reason"]))
    with open(os.path.join(state_dir, "REPORT.md"), "w") as handle:
        handle.write(report(cfg, series, current)
                     + f"\n## 6. This week's tuning decision ({stamp} UTC)\n\n"
                       f"{'SWITCHED to ' + _name(current) if verdict['adopt'] else 'No change'}: "
                       f"{verdict['reason']}.\n")
    print(f"tune: {'SWITCHED to ' + _name(current) if verdict['adopt'] else 'no change'}"
          f" - {verdict['reason']}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="autotrader", description=__doc__)
    parser.add_argument("command", choices=["tick", "tune", "backtest", "status", "resume"])
    parser.add_argument("--coins", help="backtest only: a subset of the coins, e.g. BTC,ETH")
    parser.add_argument("--since", help="backtest only: first day of history, e.g. 2016-06-01")
    parser.add_argument("--config", default=os.path.join(HERE, "config.json"))
    parser.add_argument("--state", default=os.environ.get("AUTOTRADER_STATE", os.path.join(HERE, "state")))
    args = parser.parse_args(argv)
    with open(args.config) as handle:
        cfg = json.load(handle)
    if args.command == "tick":
        return bot.tick(cfg, args.state, now=time.time())
    if args.command == "tune":
        return cmd_tune(cfg, args.state)
    if args.command == "resume":
        return bot.resume(args.state)
    if args.command == "backtest":
        if args.coins:
            wanted = args.coins.upper().split(",")
            cfg["universe"] = [asset for asset in cfg["universe"] if asset["symbol"] in wanted]
            if len(cfg["universe"]) != len(wanted):
                parser.error(f"--coins must come from the config universe, got {args.coins}")
        cfg["history_start"] = args.since or cfg["history_start"]
        print(report(cfg, data.load_history(cfg), bot.load_params(cfg, args.state)))
        return 0
    status = os.path.join(args.state, "STATUS.md")
    if os.path.exists(status):
        with open(status) as handle:
            print(handle.read())
    else:
        print("No status yet: run `tick` first.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
