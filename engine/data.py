"""
Data layer for the MLB moneyline model.

Sources, in order of how load-bearing they are:
  * MLB Stats API  (official, stable)  -> schedule, probable starters, team &
                                          pitcher season stats, splits, rest
  * Baseball Savant (official Statcast) -> xERA, xwOBA, hard-hit%, barrels
  * Open-Meteo      (free, no key)     -> temperature + wind for weather/carry

FanGraphs is deliberately NOT in the critical path (its scraper endpoint is
403-blocked); the metrics it uniquely provides (wRC+/xFIP) are treated as
optional polish elsewhere. Everything here stands on official sources + a few
quantities we compute ourselves (FIP, team wOBA).

All network reads go through a dated disk cache so the daily job pulls each
source once, and a fetch failure falls back to the most recent cached copy.
"""

import io
import json
import os
import time
import datetime as dt

import requests

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

_UA = {"User-Agent": "Mozilla/5.0 (mlb-ml-model; personal use)"}
_TIMEOUT = 30

# ----- wOBA weights (fixed; league mean is computed live so weight-year drift
#       cancels out when we normalize team wOBA to the league) ----------------
_WOBA = dict(BB=0.690, HBP=0.722, S=0.888, D=1.271, T=1.616, HR=2.101)


# --------------------------------------------------------------------------- #
# cache helpers
# --------------------------------------------------------------------------- #
def _cache_file(key):
    safe = key.replace("/", "_").replace(":", "_")
    return os.path.join(CACHE_DIR, safe + ".json")


def _read_cache(key, max_age_hours=None):
    path = _cache_file(key)
    if not os.path.exists(path):
        return None
    if max_age_hours is not None:
        age = (time.time() - os.path.getmtime(path)) / 3600.0
        if age > max_age_hours:
            return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_cache(key, obj):
    try:
        with open(_cache_file(key), "w", encoding="utf-8") as f:
            json.dump(obj, f)
    except Exception:
        pass


def _cached(key, builder, max_age_hours):
    """Return fresh data if within max_age; else rebuild; on failure fall back
    to any stale cached copy so the pipeline never hard-fails."""
    fresh = _read_cache(key, max_age_hours=max_age_hours)
    if fresh is not None:
        return fresh, "cache"
    try:
        data = builder()
        _write_cache(key, data)
        return data, "live"
    except Exception as e:
        stale = _read_cache(key, max_age_hours=None)
        if stale is not None:
            return stale, f"stale ({e.__class__.__name__})"
        raise


# --------------------------------------------------------------------------- #
# MLB Stats API
# --------------------------------------------------------------------------- #
_API = "https://statsapi.mlb.com/api/v1"


