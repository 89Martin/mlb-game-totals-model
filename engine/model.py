"""
Game-total (Over/Under) engine + market comparison + quarter-Kelly staking.

Flow for one game:
  1. Rate each starting pitcher's run prevention (blend Statcast xERA + computed
     FIP, regressed to league by sample size).  -- pitching is the heaviest input
  2. Build each side's pitching staff RA9 as a starter/bullpen innings blend.
  3. Estimate each team's expected runs: league R/G * offense factor (team wOBA
     vs league, convex) * opposing-staff factor * park * weather.
  4. Total runs mu = expected home runs + expected away runs.
  5. Spread mu over a discrete run distribution (negative binomial, dispersion
     calibrated on the backtest) -> P(total = k) for k = 0..MAX_RUNS.
     From that PMF, P(Over line) / P(Under line) for any entered total.
  6. De-vig the entered sharp-book O/U line and blend it with the model to
     temper model overconfidence (this is what the "sharp book" input is for).
  7. Size the bet with quarter-Kelly on the best available O/U price, gated by a
     minimum edge vs the sharp book and a max-stake cap.

Every knob lives in PARAMS so the backtest can calibrate them without touching
the logic.  The PMF is what gets shipped to the browser per game, so the live
page can price ANY total the user types using the exact same distribution.
"""

import math

PARAMS = {
    # --- pitching ---
    "fip_constant": 3.15,      # scales FIP into ERA units
    "sp_regress_ip": 60.0,     # IP at which a starter is ~half league-regressed
    "xera_weight": 0.5,        # blend weight on Statcast xERA vs computed FIP
    "sp_innings": 5.5,         # innings credited to the starter
    "bp_innings": 3.5,         # innings credited to the bullpen
    "bullpen_adj": 0.97,       # bullpen RA9 vs team overall ERA (pens ~ a touch better)
    # --- offense / environment ---
    "off_exponent": 1.5,       # convexity of runs in (team wOBA / league wOBA)
    "wx_temp_ref": 70.0,       # neutral temperature (F)
    "wx_temp_per_deg": 0.0007, # run multiplier change per degree above ref
    "wx_wind_out": 0.006,      # run mult per mph of wind blowing out
    "wx_wind_in": 0.005,       # run mult per mph of wind blowing in
    # --- total-runs distribution ---
    "nb_dispersion": 6.0,      # negative-binomial size r (var = mu + mu^2/r); calibrated
                               # on 2024-25: lower r (wider tails) calibrates the at-line
                               # P(Over) best -- the model adds little directional signal
                               # vs the closing total, so we don't run it overconfident.
    "max_runs": 32,            # PMF support 0..max_runs (tail folded into last bucket)
    # --- market / staking ---
    "market_blend": 0.55,      # weight on model vs sharp-book no-vig in final prob.
                               # Leans harder on the sharp book than the ML model (0.60)
                               # because totals OOS showed no market-beating edge.
    "min_edge": 0.04,          # min edge vs sharp book to flag a bet. The OOS backtest
                               # never turned a profit vs CLOSING totals; 4% is the
                               # least-bad / highest win-rate bucket and keeps flags to
                               # genuine line-shopping gaps (your best book vs the sharp).
    "kelly_fraction": 0.25,    # quarter Kelly
    "max_stake_frac": 0.05,    # cap any single bet at 5% of bankroll
}


# --------------------------------------------------------------------------- #
# pitching
# --------------------------------------------------------------------------- #
def compute_fip(line, fip_constant):
    """FIP from a season pitching line dict; None if no innings."""
    ip = line.get("ip", 0) or 0
    if ip <= 0:
        return None
    return (13 * line["hr"] + 3 * (line["bb"] + line["hbp"]) - 2 * line["k"]) / ip + fip_constant


