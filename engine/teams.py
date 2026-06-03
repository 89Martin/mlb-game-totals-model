"""
Canonical MLB team reference table.

Keyed by MLBAM team id (same id used by the MLB Stats API and Baseball Savant),
so everything downstream joins cleanly. Holds display data (name, colors, logo)
and model inputs (park run factor, venue coords + field azimuth for weather).

Park factors are 3-year-ish runs factors (100 = neutral). They are tunable —
adjust in one place and the whole engine follows.

Logos are official MLB static assets keyed by team id:
    https://www.mlbstatic.com/team-logos/{id}.svg
"""

# id: (abbr, name, short, primary_hex, secondary_hex, park_factor, lat, lon, azimuth, roof)
# roof: "open" = weather applies, "retractable" = reduced weather weight, "dome" = none
_TEAMS = {
    108: ("LAA", "Los Angeles Angels", "Angels", "#BA0021", "#003263", 101, 33.80019, -117.88240, 43.6, "open"),
    109: ("AZ",  "Arizona Diamondbacks", "D-backs", "#A71930", "#E3D4AD", 103, 33.44530, -112.06669, 0.0, "retractable"),
    110: ("BAL", "Baltimore Orioles", "Orioles", "#DF4601", "#000000", 102, 39.28379, -76.62169, 31.0, "open"),
    111: ("BOS", "Boston Red Sox", "Red Sox", "#BD3039", "#0C2340", 104, 42.34646, -71.09744, 45.0, "open"),
    112: ("CHC", "Chicago Cubs", "Cubs", "#0E3386", "#CC3433", 100, 41.94817, -87.65550, 37.0, "open"),
    113: ("CIN", "Cincinnati Reds", "Reds", "#C6011F", "#000000", 105, 39.09739, -84.50661, 122.0, "open"),
    114: ("CLE", "Cleveland Guardians", "Guardians", "#00385D", "#E50022", 98, 41.49586, -81.68526, 0.0, "open"),
    115: ("COL", "Colorado Rockies", "Rockies", "#33006F", "#C4CED4", 112, 39.75604, -104.99414, 4.0, "open"),
    116: ("DET", "Detroit Tigers", "Tigers", "#0C2340", "#FA4616", 97, 42.33912, -83.04870, 150.0, "open"),
    117: ("HOU", "Houston Astros", "Astros", "#002D62", "#EB6E1F", 101, 29.75697, -95.35551, 343.0, "retractable"),
    118: ("KC",  "Kansas City Royals", "Royals", "#004687", "#BD9B60", 103, 39.05157, -94.48048, 46.0, "open"),
    119: ("LAD", "Los Angeles Dodgers", "Dodgers", "#005A9C", "#EF3E42", 99, 34.07368, -118.24053, 26.0, "open"),
    120: ("WSH", "Washington Nationals", "Nationals", "#AB0003", "#14225A", 100, 38.87286, -77.00750, 28.0, "open"),
    121: ("NYM", "New York Mets", "Mets", "#002D72", "#FF5910", 98, 40.75753, -73.84559, 13.0, "open"),
    133: ("ATH", "Athletics", "Athletics", "#003831", "#EFB21E", 100, 38.57994, -121.51246, 46.0, "open"),
    134: ("PIT", "Pittsburgh Pirates", "Pirates", "#FDB827", "#27251F", 98, 40.44690, -80.00575, 116.0, "open"),
    135: ("SD",  "San Diego Padres", "Padres", "#2F241D", "#FFC425", 96, 32.70786, -117.15728, 0.0, "open"),
    136: ("SEA", "Seattle Mariners", "Mariners", "#0C2C56", "#005C5C", 96, 47.59133, -122.33251, 49.0, "retractable"),
    137: ("SF",  "San Francisco Giants", "Giants", "#FD5A1E", "#27251F", 96, 37.77838, -122.38945, 85.0, "open"),
    138: ("STL", "St. Louis Cardinals", "Cardinals", "#C41E3A", "#0C2340", 98, 38.62257, -90.19287, 62.0, "open"),
    139: ("TB",  "Tampa Bay Rays", "Rays", "#092C5C", "#8FBCE6", 99, 27.97997, -82.50702, 60.0, "open"),
    140: ("TEX", "Texas Rangers", "Rangers", "#003278", "#C0111F", 101, 32.74730, -97.08182, 30.0, "retractable"),
    141: ("TOR", "Toronto Blue Jays", "Blue Jays", "#134A8E", "#1D2D5C", 102, 43.64155, -79.38915, 345.0, "retractable"),
    142: ("MIN", "Minnesota Twins", "Twins", "#002B5C", "#D31145", 100, 44.98183, -93.27789, 129.0, "open"),
    143: ("PHI", "Philadelphia Phillies", "Phillies", "#E81828", "#002D72", 102, 39.90539, -75.16717, 9.0, "open"),
    144: ("ATL", "Atlanta Braves", "Braves", "#CE1141", "#13274F", 101, 33.89067, -84.46764, 145.0, "open"),
    145: ("CWS", "Chicago White Sox", "White Sox", "#27251F", "#C4CED4", 101, 41.83000, -87.63417, 127.0, "open"),
    146: ("MIA", "Miami Marlins", "Marlins", "#00A3E0", "#EF3340", 97, 25.77796, -80.21952, 128.0, "retractable"),
    147: ("NYY", "New York Yankees", "Yankees", "#0C2340", "#C4CED4", 100, 40.82919, -73.92650, 75.0, "open"),
    158: ("MIL", "Milwaukee Brewers", "Brewers", "#12284B", "#FFC52F", 99, 43.02838, -87.97099, 129.0, "retractable"),
}

# Common alternate names -> id, for joining odds/data feeds that use different labels.
_NAME_ALIASES = {
    "oakland athletics": 133, "athletics": 133, "las vegas athletics": 133,
    "cleveland indians": 114, "tampa bay devil rays": 139,
    "st louis cardinals": 138, "la angels": 108, "la dodgers": 119,
    "chi cubs": 112, "chi white sox": 145, "ny mets": 121, "ny yankees": 147,
    "sf giants": 137, "sd padres": 135, "kansas city": 118, "washington": 120,
}


def all_ids():
    return list(_TEAMS.keys())


def team(team_id):
    """Return a dict of display + model fields for a team id, or None."""
    t = _TEAMS.get(team_id)
    if not t:
        return None
    abbr, name, short, primary, secondary, pf, lat, lon, az, roof = t
    return {
        "id": team_id,
        "abbr": abbr,
        "name": name,
        "short": short,
        "primary": primary,
        "secondary": secondary,
        "logo": f"https://www.mlbstatic.com/team-logos/{team_id}.svg",
        "park_factor": pf,
        "lat": lat,
        "lon": lon,
        "azimuth": az,
        "roof": roof,
    }


def id_from_name(name):
    """Best-effort resolve a team id from a name string (for odds feeds)."""
    if not name:
        return None
    n = name.strip().lower()
    for tid, t in _TEAMS.items():
        if t[1].lower() == n or t[2].lower() == n or t[0].lower() == n:
            return tid
    return _NAME_ALIASES.get(n)