def _get(url, params=None):
    r = requests.get(url, params=params, headers=_UA, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


def fetch_schedule(date_str):
    """Games for a date (YYYY-MM-DD) with probable starters and pitcher hands."""
    def build():
        d = _get(f"{_API}/schedule", {
            "sportId": 1, "date": date_str,
            "hydrate": "probablePitcher,team,venue,linescore",
        })
        games = []
        for day in d.get("dates", []):
            for g in day.get("games", []):
                away, home = g["teams"]["away"], g["teams"]["home"]
                ap = away.get("probablePitcher") or {}
                hp = home.get("probablePitcher") or {}
                games.append({
                    "gamePk": g["gamePk"],
                    "gameTime": g.get("gameDate"),
                    "status": g.get("status", {}).get("abstractGameState"),
                    "venue_id": g.get("venue", {}).get("id"),
                    "away_id": away["team"]["id"],
                    "home_id": home["team"]["id"],
                    "away_sp_id": ap.get("id"),
                    "away_sp_name": ap.get("fullName"),
                    "home_sp_id": hp.get("id"),
                    "home_sp_name": hp.get("fullName"),
                    # final scores (used by the backtest; null for upcoming games)
                    "away_score": away.get("score"),
                    "home_score": home.get("score"),
                })
        return games
    # short cache: probables change through the morning
    data, _ = _cached(f"schedule_{date_str}", build, max_age_hours=3)
    return data


def _hand(pid):
    """Pitching hand 'L'/'R' for a player id."""
    try:
        d = _get(f"{_API}/people/{pid}")
        return d["people"][0].get("pitchHand", {}).get("code")
    except Exception:
        return None


def fetch_pitcher_hand(pid):
    if pid is None:
        return None
    data, _ = _cached(f"hand_{pid}", lambda: {"hand": _hand(pid)}, max_age_hours=720)
    return data.get("hand")


def fetch_pitcher_season(pid, year):
    """A starter's season pitching line -> compute FIP; also K%/BB% and IP."""
    def build():
        d = _get(f"{_API}/people/{pid}/stats",
                 {"stats": "season", "group": "pitching", "season": year})
        stats = d.get("stats") or []
        splits = (stats[0].get("splits", []) if stats else [])
        if not splits:
            return {}
        s = splits[0]["stat"]
        ip = float(s.get("inningsPitched", 0) or 0)
        bb = float(s.get("baseOnBalls", 0) or 0)
        hbp = float(s.get("hitByPitch", 0) or 0)
        k = float(s.get("strikeOuts", 0) or 0)
        hr = float(s.get("homeRuns", 0) or 0)
        bf = float(s.get("battersFaced", 0) or 0)
        era = float(s.get("era", 0) or 0)
        return {"ip": ip, "bb": bb, "hbp": hbp, "k": k, "hr": hr,
                "bf": bf, "era": era, "gs": int(s.get("gamesStarted", 0) or 0)}
    data, _ = _cached(f"sp_{pid}_{year}", build, max_age_hours=18)
    return data


def fetch_team_hitting(year):
    """Per-team season hitting -> computed team wOBA, plus league mean wOBA."""
    def build():
        d = _get(f"{_API}/teams/stats",
                 {"sportId": 1, "season": year, "stats": "season", "group": "hitting"})
        splits = d.get("stats", [{}])[0].get("splits", [])
        teams, lg_num, lg_den = {}, 0.0, 0.0
        for sp in splits:
            tid = sp.get("team", {}).get("id")
            s = sp["stat"]
            h = float(s.get("hits", 0) or 0)
            d2 = float(s.get("doubles", 0) or 0)
            t3 = float(s.get("triples", 0) or 0)
            hr = float(s.get("homeRuns", 0) or 0)
            s1 = h - d2 - t3 - hr
            bb = float(s.get("baseOnBalls", 0) or 0)
            ibb = float(s.get("intentionalWalks", 0) or 0)
            ubb = max(bb - ibb, 0)
            hbp = float(s.get("hitByPitch", 0) or 0)
            ab = float(s.get("atBats", 0) or 0)
            sf = float(s.get("sacFlies", 0) or 0)
            num = (_WOBA["BB"] * ubb + _WOBA["HBP"] * hbp + _WOBA["S"] * s1 +
                   _WOBA["D"] * d2 + _WOBA["T"] * t3 + _WOBA["HR"] * hr)
            den = ab + bb - ibb + sf + hbp
            woba = num / den if den else 0
            teams[str(tid)] = {"woba": round(woba, 4), "pa": float(s.get("plateAppearances", 0) or 0),
                               "runs": float(s.get("runs", 0) or 0),
                               "games": float(s.get("gamesPlayed", 0) or 0)}
            lg_num += num
            lg_den += den
        lg_woba = lg_num / lg_den if lg_den else 0.310
        total_r = sum(t["runs"] for t in teams.values())
        total_g = sum(t["games"] for t in teams.values())
        lg_rg = (total_r / total_g) if total_g else 4.4
        return {"teams": teams, "lg_woba": round(lg_woba, 4), "lg_rg": round(lg_rg, 3)}
    data, _ = _cached(f"team_hitting_{year}", build, max_age_hours=18)
    return data


def fetch_team_pitching(year):
    """Per-team season pitching (overall) -> team ERA/FIP and league ERA."""
    def build():
        d = _get(f"{_API}/teams/stats",
                 {"sportId": 1, "season": year, "stats": "season", "group": "pitching"})
        splits = d.get("stats", [{}])[0].get("splits", [])
        teams, lg_er, lg_ip = {}, 0.0, 0.0
        for sp in splits:
            tid = sp.get("team", {}).get("id")
            s = sp["stat"]
            ip = float(s.get("inningsPitched", 0) or 0)
            era = float(s.get("era", 0) or 0)
            teams[str(tid)] = {"era": era, "ip": ip,
                               "k": float(s.get("strikeOuts", 0) or 0),
                               "bb": float(s.get("baseOnBalls", 0) or 0),
                               "hr": float(s.get("homeRuns", 0) or 0)}
            lg_er += float(s.get("earnedRuns", 0) or 0)
            lg_ip += ip
        lg_era = (lg_er * 9 / lg_ip) if lg_ip else 4.1
        return {"teams": teams, "lg_era": round(lg_era, 3)}
    data, _ = _cached(f"team_pitching_{year}", build, max_age_hours=18)
    return data


# --------------------------------------------------------------------------- #
# Baseball Savant (Statcast) -- official, returns advanced metrics
# --------------------------------------------------------------------------- #
def _savant_csv(url):
    import pandas as pd
    r = requests.get(url, headers=_UA, timeout=_TIMEOUT)
    r.raise_for_status()
    return pd.read_csv(io.StringIO(r.text))


def fetch_savant_pitchers(year):
    """xERA / xwOBA-against by pitcher_id from Statcast expected stats."""
    def build():
        url = (f"https://baseballsavant.mlb.com/leaderboard/expected_statistics"
               f"?type=pitcher&year={year}&position=&team=&filterType=bip&min=10&csv=true")
        df = _savant_csv(url)
        out = {}
        for _, row in df.iterrows():
            pid = int(row["player_id"])
            out[str(pid)] = {
                "xera": float(row["xera"]) if row.get("xera") == row.get("xera") else None,
                "xwoba": float(row["est_woba"]) if row.get("est_woba") == row.get("est_woba") else None,
                "woba": float(row["woba"]) if row.get("woba") == row.get("woba") else None,
                "pa": float(row.get("pa", 0) or 0),
            }
        return out
    data, _ = _cached(f"savant_pit_{year}", build, max_age_hours=24)
    return data


def fetch_savant_batters(year):
    """xwOBA by batter_id from Statcast expected stats (offense polish)."""
    def build():
        url = (f"https://baseballsavant.mlb.com/leaderboard/expected_statistics"
               f"?type=batter&year={year}&position=&team=&filterType=bip&min=10&csv=true")
        df = _savant_csv(url)
        out = {}
        for _, row in df.iterrows():
            pid = int(row["player_id"])
            out[str(pid)] = {
                "xwoba": float(row["est_woba"]) if row.get("est_woba") == row.get("est_woba") else None,
                "woba": float(row["woba"]) if row.get("woba") == row.get("woba") else None,
            }
        return out
    data, _ = _cached(f"savant_bat_{year}", build, max_age_hours=24)
    return data


# --------------------------------------------------------------------------- #
# Weather (Open-Meteo, free, no key) -- only meaningful for open-air parks
# --------------------------------------------------------------------------- #
def fetch_weather(lat, lon, date_str, hour=19):
    """Temp (F) + wind speed (mph) + wind direction (deg) near game time."""
    def build():
        d = _get("https://api.open-meteo.com/v1/forecast", {
            "latitude": lat, "longitude": lon,
            "hourly": "temperature_2m,wind_speed_10m,wind_direction_10m",
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
            "start_date": date_str, "end_date": date_str, "timezone": "auto",
        })
        h = d.get("hourly", {})
        times = h.get("time", [])
        idx = min(range(len(times)), key=lambda i: abs(int(times[i][11:13]) - hour)) if times else None
        if idx is None:
            return {}
        return {
            "temp_f": h["temperature_2m"][idx],
            "wind_mph": h["wind_speed_10m"][idx],
            "wind_dir": h["wind_direction_10m"][idx],
        }
    # weather forecast is only useful for near-future dates
    data, _ = _cached(f"wx_{lat}_{lon}_{date_str}", build, max_age_hours=6)
    return data
