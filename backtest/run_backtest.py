"""
Backtest the GAME-TOTALS engine on the free historical odds dataset
(mlb_odds_dataset.json: 2024 full season + 2025 through 2025-08-16), which
carries closing Over/Under lines + prices across 6 books and final scores.

Honest framings (pick with --ratings):
  * predict 2025 games with 2024 full-season ratings -> STRICT out-of-sample
    (no same-season info at all) = the bankable number.
  * predict 2024 games with 2024 full-season ratings -> in-sample ceiling
    (knows the season it is grading) = optimistic, shown for contrast.
The live model uses current-season-to-date ratings, so it sits between.

Phase 1 collects one record per matched game (model mu_total, consensus closing
line, best O/U prices, consensus no-vig, actual total). Phase 2 sweeps the
distribution dispersion / market blend / edge threshold over those records --
cheap, because mu_total does not depend on those knobs.

NOT shipped in the live model -- validation only.
"""

import argparse
import datetime as dt
import json
import math
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import warnings; warnings.filterwarnings("ignore")

from engine import data, model, predict, teams

ODDS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mlb_odds_dataset.json")

_EXTRA = {"oak": 133, "ath": 133, "athletics": 133, "az": 109, "ari": 109,
          "wsh": 120, "was": 120, "sd": 135, "sf": 137, "tb": 139, "kc": 118,
          "cws": 145, "chw": 145, "wsox": 145}


def _resolve(team_obj):
    for key in ("shortName", "nickname", "fullName", "displayName", "name"):
        v = team_obj.get(key)
        tid = teams.id_from_name(v) if v else None
        if tid:
            return tid
        if v and v.strip().lower() in _EXTRA:
            return _EXTRA[v.strip().lower()]
    return None


