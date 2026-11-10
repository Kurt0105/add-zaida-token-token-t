"""Tests run on made-up prices only: no network, no real state."""
import copy
import csv
import json
import math
import os
import random
import tempfile
import unittest
import urllib.parse

from autotrader import bot, data, tune
from autotrader.__main__ import report
from autotrader.data import DAY, DataError, ts_of
from autotrader.engine import (apply_fills, hold, metrics, plan_orders, simulate, target_weights,
                               trend_states, volatility)

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "..", "config.json")) as _handle:
    CONFIG = json.load(_handle)
COSTS = {"fee_rate": 0.01, "slippage": 0.0}
EXEC = {"min_order_usd": 1.0, "rebalance_band": 0.05}
FULL_SIZE = {"max_total_exposure": 1.0, "vol_target": None, "vol_window": 30}
SIMPLE = {"costs": COSTS, "execution": EXEC, "risk": FULL_SIZE}  # 1% fee, no bet scaling
DAY0 = ts_of("2024-01-01")
data.PAUSE = 0.0  # no need to be polite to a fake exchange


def one_asset(closes, opens=None):
    opens = opens or [closes[0]] + closes[:-1]
    return {"days": [DAY0 + i * DAY for i in range(len(closes))],
            "open": {"A": opens}, "close": {"A": closes}, "filled": 0}


def run_one(series, lookback, i0, i1, cash=100.0):
    closes = series["close"]["A"]
    return simulate(series, {"A": trend_states(closes, lookback, 0.0)}, {"A": [None] * len(closes)},
                    SIMPLE, i0, i1, cash)