def starter_ra9(sp_line, savant, lg_era, p=PARAMS):
    """Run-prevention RA9 for a starter: blend xERA + FIP, regress to league.

    Returns (ra9, detail dict). Falls back gracefully to whatever is available,
    finally to league average if the starter is unknown."""
    fip = compute_fip(sp_line, p["fip_constant"]) if sp_line else None
    xera = savant.get("xera") if savant else None
    ip = (sp_line or {}).get("ip", 0) or 0

    if xera is not None and fip is not None:
        raw = p["xera_weight"] * xera + (1 - p["xera_weight"]) * fip
    elif xera is not None:
        raw = xera
    elif fip is not None:
        raw = fip
    else:
        return lg_era, {"source": "league", "fip": None, "xera": None}

    # regress toward league by sample size
    w = ip / (ip + p["sp_regress_ip"]) if ip > 0 else 0.0
    ra9 = w * raw + (1 - w) * lg_era
    return ra9, {"source": "blend", "fip": round(fip, 2) if fip else None,
                 "xera": xera, "raw": round(raw, 2), "regress_w": round(w, 2)}


def staff_ra9(sp_ra9, bullpen_era, p=PARAMS):
    """Blend starter + bullpen into a single staff RA9 over a 9-inning game."""
    bp = bullpen_era * p["bullpen_adj"]
    return (p["sp_innings"] * sp_ra9 + p["bp_innings"] * bp) / (p["sp_innings"] + p["bp_innings"])


# --------------------------------------------------------------------------- #
# environment
# --------------------------------------------------------------------------- #
def weather_multiplier(wx, azimuth, roof, p=PARAMS):
    """Run multiplier from temperature + wind (open-air parks only)."""
    if not wx or roof == "dome":
        return 1.0, {"applied": False}
    scale = 1.0 if roof == "open" else 0.35  # retractable: dampen
    mult = 1.0
    temp = wx.get("temp_f")
    if temp is not None:
        mult *= 1.0 + scale * (temp - p["wx_temp_ref"]) * p["wx_temp_per_deg"]
    wind = wx.get("wind_mph")
    wdir = wx.get("wind_dir")
    detail = {"applied": True, "temp_f": temp, "wind_mph": wind, "wind_dir": wdir}
    if wind and wdir is not None and azimuth is not None:
        # wind_dir is the direction wind comes FROM; component along plate->CF axis.
        diff = math.radians((wdir - azimuth + 180) % 360 - 180)
        along = math.cos(diff)  # +1 = straight out to CF, -1 = straight in
        if along >= 0:
            mult *= 1.0 + scale * wind * along * p["wx_wind_out"]
        else:
            mult *= 1.0 + scale * wind * along * p["wx_wind_in"]
        detail["carry"] = round(along, 2)
    return mult, detail


# --------------------------------------------------------------------------- #
# core: expected runs
# --------------------------------------------------------------------------- #
def expected_runs(off_woba, lg_woba, lg_rg, opp_staff_ra9, lg_era,
                  park_factor, wx_mult, p=PARAMS):
    off_factor = (off_woba / lg_woba) ** p["off_exponent"] if lg_woba else 1.0
    pit_factor = opp_staff_ra9 / lg_era if lg_era else 1.0
    return lg_rg * off_factor * pit_factor * (park_factor / 100.0) * wx_mult


# --------------------------------------------------------------------------- #
# total-runs distribution (negative binomial)
# --------------------------------------------------------------------------- #
def total_runs_pmf(mu_total, p=PARAMS):
    """Discrete distribution of total runs in a game.

    Negative binomial with mean mu and size r (= nb_dispersion): variance is
    mu + mu^2/r, which lets us match MLB's over-dispersion vs a plain Poisson.
    Returns a list pmf[0..max_runs] that sums to 1 (tail folded into the last
    bucket).  This array is what the browser uses to price any entered total."""
    r = p["nb_dispersion"]
    n = p["max_runs"]
    if mu_total <= 0:
        out = [0.0] * (n + 1)
        out[0] = 1.0
        return out
    prob = r / (r + mu_total)            # "success" prob in NB parameterization
    log_prob = math.log(prob)
    log_1mp = math.log(1 - prob)
    pmf = []
    for k in range(n + 1):
        logpmf = (math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1)
                  + r * log_prob + k * log_1mp)
        pmf.append(math.exp(logpmf))
    s = sum(pmf)
    if s > 0:                            # normalize so folded tail mass is kept
        pmf = [x / s for x in pmf]
    return pmf