def load_odds_index(year_prefix):
    """date -> list of {home_id, away_id, books:[(total,over,under)...],
    home_score, away_score} from the closing line of each book."""
    with open(ODDS_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    idx = {}
    for date_str, games in raw.items():
        if not str(date_str).startswith(year_prefix):
            continue
        rows = []
        for g in games:
            gi = g.get("gameView") or g
            hid = _resolve(gi.get("homeTeam", {}))
            aid = _resolve(gi.get("awayTeam", {}))
            if not hid or not aid:
                continue
            books = []
            for tl in (g.get("odds", {}) or {}).get("totals", []):
                cur = tl.get("currentLine") or {}
                total, ov, un = cur.get("total"), cur.get("overOdds"), cur.get("underOdds")
                if total is not None and ov is not None and un is not None:
                    books.append((float(total), ov, un))
            if not books:
                continue
            rows.append({
                "home_id": hid, "away_id": aid, "books": books,
                "home_score": gi.get("homeTeamScore"),
                "away_score": gi.get("awayTeamScore"),
            })
        idx[date_str] = rows
    return idx


def market_at_consensus(books):
    """Pick the modal closing total, then among books on that number return
    (line, best_over_price, best_under_price, consensus_no_vig_over)."""
    books = [(t, o, u) for (t, o, u) in books
             if not (-100 < o < 100) and not (-100 < u < 100)]
    if not books:
        return None
    totals = [t for (t, _, _) in books]
    # modal line; ties -> median of the modes
    counts = {}
    for t in totals:
        counts[t] = counts.get(t, 0) + 1
    top = max(counts.values())
    modes = sorted(t for t, c in counts.items() if c == top)
    line = modes[len(modes) // 2]
    at_line = [(o, u) for (t, o, u) in books if abs(t - line) < 1e-9]
    best_over = max(o for (o, u) in at_line)
    best_under = max(u for (o, u) in at_line)
    novigs = []
    for o, u in at_line:
        nv_o, _ = model.devig_two_way(o, u)
        if nv_o is not None:
            novigs.append(nv_o)
    cons_over = sum(novigs) / len(novigs) if novigs else None
    return line, best_over, best_under, cons_over


def match(sched_games, odds_rows):
    by_pair = {}
    for r in odds_rows:
        by_pair.setdefault((r["home_id"], r["away_id"]), []).append(r)
    pairs = []
    for g in sched_games:
        cands = by_pair.get((g["home_id"], g["away_id"]))
        if cands:
            pairs.append((g, cands.pop(0)))
    return pairs


def daterange(start, end):
    d = start
    while d <= end:
        yield d.isoformat()
        d += dt.timedelta(days=1)


def collect_records(ratings_year, game_prefix, start, end):
    """Phase 1: one record per matched game with a closing total + final score."""
    odds_idx = load_odds_index(game_prefix)
    bundle = predict.load_bundle(ratings_year)
    recs = []
    for date_str in daterange(start, end):
        if date_str not in odds_idx:
            continue
        try:
            sched = data.fetch_schedule(date_str)
        except Exception:
            continue
        sched = [g for g in sched if g.get("home_score") is not None
                 and g.get("away_score") is not None]
        for g, od in match(sched, odds_idx[date_str]):
            actual = g["home_score"] + g["away_score"]
            mkt = market_at_consensus(od["books"])
            if not mkt:
                continue
            line, best_over, best_under, cons_over = mkt
            mt = predict.model_total(g, bundle, use_weather=False)
            recs.append({
                "mu": mt["mu_total"], "line": line, "actual": actual,
                "best_over": best_over, "best_under": best_under,
                "cons_over": cons_over,
            })
    return recs


def evaluate(recs, dispersion, blend, min_edge, scale=1.0, bankroll=1000.0):
    """Phase 2: calibration + quarter-Kelly betting sim for one knob set."""
    P = dict(model.PARAMS)
    P["nb_dispersion"] = dispersion
    P["market_blend"] = blend

    n = len(recs)
    err = sq = 0.0
    over_hits = over_n = 0                 # actual over-rate vs market line
    brier = 0.0; brier_n = 0
    cal = {i: [0, 0] for i in range(10)}   # model P(over) decile -> [n, actual overs]

    bk = bankroll; start_bk = bankroll
    n_bets = won = push = 0
    staked = 0.0; peak = bankroll; max_dd = 0.0

    for r in recs:
        mu = r["mu"] * scale
        line = r["line"]; actual = r["actual"]
        pmf = model.total_runs_pmf(mu, P)
        p_over, p_under, _ = model.over_under_probs(pmf, line)
        m_over = model.over_prob_no_push(p_over, p_under)

        # actual over/under (skip pushes for the rate)
        if actual != line:
            over_n += 1
            is_over = 1 if actual > line else 0
            over_hits += is_over
            if m_over is not None:
                brier += (m_over - is_over) ** 2; brier_n += 1
                b = min(int(m_over * 10), 9)
                cal[b][0] += 1; cal[b][1] += is_over

        # betting: blend model with consensus no-vig, edge per side
        cons_over = r["cons_over"]
        if cons_over is None or m_over is None:
            continue
        p_over_f = model.blend_with_market(m_over, cons_over, P)
        for side, pf, pcons, price in (
            ("over", p_over_f, cons_over, r["best_over"]),
            ("under", 1 - p_over_f, 1 - cons_over, r["best_under"]),
        ):
            edge = pf - pcons
            if edge < min_edge:
                continue
            stake, _ = model.kelly_stake(pf, price, bk, P)
            if stake <= 0:
                continue
            n_bets += 1; staked += stake; bk -= stake
            won_side = (side == "over" and actual > line) or (side == "under" and actual < line)
            if actual == line:                      # push -> refund
                bk += stake; push += 1
            elif won_side:
                bk += stake * model.american_to_decimal(price); won += 1
            peak = max(peak, bk); max_dd = max(max_dd, peak - bk)

    return {
        "n": n,
        "mean_pred": sum(r["mu"] * scale for r in recs) / n if n else 0,
        "mean_actual": sum(r["actual"] for r in recs) / n if n else 0,
        "rmse": math.sqrt(sum((r["mu"] * scale - r["actual"]) ** 2 for r in recs) / n) if n else 0,
        "over_rate": over_hits / over_n if over_n else 0,
        "brier": brier / brier_n if brier_n else 0,
        "cal": cal,
        "n_bets": n_bets, "won": won, "push": push,
        "staked": staked, "profit": bk - start_bk,
        "roi": (bk - start_bk) / staked if staked else 0,
        "bk": bk, "start_bk": start_bk, "max_dd": max_dd,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ratings", type=int, default=2024)
    ap.add_argument("--games", default=None, help="season of games to grade (default = ratings year)")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--bankroll", type=float, default=1000.0)
    ap.add_argument("--sweep", action="store_true", help="sweep dispersion/blend/edge")
    args = ap.parse_args()

    games_year = int(args.games) if args.games else args.ratings
    prefix = str(games_year)
    start = dt.date.fromisoformat(args.start) if args.start else dt.date(games_year, 3, 1)
    end = dt.date.fromisoformat(args.end) if args.end else dt.date(games_year, 11, 1)

    print(f"Collecting records: games={games_year} ratings={args.ratings} "
          f"{start}..{end}", flush=True)
    recs = collect_records(args.ratings, prefix, start, end)
    print(f"Matched games with closing totals + finals: {len(recs)}", flush=True)
    if not recs:
        return

    if args.sweep:
        print("\n--- dispersion calibration (blend=0.6, edge=3%) ---")
        print("  disp |  RMSE | model-over-Brier")
        for disp in (5, 6, 7, 8, 9, 10, 12):
            r = evaluate(recs, disp, 0.60, 0.03, bankroll=args.bankroll)
            print(f"  {disp:>4} | {r['rmse']:.3f} | {r['brier']:.4f}")
        print("\n--- mu scale check (find total bias) ---")
        base = evaluate(recs, 8, 0.60, 0.03, bankroll=args.bankroll)
        print(f"  mean predicted total: {base['mean_pred']:.2f}  "
              f"mean actual: {base['mean_actual']:.2f}  "
              f"(scale to match = {base['mean_actual']/base['mean_pred']:.3f})")
        print("\n--- edge-threshold sweep (disp=8, blend=0.6) ---")
        print("  edge | bets | win% | push | ROI/stake |  profit  | maxDD")
        for e in (0.01, 0.02, 0.03, 0.04, 0.05, 0.06):
            r = evaluate(recs, 8, 0.60, e, bankroll=args.bankroll)
            decided = r["n_bets"] - r["push"]
            wr = r["won"] / decided if decided else 0
            print(f"  {e:>4.0%} | {r['n_bets']:>4} | {wr:>4.1%} | {r['push']:>4} | "
                  f"{r['roi']:>8.2%} | {r['profit']:>8,.0f} | {r['max_dd']:>6,.0f}")
        return

    # single run report
    r = evaluate(recs, model.PARAMS["nb_dispersion"], model.PARAMS["market_blend"],
                 model.PARAMS["min_edge"], bankroll=args.bankroll)
    print("\n" + "=" * 64)
    print(f"TOTALS BACKTEST  games={games_year}  ratings={args.ratings}")
    print("=" * 64)
    print(f"Games graded: {r['n']}")
    print(f"\n-- Total-runs accuracy --")
    print(f"  mean predicted: {r['mean_pred']:.2f}  mean actual: {r['mean_actual']:.2f}")
    print(f"  RMSE: {r['rmse']:.2f}  | actual Over rate vs market line: {r['over_rate']:.1%}")
    print(f"  model P(Over) Brier: {r['brier']:.4f}")
    print(f"\n-- Calibration (model P(Over) decile -> actual Over rate) --")
    for i in range(10):
        nn, w = r["cal"][i]
        if nn:
            print(f"  {i*10:>2}-{i*10+10:>3}%: actual {w/nn:>5.1%}  (n={nn})")
    decided = r["n_bets"] - r["push"]
    wr = r["won"] / decided if decided else 0
    print(f"\n-- Quarter-Kelly sim (edge>={model.PARAMS['min_edge']:.0%} vs consensus no-vig) --")
    print(f"  Bets: {r['n_bets']} ({r['push']} push) | win%: {wr:.1%}")
    print(f"  Staked: {r['staked']:,.0f} | Net: {r['profit']:+,.0f} | "
          f"ROI/stake: {r['roi']:+.2%}")
    print(f"  Bankroll: {r['start_bk']:,.0f} -> {r['bk']:,.0f} "
          f"({r['bk']/r['start_bk']-1:+.1%}) | maxDD: {r['max_dd']:,.0f}")
    print("=" * 64)


if __name__ == "__main__":
    main()
