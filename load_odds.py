"""
Pulls the current sportsbook lines for upcoming NHL games from NHL.com's public
odds feed (the same DraftKings numbers shown in its scoreboard widget - no key
needed) into game_odds, one row per game, overwritten each run.

These are context for the dashboard ("the market has MTL -225, O/U 6.5"), not
bets anyone made, so they never touch the analyst rankings or the track record.

Usage:
    python load_odds.py
"""
import requests
from db import get_conn

URL = "https://api-web.nhle.com/v1/partner-game/US/now"


def _side(team):
    out = {}
    for o in team.get("odds", []):
        d, q, v = o["description"], o.get("qualifier") or "", int(round(o["value"]))
        if d == "MONEY_LINE_2_WAY":
            out["ml"] = v
        elif d == "PUCK_LINE":
            out["puck"] = (float(q), v)
        elif d == "OVER_UNDER" and q[:1] in ("O", "U"):
            out["total"] = (float(q[1:]), v)
    return out


def main():
    resp = requests.get(URL, timeout=30)
    resp.raise_for_status()
    games = resp.json().get("games", [])
    conn = get_conn()
    cur = conn.cursor()
    saved = 0
    for g in games:
        cur.execute("SELECT id FROM games WHERE nhl_game_id = %s", (g["gameId"],))
        row = cur.fetchone()
        if not row:
            continue
        h, a = _side(g["homeTeam"]), _side(g["awayTeam"])
        total = h.get("total") or a.get("total")
        cur.execute(
            """
            INSERT INTO game_odds (game_id, ml_home, ml_away, puck_home_line, puck_home_odds,
                                   puck_away_line, puck_away_odds, total_line, over_odds, under_odds, fetched_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
            ON CONFLICT (game_id) DO UPDATE SET
                ml_home=EXCLUDED.ml_home, ml_away=EXCLUDED.ml_away,
                puck_home_line=EXCLUDED.puck_home_line, puck_home_odds=EXCLUDED.puck_home_odds,
                puck_away_line=EXCLUDED.puck_away_line, puck_away_odds=EXCLUDED.puck_away_odds,
                total_line=EXCLUDED.total_line, over_odds=EXCLUDED.over_odds, under_odds=EXCLUDED.under_odds,
                fetched_at=now()
            """,
            (row[0], h.get("ml"), a.get("ml"),
             h.get("puck", (None, None))[0], h.get("puck", (None, None))[1],
             a.get("puck", (None, None))[0], a.get("puck", (None, None))[1],
             total[0] if total else None,
             (h.get("total") or (None, None))[1], (a.get("total") or (None, None))[1]),
        )
        saved += 1
    conn.commit()
    print(f"Odds: {saved} of {len(games)} games with lines saved.")


if __name__ == "__main__":
    main()
