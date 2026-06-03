# MLB Game Totals Model — Bet Cards

A self-refreshing web app for pricing MLB **game totals (Over/Under)**. Each day
it pulls the slate + probable starters, projects a total-runs distribution per
game, and shows a clean "bet card" with team colors/logos. You type the book
prices (best book + a sharp book), and it computes fair odds, your edge, and a
quarter-Kelly stake on a $1,000 bankroll — live, in the browser.

Sister model to the MLB Moneyline app; shares its engine and data philosophy.

## What drives the projection

Weighted roughly the way totals actually move:

1. **Starting pitching (heaviest input)** — each starter's run prevention is a
   blend of **Statcast xERA** (official, keyless) and a **self-computed FIP**
   (from K/BB/HR/IP via the MLB Stats API), regressed to league by innings.
   > Note: true **SIERA/xFIP** live only on FanGraphs, which hard-blocks
   > scraping. xERA + FIP is the defensible keyless proxy with the same intent
   > (strip out defense/luck). Labeled honestly in the card (RA9 · FIP · xERA).
2. **Bullpen** — staff RA9 blends the starter (~5.5 IP) with the team bullpen
   (~3.5 IP) ERA.
3. **Park + weather** — static park run factors; temperature + wind (Open-Meteo,
   keyless) along the plate→CF axis for open-air parks; domes skip weather,
   retractables are dampened.
4. **Offense** — team **wOBA** vs league (convex), computed from MLB Stats API.

Expected home + away runs → **μ_total**, spread over a **negative-binomial**
run distribution (dispersion calibrated on 2024–25). That PMF ships to the
browser, so any total you type (7.5, 8, 9.5, …) is priced from the same curve,
with whole-number push handling.

## Reading the card

- **Model proj** — μ_total (expected combined runs).
- **Total line** — type the book's number; seeds to the model proj.
- **Over / Under rows** — enter **Best** (your best price) and **Sharp** (your
  Pinnacle / sharp book). The model fair prob is **blended 55/45 toward the
  sharp** to temper overconfidence, then compared to it for the **Edge**.
- **Bet** — fires when edge ≥ the Min-edge setting; stake is quarter-Kelly
  (switchable to ½ / ⅛ / full Kelly or flat units), capped at 5% of bankroll.

## Daily use

It auto-rebuilds via GitHub Actions; just open the page and pick a date. To run
locally:

```bash
pip install -r requirements.txt
python scripts/build_slate.py            # writes web/data/slate-*.json + index.json
python -m http.server 8770 --directory web   # open http://localhost:8770
```

## ⚠️ Honest performance note

A 2-season out-of-sample backtest (2025 games, 2024 ratings, 1,817 games) shows
the model **does not beat the closing totals market** — ~51–53% win rate,
slightly negative ROI at every edge threshold. Closing MLB totals are highly
efficient. Use this as a **fair-value + line-shopping tool** (catch a book off
the sharp number), not as a closing-line predictor. Full numbers and the
leakage discussion: [`backtest/BACKTEST.md`](backtest/BACKTEST.md).

## Layout

```
engine/      data.py (MLB Stats API + Savant + Open-Meteo), teams.py (colors/
             logos/parks), model.py (NB totals + odds math), predict.py
scripts/     build_slate.py  -> web/data/*.json
web/         index.html, app.js, style.css  (the bet card; all odds math client-side)
backtest/    run_backtest.py, BACKTEST.md  (validation only, not shipped)
```

Data: MLB Stats API + Baseball Savant + Open-Meteo — all official/keyless. No
odds API; you enter prices yourself.
