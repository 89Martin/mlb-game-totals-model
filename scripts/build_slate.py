"""
Build the game-totals slate for today + the next few days: pull each day's games
+ probable starters, run the model, and write web/data/slate-YYYY-MM-DD.json plus
an index.json the Bet Card page uses for its date picker.

Each game ships its full total-runs PMF, so the browser can price ANY total the
user types (Over/Under at 7.5, 8, 9.5, ...) with the same distribution the model
produced. Odds are NOT included -- the user enters best + sharp-book O/U prices
in the browser, and the page does the de-vig / blend / edge / Kelly math
client-side using the params embedded here.

Usage:
    python scripts/build_slate.py [--date YYYY-MM-DD] [--season YYYY] [--days N]
"""

import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import warnings; warnings.filterwarnings("ignore")

from engine import data, model, predict, teams

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "web", "data")

PARAM_KEYS = ["market_blend", "min_edge", "kelly_fraction", "max_stake_frac"]


def pick_season(today):
    """Use the current MLB season year (Apr-Oct); before Apr, use prior year."""
    return today.year if today.month >= 4 else today.year - 1


def team_card(tid):
    t = teams.team(tid)
    if not t:
        return {"id": tid, "abbr": "?", "name": "Unknown", "short": "Unknown",
                "primary": "#444", "secondary": "#888", "logo": ""}
    return {k: t[k] for k in ("id", "abbr", "name", "short", "primary", "secondary", "logo")}


def build_slate(date_str, season, bundle, bankroll, generated_at):
    """Build one day's slate dict."""
    games_raw = data.fetch_schedule(date_str)
    cards = []
    for g in games_raw:
        mt = predict.model_total(g, bundle, use_weather=True)
        d = mt["detail"]
        # round the PMF to keep slate.json small; renormalize client-side is not
        # needed since rounding error is < 1e-4 across ~33 buckets.
        pmf = [round(x, 6) for x in mt["pmf"]]
        cards.append({
            "gamePk": g["gamePk"],
            "gameTime": g.get("gameTime"),
            "status": g.get("status"),
            "away": team_card(g["away_id"]),
            "home": team_card(g["home_id"]),
            "away_sp": {"name": g.get("away_sp_name") or "TBD",
                        "ra9": d["away_sp"]["ra9"], "fip": d["away_sp"].get("fip"),
                        "xera": d["away_sp"].get("xera")},
            "home_sp": {"name": g.get("home_sp_name") or "TBD",
                        "ra9": d["home_sp"]["ra9"], "fip": d["home_sp"].get("fip"),
                        "xera": d["home_sp"].get("xera")},
            "model": {"mu_total": round(mt["mu_total"], 2),
                      "er_home": mt["er_home"], "er_away": mt["er_away"]},
            "pmf": pmf,
            "park_factor": d["park_factor"],
            "weather": d["weather"],
        })
    return {
        "date": date_str,
        "season": season,
        "generated_at": generated_at,
        "bankroll": bankroll,
        "params": {k: model.PARAMS[k] for k in PARAM_KEYS},
        "league": {"woba": bundle["hitting"]["lg_woba"],
                   "rg": bundle["hitting"]["lg_rg"],
                   "era": bundle["pitching"]["lg_era"]},
        "games": cards,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="start date (default today)")
    ap.add_argument("--season", type=int, default=None)
    ap.add_argument("--bankroll", type=float, default=1000.0)
    ap.add_argument("--days", type=int, default=3, help="how many days to build, starting today")
    args = ap.parse_args()

    start = dt.date.today() if not args.date else dt.date.fromisoformat(args.date)
    season = args.season or pick_season(start)
    generated_at = dt.datetime.utcnow().isoformat() + "Z"

    print(f"Loading {season} ratings bundle...", flush=True)
    bundle = predict.load_bundle(season)
    os.makedirs(DATA_DIR, exist_ok=True)

    built = []
    for i in range(max(args.days, 1)):
        date_str = (start + dt.timedelta(days=i)).isoformat()
        slate = build_slate(date_str, season, bundle, args.bankroll, generated_at)
        path = os.path.join(DATA_DIR, f"slate-{date_str}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(slate, f, indent=2)
        built.append({"date": date_str, "games": len(slate["games"])})
        print(f"  {date_str}: {len(slate['games'])} games", flush=True)

    index = {
        "generated_at": generated_at,
        "today": start.isoformat(),
        "season": season,
        "dates": built,
    }
    with open(os.path.join(DATA_DIR, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)
    print(f"Wrote index.json ({len(built)} days) -> {DATA_DIR}", flush=True)


if __name__ == "__main__":
    main()