def random_walks(n, seed, swing=0.03, cycle=150):
    """{symbol: [price per day]} with alternating up and down markets."""
    rng = random.Random(seed)
    walks = {}
    for k, symbol in enumerate(("BTC", "ETH", "SOL")):
        price, path = 100.0 * (k + 1), []
        for i in range(n):
            price *= 1 + (0.004 if (i // cycle) % 2 == 0 else -0.004) + rng.gauss(0, swing * (k + 1) / 2)
            path.append(price)
        walks[symbol] = path
    return walks


def random_series(n, seed=7):
    walks = random_walks(n, seed)
    return {"days": [DAY0 + i * DAY for i in range(n)], "filled": 0, "close": walks,
            "open": {s: [100.0 * (k + 1)] + path[:-1] for k, (s, path) in enumerate(walks.items())}}


def cut(series, n):
    return {"days": series["days"][:n], "filled": 0,
            "open": {s: v[:n] for s, v in series["open"].items()},
            "close": {s: v[:n] for s, v in series["close"].items()}}


class Signals(unittest.TestCase):
    def test_trend_light_hand_worked(self):
        self.assertEqual(trend_states([10, 10, 12, 12, 12, 9, 9], 2, 0.0),
                         [False, False, True, True, True, False, False])

    def test_buffer_holds_yesterdays_answer_inside_the_band(self):
        closes = [100, 100, 120, 118, 119, 60]
        # day 3: close 118 is just under its 2-day average of 119, but inside the 5% band
        self.assertEqual(trend_states(closes, 2, 0.05), [False, False, True, True, True, False])
        self.assertEqual(trend_states(closes, 2, 0.0), [False, False, True, False, True, False])

    def test_volatility_hand_worked(self):
        vols = volatility([100, 110, 99, 108.9], 2)
        expected = math.sqrt((math.log(1.1) - math.log(0.9)) ** 2 / 2 * 365)
        self.assertEqual(vols[:2], [None, None])
        self.assertAlmostEqual(vols[2], expected)
        self.assertAlmostEqual(vols[3], expected)

    def test_signals_never_look_ahead(self):
        closes = random_series(300)["close"]["BTC"]
        lights, vols = trend_states(closes, 50, 0.02), volatility(closes, 30)
        for n in (60, 150, 299):
            self.assertEqual(trend_states(closes[:n], 50, 0.02), lights[:n])
            self.assertEqual(volatility(closes[:n], 30), vols[:n])

    def test_weights_are_equal_slots_under_the_cap(self):
        weights = target_weights({"A": True, "B": False, "C": True}, {},
                                 {"max_total_exposure": 0.9, "vol_target": None})
        self.assertAlmostEqual(weights["A"], 0.3)
        self.assertEqual(weights["B"], 0.0)
        self.assertLessEqual(sum(weights.values()), 0.9 + 1e-12)

    def test_jumpy_coins_get_smaller_bets_and_calm_ones_are_never_scaled_up(self):
        risk = {"max_total_exposure": 0.9, "vol_target": 0.3}
        lights = {"A": True, "B": True, "C": True, "D": False}
        weights = target_weights(lights, {"A": 0.1, "B": 0.6, "C": None, "D": 0.6}, risk)
        self.assertAlmostEqual(weights["A"], 0.225)        # calm: full slot, not more
        self.assertAlmostEqual(weights["B"], 0.1125)       # twice the target swing: half a slot
        self.assertEqual((weights["C"], weights["D"]), (0.0, 0.0))  # unknown swing or red light


class Orders(unittest.TestCase):
    def test_small_drift_is_ignored(self):
        orders = plan_orders({"A": 4.8}, 52.0, {"A": 10.0}, {"A": 0.5}, COSTS, EXEC)
        self.assertEqual(orders, [])  # 48 held vs 50 target: inside the 5% band

    def test_red_light_sells_everything_even_if_tiny(self):
        orders = plan_orders({"A": 0.01}, 99.9, {"A": 10.0}, {"A": 0.0}, COSTS, EXEC)
        self.assertEqual([(o.symbol, o.side, o.units) for o in orders], [("A", "sell", 0.01)])
        self.assertAlmostEqual(orders[0].notional, 0.1)

    def test_sells_come_first_and_buys_never_exceed_cash(self):
        units, cash, prices = {"A": 10.0, "B": 0.0}, 0.0, {"A": 10.0, "B": 5.0}
        orders = plan_orders(units, cash, prices, {"A": 0.0, "B": 1.0}, COSTS, EXEC)
        self.assertEqual([o.side for o in orders], ["sell", "buy"])
        units, cash, fees = apply_fills(units, cash, orders, prices, COSTS)
        self.assertGreaterEqual(cash, 0.0)
        self.assertAlmostEqual(cash, 0.0, places=9)
        self.assertEqual(units["A"], 0.0)
        self.assertAlmostEqual(fees, 1.0 + 99.0 / 1.01 * 0.01)

    def test_order_below_minimum_is_not_placed(self):
        execution = {"min_order_usd": 5.0, "rebalance_band": 0.0}
        self.assertEqual(plan_orders({"A": 0.0}, 4.0, {"A": 10.0}, {"A": 1.0}, COSTS, execution), [])


class Replay(unittest.TestCase):
    def test_hand_worked_buy_and_sell_with_fees(self):
        series = one_asset([10, 10, 12, 12, 12, 9, 9], opens=[10, 10, 10, 12, 12, 12, 9])
        run = run_one(series, 2, 1, 7)
        spent = 100 / 1.01                      # buy at 12 on day 3 with a 1% fee
        after_drop = spent / 12 * 9             # falls to 9
        self.assertEqual(run.trades, 2)
        self.assertAlmostEqual(run.equity[2], spent)
        self.assertAlmostEqual(run.equity[-1], after_drop * 0.99)
        self.assertAlmostEqual(run.fees, spent * 0.01 + after_drop * 0.01)
        self.assertEqual(run.units["A"], 0.0)

    def test_flat_market_never_trades(self):
        run = run_one(one_asset([50.0] * 40), 10, 1, 40)
        self.assertEqual((run.trades, run.fees), (0, 0.0))
        self.assertEqual(set(run.equity), {100.0})

    def test_the_future_cannot_change_the_past(self):
        series = random_series(400)
        full = tune.Lab(series, CONFIG).run(CONFIG["defaults"], 120, 400)
        self.assertGreater(full.trades, 0)
        self.assertEqual(tune.Lab(cut(series, 300), CONFIG).run(CONFIG["defaults"], 120, 300).equity,
                         full.equity[:180])

    def test_never_borrows_and_never_shorts(self):
        lab = tune.Lab(random_series(600, seed=3), CONFIG)
        cash, units = 100.0, None
        for day in range(101, 600):  # step one day at a time and inspect the book
            run = lab.run({"lookback": 20, "buffer": 0.0}, day, day + 1, cash, units)
            cash, units = run.cash, run.units
            self.assertGreaterEqual(cash, 0.0)
            self.assertTrue(all(u >= 0.0 for u in units.values()))

    def test_stepping_day_by_day_matches_one_long_run(self):
        lab = tune.Lab(random_series(400, seed=5), CONFIG)
        long_run = lab.run(CONFIG["defaults"], 101, 400).equity
        cash, units, stepped = 100.0, None, []
        for day in range(101, 400):
            run = lab.run(CONFIG["defaults"], day, day + 1, cash, units)
            cash, units = run.cash, run.units
            stepped += run.equity
        self.assertEqual(len(stepped), len(long_run))
        for a, b in zip(stepped, long_run):
            self.assertAlmostEqual(a, b, places=9)

    def test_costs_only_ever_hurt(self):
        series = random_series(500, seed=11)
        cheap, dear = copy.deepcopy(CONFIG), copy.deepcopy(CONFIG)
        cheap["costs"] = {"fee_rate": 0.0, "slippage": 0.0}
        dear["costs"] = {"fee_rate": 0.01, "slippage": 0.005}
        params = {"lookback": 20, "buffer": 0.0}
        free = tune.Lab(series, cheap).run(params, 101, 500)
        paid = tune.Lab(series, dear).run(params, 101, 500)
        self.assertEqual(free.fees, 0.0)
        self.assertGreater(paid.fees, 0.0)
        self.assertGreater(free.equity[-1], paid.equity[-1])

    def test_smaller_bets_mean_less_money_in_the_market(self):
        series = random_series(500, seed=11)
        full = copy.deepcopy(CONFIG)
        full["risk"]["vol_target"] = None
        careful = tune.Lab(series, CONFIG).run(CONFIG["defaults"], 101, 500)
        bold = tune.Lab(series, full).run(CONFIG["defaults"], 101, 500)
        self.assertGreater(bold.exposure, careful.exposure)
        self.assertGreater(careful.exposure, 0.0)

    def test_hold_and_metrics(self):
        series = one_asset([10.0, 20.0, 10.0, 15.0], opens=[10.0, 10.0, 20.0, 10.0])
        curve = [100.0] + hold(series, {"fee_rate": 0.0, "slippage": 0.0}, 0, 4, 100.0)
        self.assertEqual(curve, [100.0, 100.0, 200.0, 100.0, 150.0])
        m = metrics(curve)
        self.assertAlmostEqual(m["total_return"], 0.5)
        self.assertAlmostEqual(m["max_drawdown"], 0.5)
        self.assertEqual(metrics([100.0])["sharpe"], 0.0)


class FakeLab:
    """A lab whose scores are scripted, to test the switching rules in isolation."""

    def __init__(self, days, older_best, recent):
        self.days, self.older_best, self.recent = days, older_best, recent

    def score(self, params, i0, i1):
        if i1 < self.days:  # the older "selection" window
            return {"sharpe": 9.0 if tune.same(params, self.older_best) else 0.0, "max_drawdown": 0.0}
        return self.recent(params)


class Tuning(unittest.TestCase):
    CHAMPION = {"lookback": 100, "buffer": 0.02}
    RIVAL = {"lookback": 50, "buffer": 0.0}

    def decide(self, rival_sharpe, rival_dip=0.05, older_best=None):
        def recent(params):
            if tune.same(params, self.RIVAL):
                return {"sharpe": rival_sharpe, "max_drawdown": rival_dip}
            return {"sharpe": 1.0, "max_drawdown": 0.05}
        return tune.decide(FakeLab(2000, older_best or self.RIVAL, recent), CONFIG, self.CHAMPION)

    @staticmethod
    def quick(cfg=CONFIG):
        cfg = copy.deepcopy(cfg)
        cfg["tuning"].update(train_days=300, holdout_days=60, min_sharpe_gain=0.0)
        return cfg

    def test_grid_is_the_small_menu_from_config(self):
        self.assertEqual(len(tune.param_grid(CONFIG)), 15)

    def test_clear_winner_on_unseen_data_takes_over(self):
        verdict = self.decide(rival_sharpe=1.5)
        self.assertTrue(verdict["adopt"])
        self.assertEqual(verdict["challenger"], self.RIVAL)

    def test_narrow_winner_does_not_take_over(self):
        self.assertFalse(self.decide(rival_sharpe=1.2)["adopt"])

    def test_winner_that_breaks_the_drawdown_limit_is_rejected(self):
        too_deep = CONFIG["risk"]["max_drawdown_halt"] + 0.01
        self.assertFalse(self.decide(rival_sharpe=5.0, rival_dip=too_deep)["adopt"])

    def test_no_switch_when_the_champion_is_still_the_best(self):
        verdict = self.decide(rival_sharpe=5.0, older_best=self.CHAMPION)
        self.assertFalse(verdict["adopt"])
        self.assertIn("current champion", verdict["reason"])

    def test_no_switch_without_enough_history(self):
        lab = FakeLab(500, self.RIVAL, lambda p: {"sharpe": 9.0, "max_drawdown": 0.0})
        self.assertFalse(tune.decide(lab, CONFIG, self.CHAMPION)["adopt"])

    def test_pick_matches_brute_force_and_breaks_ties_toward_slower(self):
        lab, grid = tune.Lab(random_series(700, seed=2), CONFIG), tune.param_grid(CONFIG)
        best, score = tune.pick(lab, grid, 200, 700)
        self.assertAlmostEqual(score, max(lab.score(p, 200, 700)["sharpe"] for p in grid), places=6)
        slowest, zero = tune.pick(tune.Lab(one_asset([10.0] * 400), CONFIG), grid, 201, 400)
        self.assertEqual((slowest, zero), ({"lookback": 200, "buffer": 0.05}, 0.0))

    def test_a_decision_only_uses_prices_from_before_that_day(self):
        cfg, series = self.quick(), random_series(900, seed=4)
        seen = []
        for day in (600, 700, 800):
            early = tune.decide(tune.Lab(series, cfg), cfg, cfg["defaults"], upto=day)
            self.assertEqual(early, tune.decide(tune.Lab(cut(series, day), cfg), cfg, cfg["defaults"]))
            self.assertIsNotNone(early["challenger"])
            seen.append(early["adopt"])
        self.assertIn(True, seen)  # the made-up market does produce at least one real switch

    def test_replay_runs_the_weekly_rule_and_never_peeks(self):
        cfg, series = self.quick(), random_series(900, seed=4)
        first = tune.replay(tune.Lab(series, cfg), cfg, cfg["defaults"])
        self.assertEqual(first["from"], series["days"][560])  # 200 warm-up + 300 + 60
        self.assertEqual(first["tuned"]["days"], 340)
        self.assertEqual(first["fixed"]["days"], 340)
        self.assertGreater(len(first["switches"]), 0)
        changed = copy.deepcopy(series)
        for symbol in changed["close"]:  # rewrite the market from day 740 on
            for i in range(740, 900):
                changed["close"][symbol][i] *= 3.0
                changed["open"][symbol][i] *= 3.0
        second = tune.replay(tune.Lab(changed, cfg), cfg, cfg["defaults"])
        cutoff = series["days"][740]
        early = [s for s in first["switches"] if s["day"] <= cutoff]
        self.assertEqual(early, [s for s in second["switches"] if s["day"] <= cutoff])
        self.assertGreater(len(early), 0)

    def test_report_runs_end_to_end_on_made_up_prices(self):
        cfg = self.quick()
        text = report(cfg, random_series(900, seed=4), cfg["defaults"])
        for needle in ("## 1.", "## 2.", "## 3.", "## 4.", "## 5.", "Self-tuning bot", "(assumed)",
                       "(current)"):
            self.assertIn(needle, text)
        self.assertIn("Skipped", report(cfg, random_series(400, seed=4), cfg["defaults"]))


def fake_market(table, kraken_scale=1.0, kraken_live_scale=1.0, calls=None):
    """One price table {symbol: {day_ts: close}} served in both exchanges' formats."""
    products = {a["coinbase"]: a["symbol"] for a in CONFIG["universe"]}
    pairs = {a["kraken"]: a["symbol"] for a in CONFIG["universe"]}

    def get(url):
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        if calls is not None:
            calls.append(url)
        if "coinbase" in parsed.netloc:
            start, end = ts_of(query["start"][0][:10]), ts_of(query["end"][0][:10])
            if (end - start) // DAY + 1 > 300:
                return {"message": "granularity too small for the requested time range"}
            prices = table[products[parsed.path.split("/")[2]]]
            return [[d, c * 0.99, c * 1.01, c, c, 1.0]
                    for d, c in sorted(prices.items(), reverse=True) if start <= d <= end]
        prices = dict(table[pairs[query["pair"][0]]])
        prices[max(prices)] *= kraken_live_scale  # only the bar still in progress
        rows = [[d, str(c * kraken_scale), str(c * kraken_scale * 1.01), str(c * kraken_scale * 0.99),
                 str(c * kraken_scale), "0", "1", 1] for d, c in sorted(prices.items())]
        return {"error": [], "result": {"X" + query["pair"][0]: rows, "last": 0}}
    return get


def rising(days, rate, start=DAY0):
    return {s: {start + i * DAY: 100.0 * (k + 1) * (1 + rate) ** i for i in range(days)}
            for k, s in enumerate(("BTC", "ETH", "SOL"))}


class Prices(unittest.TestCase):
    def test_long_ranges_are_fetched_in_pages_the_api_accepts(self):
        table, calls = rising(650, 0.001), []
        bars = data.coinbase_daily("BTC-USD", DAY0, DAY0 + 649 * DAY, fake_market(table, calls=calls))
        self.assertEqual(len(calls), 3)
        self.assertEqual(sorted(bars), sorted(table["BTC"]))
        self.assertEqual(bars[DAY0], (100.0, 100.0))

    def test_broken_bars_are_dropped_and_error_replies_raise(self):
        rows = [[DAY0, 9, 11, 10, 10, 1], [DAY0 + DAY, 12, 13, 10, 10, 1], [DAY0 + 2 * DAY, 0, 1, 0, 0, 1]]
        self.assertEqual(list(data.coinbase_daily("X", DAY0, DAY0 + 2 * DAY, lambda url: rows)), [DAY0])
        with self.assertRaises(DataError):
            data.coinbase_daily("X", DAY0, DAY0, lambda url: {"message": "nope"})
        with self.assertRaises(DataError):
            data.kraken_daily("X", lambda url: {"error": ["EGeneral:Bad"]})

    def test_kraken_reply_is_parsed(self):
        bars = data.kraken_daily("XBTUSD", fake_market(rising(5, 0.01)))
        self.assertEqual(sorted(bars), [DAY0 + i * DAY for i in range(5)])
        self.assertAlmostEqual(bars[DAY0 + DAY][1], 101.0)

    def test_align_fills_gaps_with_the_previous_close(self):
        a = {DAY0: (1.0, 2.0), DAY0 + DAY: (2.0, 3.0), DAY0 + 2 * DAY: (3.0, 4.0)}
        b = {DAY0 - DAY: (9.0, 8.0), DAY0 + 2 * DAY: (7.0, 6.0)}  # misses two days
        series = data.align({"A": a, "B": b})
        self.assertEqual(series["days"], [DAY0, DAY0 + DAY, DAY0 + 2 * DAY])
        self.assertEqual(series["close"]["B"], [8.0, 8.0, 6.0])
        self.assertEqual(series["open"]["B"], [8.0, 8.0, 7.0])
        self.assertEqual(series["filled"], 2)
        with self.assertRaises(DataError):
            data.align({"A": {DAY0: (1.0, 1.0)}, "B": {DAY0 - 5 * DAY: (1.0, 1.0)}})

    def test_history_stops_at_yesterday_and_skips_the_unfinished_bar(self):
        cfg = dict(CONFIG, history_start="2024-01-01")
        series = data.load_history(cfg, now=DAY0 + 9 * DAY + 5000, get=fake_market(rising(10, 0.01)))
        self.assertEqual(series["days"], [DAY0 + i * DAY for i in range(9)])


class DailyTick(unittest.TestCase):
    DAYS = 700

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = self.tmp.name
        self.table = rising(self.DAYS, 0.01)
        self.today = DAY0 + (self.DAYS - 1) * DAY  # last row is the bar still in progress
        self.log = []

    def tick(self, days_later=0, table=None, **market):
        now = self.today + days_later * DAY + 1000
        return bot.tick(CONFIG, self.state, now=now, get=fake_market(table or self.table, **market),
                        log=self.log.append)

    def book(self):
        return bot.load_json(os.path.join(self.state, "portfolio.json"))

    def journal(self):
        with open(os.path.join(self.state, "journal.csv")) as handle:
            return list(csv.DictReader(handle))

    def status(self):
        with open(os.path.join(self.state, "STATUS.md")) as handle:
            return handle.read()

    def extend(self, factor):
        table = copy.deepcopy(self.table)
        for prices in table.values():
            prices[self.today + DAY] = prices[self.today] * factor
        return table

    def test_first_tick_buys_the_green_lights_and_writes_everything(self):
        self.assertEqual(self.tick(), bot.OK)
        book, rows = self.book(), self.journal()
        self.assertEqual(book["mode"], "paper")
        self.assertTrue(all(u > 0 for u in book["units"].values()))
        self.assertLess(book["cash"], 1.0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["orders"].count("buy"), 3)
        self.assertAlmostEqual(float(rows[0]["equity"]), 100 - 100 * (0.0025 + 0.001), delta=0.05)
        self.assertAlmostEqual(float(rows[0]["just_hold"]), float(rows[0]["equity"]), delta=0.01)
        self.assertIn("PAPER money", self.status())
        self.assertIn("BTC GREEN", self.status())

    def test_running_twice_in_a_day_does_nothing_the_second_time(self):
        self.tick()
        before = self.book()
        self.assertEqual(self.tick(), bot.OK)
        self.assertEqual(self.book(), before)
        self.assertEqual(len(self.journal()), 1)
        self.assertIn("Already ran", self.log[-1])

    def test_next_day_adds_one_journal_row_and_no_churn(self):
        self.tick()
        self.assertEqual(self.tick(days_later=1, table=self.extend(1.01)), bot.OK)
        rows = self.journal()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["orders"], "none")
        self.assertEqual(self.book()["ticks"], 2)

    def test_missed_days_are_counted_and_noted(self):
        self.tick()
        table = copy.deepcopy(self.table)
        for prices in table.values():
            for extra in (1, 2, 3):
                prices[self.today + extra * DAY] = prices[self.today] * 1.01 ** extra
        self.assertEqual(self.tick(days_later=3, table=table), bot.OK)
        self.assertEqual(self.book()["missed_days"], 2)
        self.assertIn("missed 2 daily run(s)", self.journal()[-1]["note"])
        self.assertIn("2 done, 2 missed", self.status())

    def test_disagreeing_price_sources_mean_no_trading(self):
        self.assertEqual(self.tick(kraken_scale=1.05), bot.SKIPPED)
        self.assertIsNone(self.book())
        self.assertIn("disagree", self.journal()[0]["note"])
        self.assertEqual(self.tick(), bot.OK)  # a later clean run the same day still works

    def test_a_disagreeing_live_price_means_no_trading(self):
        self.assertEqual(self.tick(kraken_live_scale=0.9), bot.SKIPPED)
        self.assertIsNone(self.book())
        self.assertIn(data.day_of(self.today), self.journal()[0]["note"])

    def test_stale_prices_mean_no_trading(self):
        self.assertEqual(self.tick(days_later=3), bot.SKIPPED)
        self.assertIn("no finished bar", self.journal()[0]["note"])

    def test_auto_stop_sells_everything_and_stays_stopped(self):
        self.tick()
        crash = self.extend(0.6)  # -40% in a day, lights still green: only the auto-stop can act
        self.assertEqual(self.tick(days_later=1, table=crash), bot.HALTED)
        book = self.book()
        self.assertTrue(book["halted"])
        self.assertEqual(set(book["units"].values()), {0.0})
        self.assertIn("AUTO-STOP", self.journal()[-1]["note"])
        self.assertIn("GREEN", self.status())
        self.assertIn("STOPPED", self.status())
        rebound = copy.deepcopy(crash)
        for prices in rebound.values():
            prices[self.today + 2 * DAY] = prices[self.today + DAY] * 2
        self.assertEqual(self.tick(days_later=2, table=rebound), bot.OK)
        self.assertEqual(self.book(), book)  # no trades until a human resets it
        self.assertEqual(len(self.journal()), 2)

    def test_a_human_can_resume_after_an_auto_stop(self):
        self.assertEqual(bot.resume(self.state, log=self.log.append), bot.OK)  # nothing to do yet
        self.assertIn("Nothing to resume", self.log[-1])
        self.tick()
        crash = self.extend(0.6)
        self.assertEqual(self.tick(days_later=1, table=crash), bot.HALTED)
        self.assertEqual(bot.resume(self.state, log=self.log.append), bot.OK)
        book = self.book()
        self.assertFalse(book["halted"])
        self.assertEqual(book["peak_equity"], book["cash"])  # the next dip is measured from here
        for prices in crash.values():
            prices[self.today + 2 * DAY] = prices[self.today + DAY]
        self.assertEqual(self.tick(days_later=2, table=crash), bot.OK)
        notes = [row["note"] for row in self.journal()]
        self.assertEqual(len(notes), 4)
        self.assertIn("RESUMED", notes[2])
        self.assertIn("| Bot | running |", self.status())

    def test_a_dip_inside_the_limit_does_not_stop_the_bot(self):
        self.tick()
        self.assertEqual(self.tick(days_later=1, table=self.extend(0.8)), bot.OK)
        self.assertFalse(self.book()["halted"])
        self.assertAlmostEqual(self.book()["worst_drawdown"], 0.2, delta=0.01)

    def test_live_mode_is_locked(self):
        live = dict(CONFIG, mode="live")
        with self.assertRaises(bot.LiveTradingLocked):
            bot.tick(live, self.state, now=self.today + 1000, get=fake_market(self.table))
        self.assertFalse(os.path.exists(os.path.join(self.state, "portfolio.json")))

    def test_tick_agrees_with_the_replay(self):
        """Thirty daily ticks must land on the same money as one replay of the same days."""
        walks = random_walks(760, seed=9, cycle=120)
        table = {s: {DAY0 + i * DAY: price for i, price in enumerate(path)} for s, path in walks.items()}
        cfg = copy.deepcopy(CONFIG)
        cfg["risk"]["max_drawdown_halt"] = 0.99  # the replay has no auto-stop; keep it out of the way
        first = 620  # an up-market is ending here, so the 30 days hold both buys and sells
        for step in range(30):  # tick sees bars up to "today"; today's bar is the fill price
            today = DAY0 + (first + step) * DAY
            visible = {s: {d: c for d, c in prices.items() if d <= today} for s, prices in table.items()}
            self.assertEqual(bot.tick(cfg, self.state, now=today + 500, get=fake_market(visible),
                                      log=self.log.append), bot.OK)
        days = [DAY0 + i * DAY for i in range(first + 30)]
        closes = {s: path[:first + 30] for s, path in walks.items()}
        series = {"days": days, "open": closes, "close": closes, "filled": 0}  # fake bars: open == close
        replay = tune.Lab(series, cfg).run(cfg["defaults"], first, first + 30)
        rows = self.journal()
        self.assertEqual(len(rows), 30)
        for row, expected in zip(rows, replay.equity):
            self.assertAlmostEqual(float(row["equity"]), expected, delta=0.006)
        placed = sum(len(r["orders"].split("; ")) for r in rows if r["orders"] != "none")
        self.assertEqual(placed, replay.trades)
        self.assertGreater(placed, 0)


if __name__ == "__main__":
    unittest.main()
