"""
Loads/refreshes NHL reference data from the NHL's public web API
(api-web.nhle.com): teams (+ the nicknames analysts actually say), active
rosters (+ name aliases for fuzzy matching), and the season schedule with
scores. Run at the start of every pipeline run - rosters and game states
change daily, and it takes a few seconds.

Usage:
    python load_nhl.py
"""
import time
import unicodedata
from datetime import datetime
import requests
from db import get_conn

API = "https://api-web.nhle.com/v1"

# What hockey people call teams besides the official nickname.
SLANG = {
    "MTL": ["Habs", "Canadiens", "Montreal"], "TOR": ["Leafs", "Buds", "Maple Leafs"],
    "PIT": ["Pens"], "WSH": ["Caps", "Washington"], "NYI": ["Isles", "Islanders"],
    "NSH": ["Preds"], "CHI": ["Hawks", "Blackhawks"], "DET": ["Wings", "Red Wings"],
    "TBL": ["Bolts", "Lightning", "Tampa"], "CAR": ["Canes", "Hurricanes"],
    "CBJ": ["Jackets", "Blue Jackets", "Columbus"], "VGK": ["Knights", "Golden Knights", "Vegas"],
    "OTT": ["Sens", "Senators"], "COL": ["Avs", "Avalanche"], "UTA": ["Utah", "Mammoth", "Utah Mammoth"],
    "NJD": ["Devils", "New Jersey"], "SJS": ["Sharks", "San Jose"], "LAK": ["Kings", "LA Kings", "Los Angeles"],
    "STL": ["Blues", "St. Louis", "St Louis"], "WPG": ["Jets", "Winnipeg"], "FLA": ["Panthers", "Florida"],
    "ANA": ["Ducks", "Anaheim"], "MIN": ["Wild", "Minnesota"], "SEA": ["Kraken", "Seattle"],
    "CGY": ["Flames", "Calgary"], "VAN": ["Canucks", "Nucks", "Vancouver"], "EDM": ["Oilers", "Edmonton"],
    "BUF": ["Sabres", "Buffalo"], "BOS": ["Bruins", "B's", "Boston"], "DAL": ["Stars", "Dallas"],
    "PHI": ["Flyers", "Philly", "Philadelphia"], "NYR": ["Rangers", "Blueshirts"],
}


def ascii_fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")


def get_json(path):
    # ~70 calls per refresh; the API starts returning 429 if they're back to
    # back, so pace them and back off harder when it does push back.
    time.sleep(0.25)
    for attempt in range(5):
        try:
            r = requests.get(f"{API}{path}", timeout=20)
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == 4:
                raise
            time.sleep(2)
    raise RuntimeError(f"NHL API kept rate-limiting {path}")


def load_teams(cur):
    standings = get_json("/standings/now")["standings"]
    abbrevs = []
    for t in standings:
        abbrev = t["teamAbbrev"]["default"]
        name = ascii_fold(t["teamName"]["default"])
        nick = ascii_fold(t["teamCommonName"]["default"])
        abbrevs.append(abbrev)
        cur.execute(
            """
            INSERT INTO teams (abbrev, name, nickname, division, conference) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (abbrev) DO UPDATE SET name = EXCLUDED.name, nickname = EXCLUDED.nickname,
                division = EXCLUDED.division, conference = EXCLUDED.conference
            """,
            (abbrev, name, nick, t.get("divisionName"), t.get("conferenceName")),
        )
        aliases = {abbrev, name, nick, ascii_fold(t["placeName"]["default"])}
        aliases.update(SLANG.get(abbrev, []))
        for a in aliases:
            cur.execute(
                "INSERT INTO team_aliases (team, alias) VALUES (%s, %s) ON CONFLICT DO NOTHING", (abbrev, a)
            )
    return abbrevs


def load_rosters(cur, abbrevs):
    seen = set()
    for abbrev in abbrevs:
        try:
            roster = get_json(f"/roster/{abbrev}/current")
        except Exception as e:
            print(f"  roster {abbrev} failed: {e}")
            continue
        for group in ("forwards", "defensemen", "goalies"):
            for p in roster.get(group, []):
                first = ascii_fold(p["firstName"]["default"])
                last = ascii_fold(p["lastName"]["default"])
                full = f"{first} {last}"
                cur.execute(
                    """
                    INSERT INTO players (nhl_id, full_name, team, position, active, updated_at)
                    VALUES (%s, %s, %s, %s, true, now())
                    ON CONFLICT (nhl_id) DO UPDATE SET full_name = EXCLUDED.full_name, team = EXCLUDED.team,
                        position = EXCLUDED.position, active = true, updated_at = now()
                    RETURNING id
                    """,
                    (p["id"], full, abbrev, p.get("positionCode")),
                )
                pid = cur.fetchone()[0]
                seen.add(pid)
                # Full name for exact hits; last name because that's how most
                # hockey talk refers to players ("McDavid over 3.5 shots").
                # Last names shared by several players are disambiguated at
                # match time using the team the analyst mentioned.
                for alias in {full, last}:
                    cur.execute(
                        "INSERT INTO player_aliases (player_id, alias) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (pid, alias),
                    )
    # Only retire players if we actually got (almost) every roster.
    if len(seen) > 600:
        cur.execute("UPDATE players SET active = false WHERE NOT (id = ANY(%s))", (list(seen),))
    return len(seen)


def load_schedule(cur, abbrevs):
    n = 0
    for abbrev in abbrevs:
        try:
            games = get_json(f"/club-schedule-season/{abbrev}/now")["games"]
        except Exception as e:
            print(f"  schedule {abbrev} failed: {e}")
            continue
        for g in games:
            if g["gameType"] not in (2, 3):
                continue
            cur.execute(
                """
                INSERT INTO games (nhl_game_id, season, game_type, game_date, start_time_utc, away, home,
                                   state, away_score, home_score)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (nhl_game_id) DO UPDATE SET game_date = EXCLUDED.game_date,
                    start_time_utc = EXCLUDED.start_time_utc, state = EXCLUDED.state,
                    away_score = EXCLUDED.away_score, home_score = EXCLUDED.home_score
                """,
                (
                    g["id"], g["season"], g["gameType"], g["gameDate"],
                    datetime.fromisoformat(g["startTimeUTC"].replace("Z", "+00:00")),
                    g["awayTeam"]["abbrev"], g["homeTeam"]["abbrev"], g.get("gameState"),
                    g["awayTeam"].get("score"), g["homeTeam"].get("score"),
                ),
            )
            n += 1
    return n


def main():
    conn = get_conn()
    cur = conn.cursor()
    abbrevs = load_teams(cur)
    conn.commit()
    print(f"Teams: {len(abbrevs)}")
    players = load_rosters(cur, abbrevs)
    conn.commit()
    print(f"Players on rosters: {players}")
    games = load_schedule(cur, abbrevs)
    conn.commit()
    print(f"Schedule rows processed: {games}")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
