# Autotrader evidence report

Real daily prices 2021-06-17 to 2026-11-09 (1972 days, 0 gap-filled). Costs charged: 0.35% per trade side. Start: $100.

Read this first: past results do not promise future results, these coins were picked knowing they survived, and a few years is too short to prove an edge.

## 1. The honest test: the bot exactly as it runs

2024-07-01 to 2026-11-09. Trades daily and re-tests its settings every 7 days, each time using only earlier prices. It switched settings 3 time(s).

| $100 run as | Ends at | Per year | Worst dip | Sharpe | Trades |
|---|---|---|---|---|---|
| Self-tuning bot | $112.48 | +5.1% | 25.6% | 0.35 | 138 |
| Same bot, settings never changed (100-day trend, 2% buffer) | $109.85 | +4.1% | 20.9% | 0.31 | 85 |
| Just holding all coins | $80.18 | -8.9% | 62.5% | 0.12 | 3 |

## 2. Whole history with the current settings

2022-01-03 to 2026-11-09. Invested 27% of the stack on average, $7.52 paid in fees.

| $100 run as | Ends at | Per year | Worst dip | Sharpe | Trades |
|---|---|---|---|---|---|
| Bot (100-day trend, 2% buffer) | $150.67 | +8.8% | 27.0% | 0.53 | 177 |
| Just holding all coins | $87.67 | -2.7% | 77.1% | 0.25 | 3 |

## 3. What if trading costs are worse than assumed?

| Cost per trade side | Bot ends at | Worst dip |
|---|---|---|
| 0.18% | $157.24 | 25.9% |
| 0.35% (assumed) | $150.67 | 27.0% |
| 0.70% | $138.75 | 29.0% |
| 1.40% | $118.07 | 32.6% |

## 4. The risk dial (`risk.vol_target` in config.json)

| Bet sizing | Bot ends at | Per year | Worst dip |
|---|---|---|---|
| Careful: 30% yearly swing per coin (current) | $150.67 | +8.8% | 27.0% |
| Medium: 50% yearly swing per coin | $165.29 | +10.9% | 39.8% |
| Full size: no scaling | $167.22 | +11.2% | 49.6% |

## 5. Why this bot trades rarely

Buying and selling once a day at 0.35% per side leaves $7.73 of $100 after a year from costs alone. This bot made 177 trades in 4.9 years.

## 6. This week's tuning decision (2026-11-10 13:11 UTC)

No change: challenger is the current champion.
