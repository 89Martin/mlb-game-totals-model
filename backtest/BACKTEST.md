# Game-Totals backtest — honest results

Data: `mlb_odds_dataset.json` (free historical archive) — closing Over/Under
lines + prices across 6 books with final scores. Coverage: **2024 full season**
and **2025 through 2025-08-16**.

Run it:

```bash
# strict out-of-sample (the bankable number): grade 2025 games with 2024 ratings
python backtest/run_backtest.py --ratings 2024 --games 2025 --start 2025-03-27 --end 2025-08-16 --sweep

# in-sample ceiling (leakage, optimistic): grade 2024 with 2024 ratings
python backtest/run_backtest.py --ratings 2024 --games 2024 --start 2024-03-28 --end 2024-10-01 --sweep
```

## Headline

**The model does not have a demonstrable edge over CLOSING MLB totals
out-of-sample.** Closing game totals are one of the most efficient MLB markets,
and a public-data model (no proprietary SIERA, no confirmed lineups, no
real-time injury/bullpen-usage info) does not beat the closing number.

### Strict out-of-sample — 2025 games, 2024 ratings (1,817 games)

| edge | bets | win% | push | ROI/stake | profit ($1k) | maxDD |
|-----:|-----:|-----:|-----:|----------:|-------------:|------:|
| 1%   | 1334 | 51.4%|  55  |  -2.45%   |   -415       |  635  |
| 2%   | 1221 | 51.4%|  50  |  -2.43%   |   -411       |  636  |
| 3%   |  952 | 51.9%|  40  |  -2.64%   |   -403       |  596  |
| **4%** | **717** | **52.8%** | 30 | **-2.04%** | **-294** | 492 |
| 5%   |  514 | 51.7%|  25  |  -3.44%   |   -398       |  561  |
| 6%   |  380 | 50.8%|  20  |  -4.45%   |   -421       |  617  |

- Win rates hover at **51–53%**; breakeven at -110 is **52.4%**. Every
  threshold loses money after vig, even with line-shopping the best of 6 books.
- Total-runs accuracy: mean predicted **8.68** vs actual **8.81** (tiny
  under-bias, ~1.4%); RMSE **4.55** runs — basically the irreducible variance of
  a baseball game.
- Model `P(Over)` Brier at the market line ≈ **0.254**, i.e. *slightly worse
  than a 0.250 coin flip*. The model's lean vs the closing total is noise.

### In-sample ceiling — 2024 games, 2024 ratings (2,387 games)

| edge | bets | win% | ROI/stake | profit ($1k) |
|-----:|-----:|-----:|----------:|-------------:|
| 3%   | 1088 | 56.7%|  +10.76%  |  +5,273      |

This looks fantastic (+10% ROI, 57% win) **and is pure leakage** — the 2024
ratings already encode how those exact 2024 games turned out. The ~13-point ROI
gap between this and the OOS run is the cost of that leakage, and it's why only
the OOS number is bankable. (See the user's standing note: separate OOS from
in-sample and never bank the in-sample figure.)

## What this means for the live tool

The edge threshold was set to **4%** — not because it wins (it doesn't, vs
close), but because it's the least-bad / highest-win-rate OOS bucket and keeps
flags down to genuine disagreements.

Treat a flagged "edge" as a **line-shopping signal** (your best book is off the
sharp/Pinnacle number you typed), **not** as proof the model out-predicts the
market. The real, repeatable edge in totals comes from:
1. catching a soft book before it moves to the sharp consensus, and
2. the model + sharp-book blend giving you a fair-value sanity check.

The model is calibrated and unbiased as a *fair-value estimator*; it is not a
market-beating *closing-line* predictor. Bet accordingly.

## Knobs calibrated here

- `nb_dispersion = 6` (negative-binomial size): lower r / wider tails calibrate
  the at-line `P(Over)` best. Variance ≈ mu + mu²/6.
- `market_blend = 0.55`: leans slightly harder on the sharp book than the
  moneyline model, because the totals model showed no standalone edge.
- `min_edge = 0.04`.