def over_under_probs(pmf, line):
    """From a runs PMF + a total line, return (p_over, p_under, p_push).

    Half lines (e.g. 8.5) never push. Whole lines (e.g. 8) push when the game
    lands exactly on the number."""
    floor_line = math.floor(line)
    is_whole = abs(line - round(line)) < 1e-9
    p_push = pmf[int(round(line))] if is_whole and 0 <= round(line) < len(pmf) else 0.0
    # Over wins on totals strictly greater than the line.
    p_over = sum(pmf[k] for k in range(len(pmf)) if k > line)
    p_under = sum(pmf[k] for k in range(len(pmf)) if k < line)
    return p_over, p_under, p_push


def over_prob_no_push(p_over, p_under):
    """Model Over probability conditioned on no push, so it is directly
    comparable to a two-way de-vigged market."""
    tot = p_over + p_under
    return p_over / tot if tot > 0 else None


# --------------------------------------------------------------------------- #
# market: odds conversion, de-vig, blend
# --------------------------------------------------------------------------- #
def american_to_prob(odds):
    # valid american odds are <= -100 or >= +100; anything else is bad data
    if odds is None or -100 < odds < 100:
        return None
    return 100.0 / (odds + 100.0) if odds > 0 else (-odds) / (-odds + 100.0)


def american_to_decimal(odds):
    if odds is None:
        return None
    return 1 + (odds / 100.0 if odds > 0 else 100.0 / -odds)


def prob_to_american(prob):
    if not prob or prob <= 0 or prob >= 1:
        return None
    return round(-100 * prob / (1 - prob)) if prob > 0.5 else round(100 * (1 - prob) / prob)


def devig_two_way(over_odds, under_odds):
    """No-vig probabilities from a two-sided market (proportional method)."""
    po, pu = american_to_prob(over_odds), american_to_prob(under_odds)
    if po is None or pu is None:
        return None, None
    tot = po + pu
    if tot <= 0:
        return None, None
    return po / tot, pu / tot


def blend_with_market(p_model, p_market_novig, p=PARAMS):
    if p_market_novig is None:
        return p_model
    w = p["market_blend"]
    return w * p_model + (1 - w) * p_market_novig


# --------------------------------------------------------------------------- #
# staking: quarter Kelly
# --------------------------------------------------------------------------- #
def kelly_stake(prob, best_odds, bankroll, p=PARAMS):
    """Recommended stake ($) for a side, quarter-Kelly, capped. 0 if no value."""
    dec = american_to_decimal(best_odds)
    if dec is None or prob is None:
        return 0.0, 0.0
    b = dec - 1
    if b <= 0:
        return 0.0, 0.0
    full = (b * prob - (1 - prob)) / b
    if full <= 0:
        return 0.0, 0.0
    frac = min(full * p["kelly_fraction"], p["max_stake_frac"])
    return round(bankroll * frac, 2), round(frac, 4)


def evaluate_side(p_final, sharp_novig, best_odds, bankroll, p=PARAMS):
    """Edge vs sharp book + stake for one side (Over or Under)."""
    edge = (p_final - sharp_novig) if sharp_novig is not None else None
    fair = prob_to_american(p_final)
    stake, frac = (0.0, 0.0)
    is_bet = False
    if best_odds is not None and edge is not None and edge >= p["min_edge"]:
        stake, frac = kelly_stake(p_final, best_odds, bankroll, p)
        is_bet = stake > 0
    return {
        "model_prob": round(p_final, 4),
        "fair_odds": fair,
        "sharp_novig": round(sharp_novig, 4) if sharp_novig is not None else None,
        "edge": round(edge, 4) if edge is not None else None,
        "best_odds": best_odds,
        "stake": stake,
        "stake_frac": frac,
        "is_bet": is_bet,
    }
