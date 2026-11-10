# Autotrader: the $100 game plan

A small robot that trades BTC, ETH and SOL by fixed rules. It runs on **pretend money** until a
human unlocks real trading. It is not connected to any trading account.

## The idea

- **Every trade pays a toll.** Buy and sell once a day at 0.35% a side and $100 becomes about $8
  in a year from tolls alone. So day trading $100 is out. This robot trades about three times a month.
- **Ride the up-escalator, step off the down one.** A coin is held only while its price is above
  its own 100-day average. Otherwise that slice waits in cash.
- **Smaller bets on jumpier coins**, so one wild coin cannot sink the stack. On average only about
  a quarter of the stack is in the market.
- **$100 buys proof, not income.** At this size a good year is a few dollars. The payoff is a
  tested machine that can carry more money later.

## What the replay says

Real prices, fees included, the robot run exactly as it runs live, scored only on days its
settings were not picked on. Snapshot from 2026-11-10; `state/REPORT.md` refreshes it weekly.

| $100 over | Robot | Just holding | Robot's worst dip | Holding's worst dip |
|---|---|---|---|---|
| BTC+ETH+SOL, Jul 2024 to Nov 2026 | $112 | $80 | 26% | 63% |
| BTC+ETH, Jun 2019 to Nov 2026 | $427 | $818 | 23% | 77% |

Read it straight: the robot's dips were about a third the size, and it made a little while holders
lost money in the recent stretch. It also made **less** than holding through the big bull runs. A
few years of data cannot prove an edge, and these coins were picked knowing they survived. Nothing
here is a promise.

## Game plan

| Phase | What happens | Moves on when |
|---|---|---|
| 0. Build | Code, tests, replay on real prices | Done |
| 1. Paper | Robot runs daily on a pretend $100 | 30 days in a row with no missed or failed runs |
| 2. Live $100 | Same rules, real account, trade-only key | 90 days without the auto-stop firing |
| 3. Scale | Add money in steps | Only while phase 2 still holds |

## Safety rules (enforced in code)

- Paper mode by default. Live mode refuses to start: there is no broker code yet, on purpose.
- Long only. No borrowing, no shorting, no leverage.
- Only the coins listed in `config.json`. It never trades your own token.
- Prices are checked against a second exchange. If they disagree by more than 2%, or are stale,
  it does not trade that day and the run fails loudly.
- Auto-stop: if the stack falls 35% from its best level it sells everything and stays stopped
  until a human runs `python -m autotrader resume`. The worst dip in the replays was 30%.

## The risk dial

`risk.vol_target` in `config.json` sets the bet size. Whole replay, Jan 2022 to Nov 2026:

| Setting | Per year | Worst dip |
|---|---|---|
| `0.3` careful (default) | +8.8% | 27% |
| `0.5` medium | +10.9% | 40% |
| `null` full size | +11.2% | 50% |

Turning it up bought little extra return for a lot more pain, and it would trip the 35% auto-stop.

## How it improves itself

Every Monday it replays history with 15 settings. A new setting replaces the current one only if
it was best on older data **and** clearly beats the current one on the latest 180 days, which
neither was picked on. Otherwise nothing changes. Every decision lands in `state/tuning_log.jsonl`.

Honest note: in the replays this self-tuning came out about level with never changing the
settings. It is a guard rail, not a secret weapon. Bigger upgrades (new rules, more markets) are
code changes that must pass the same replay and tests first.

## Where to look

- `state/STATUS.md`: the stack right now, in plain words
- `state/journal.csv`: one row per daily run
- `state/REPORT.md`: the weekly evidence report

## Commands

    python -m autotrader tick        # one daily run
    python -m autotrader tune        # weekly settings test + evidence report
    python -m autotrader backtest    # print the evidence report
    python -m autotrader backtest --coins BTC,ETH --since 2016-06-01
    python -m autotrader status
    python -m autotrader resume      # a human clears the auto-stop
    python -m unittest discover -s autotrader/tests -t .

Python 3.10+, no packages to install.

## Autopilot

`.github/workflows/autotrader.yml` runs the tick daily at 00:17 UTC and the tuning on Mondays,
then commits `state/`. It starts once it is on the default branch and Actions are enabled for the
repo. A skipped or stopped run shows red, and GitHub emails the repo owner.

## Going live needs a human

1. Pick a venue. Open the account in the name of whoever should own the profits (the company, if
   the company is to book them).
2. Create an API key that can trade but **cannot withdraw**. Keep it in a secret store, never in
   this repo.
3. Set `costs` in `config.json` to that venue's real fees.
4. Add and test a broker adapter for that venue. It is the one missing piece.
5. Move the journal to a private repo first: this one is public.

Every trade can be a taxable event. Talk to an accountant before real money moves.
