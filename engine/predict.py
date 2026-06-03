"""
Per-game prediction for game totals: combine the data layer + model into an
expected total-runs distribution (pre-odds), then optionally fold in entered
sharp-book O/U odds for an edge + stake.

Season aggregates (team hitting/pitching, Savant) are passed in as a prefetched
`bundle` so the backtest can load them once and reuse across hundreds of games.
"""

from engine import data, model, teams


def load_bundle(year):
    """Prefetch the season-level data the engine needs."""
    return {
        "year": year,
        "hitting": data.fetch_team_hitting(year),
        "pitching": data.fetch_team_pitching(year),
        "savant_pit": data.fetch_savant_pitchers(year),
    }


def _team_woba(bundle, tid, lg_woba):
    t = bundle["hitting"]["teams"].get(str(tid))
    return t["woba"] if t and t.get("woba") else lg_woba


def _team_era(bundle, tid, lg_era):
    t = bundle["pitching"]["teams"].get(str(tid))
    return t["era"] if t and t.get("era") else lg_era


def model_total(game, bundle, use_weather=True, p=model.PARAMS):
    """Return the model's total-runs distribution + transparency detail."""
    year = bundle["year"]
    lg_woba = bundle["hitting"]["lg_woba"]
    lg_rg = bundle["hitting"]["lg_rg"]
    lg_era = bundle["pitching"]["lg_era"]

    home = teams.team(game["home_id"])
    away = teams.team(game["away_id"])
    park = home["park_factor"] if home else 100

    # starters
    h_sp_line = data.fetch_pitcher_season(game["home_sp_id"], year) if game.get("home_sp_id") else {}
    a_sp_line = data.fetch_pitcher_season(game["away_sp_id"], year) if game.get("away_sp_id") else {}
    h_sp_sav = bundle["savant_pit"].get(str(game.get("home_sp_id")))
    a_sp_sav = bundle["savant_pit"].get(str(game.get("away_sp_id")))
    h_sp_ra9, h_sp_detail = model.starter_ra9(h_sp_line, h_sp_sav, lg_era, p)
    a_sp_ra9, a_sp_detail = model.starter_ra9(a_sp_line, a_sp_sav, lg_era, p)

    # staff = starter + bullpen
    h_staff = model.staff_ra9(h_sp_ra9, _team_era(bundle, game["home_id"], lg_era), p)
    a_staff = model.staff_ra9(a_sp_ra9, _team_era(bundle, game["away_id"], lg_era), p)

    # weather (open/retractable parks)
    wx_mult, wx_detail = 1.0, {"applied": False}
    if use_weather and home:
        date_str = (game.get("gameTime") or "")[:10] or None
        if date_str:
            try:
                wx = data.fetch_weather(home["lat"], home["lon"], date_str)
                wx_mult, wx_detail = model.weather_multiplier(wx, home["azimuth"], home["roof"], p)
            except Exception:
                pass

    h_woba = _team_woba(bundle, game["home_id"], lg_woba)
    a_woba = _team_woba(bundle, game["away_id"], lg_woba)

    # away offense faces the home staff; home offense faces the away staff
    er_home = model.expected_runs(h_woba, lg_woba, lg_rg, a_staff, lg_era, park, wx_mult, p)
    er_away = model.expected_runs(a_woba, lg_woba, lg_rg, h_staff, lg_era, park, wx_mult, p)
    mu_total = er_home + er_away

    pmf = model.total_runs_pmf(mu_total, p)
    return {
        "mu_total": mu_total,
        "er_home": round(er_home, 2),
        "er_away": round(er_away, 2),
        "pmf": pmf,
        "detail": {
            "home_sp": {"name": game.get("home_sp_name"), "ra9": round(h_sp_ra9, 2), **h_sp_detail},
            "away_sp": {"name": game.get("away_sp_name"), "ra9": round(a_sp_ra9, 2), **a_sp_detail},
            "home_staff_ra9": round(h_staff, 2),
            "away_staff_ra9": round(a_staff, 2),
            "home_woba": h_woba, "away_woba": a_woba,
            "park_factor": park, "weather": wx_detail,
        },
    }


def evaluate_total(game, bundle, line, odds=None, bankroll=1000,
                   use_weather=True, p=model.PARAMS):
    """Full evaluation at a given total `line`: model O/U probs blended with the
    sharp book + staking per side.

    `odds` (optional): {'over_best','under_best','over_sharp','under_sharp'}
    """
    mt = model_total(game, bundle, use_weather, p)
    odds = odds or {}

    p_over, p_under, p_push = model.over_under_probs(mt["pmf"], line)
    model_over = model.over_prob_no_push(p_over, p_under)        # no-push conditional

    over_sharp, under_sharp = model.devig_two_way(odds.get("over_sharp"), odds.get("under_sharp"))
    p_over_final = model.blend_with_market(model_over, over_sharp, p) if model_over is not None else None
    p_under_final = (1 - p_over_final) if p_over_final is not None else None
    if over_sharp is not None:
        under_sharp = 1 - over_sharp

    over_eval = model.evaluate_side(p_over_final, over_sharp, odds.get("over_best"), bankroll, p)
    under_eval = model.evaluate_side(p_under_final, under_sharp, odds.get("under_best"), bankroll, p)

    return {
        "line": line,
        "model": {"mu_total": round(mt["mu_total"], 2),
                  "er_home": mt["er_home"], "er_away": mt["er_away"],
                  "p_over": round(p_over, 4), "p_under": round(p_under, 4),
                  "p_push": round(p_push, 4)},
        "over": over_eval,
        "under": under_eval,
        "detail": mt["detail"],
    }
