"""
Grades what the analysts said against what actually happened.

Two steps, run after load_nhl.py in every pipeline run:
  1. load_game_stats: for each finished game, pull the box score and
     play-by-play from the NHL API and store per-player stats (goals,
     assists, shots, blocked shots, power-play points, saves) plus the game's
     first goal and how it ended (regulation / overtime / shootout).
  2. grade_bets: settle every ungraded bet quote whose game is over.

Grading is deliberately conservative. A bet is only graded when it can be
settled unambiguously from what was said:
  - lined props need an explicit over/under AND a numeric line;
  - odds ("+205", "-160") are not lines, so "McDavid points +100" is NOT graded;
  - a puck line needs a signed line ("-1.5");
  - anything else is stored as 'ungraded' with the reason, so the dashboard can
    say how many bets it could not settle instead of guessing.
Only quotes where the analyst took a side (favorable / unfavorable) are graded;
neutral mentions are not picks. A 'void' means the player did not play.

Usage:
    python grade_results.py
"""
import time
import requests
from collections import defaultdict
from db import get_conn
from bet_logic import (
    plausible_low_match, side_from_line, stance as make_stance, find_game, parse_line, usable_subject,
)

API = "https://api-web.nhle.com/v1"
FINAL_STATES = ("FINAL", "OFF")
# Quotes older than this can't be matched to a game we still track reliably.
LOOKBACK_DAYS = 14
# Bet types that are settled against an explicit numeric line.
LINED_BET_TYPES = {
    "goals", "points", "assists", "shots_on_goal", "blocked_shots", "power_play_points", "saves",
    "team_total", "game_total",
}


def get_json(path):
    time.sleep(0.3)
    for attempt in range(5):
        r = requests.get(f"{API}{path}", timeout=25)
        if r.status_code == 429:
            time.sleep(5 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"NHL API kept rate-limiting {path}")


# --------------------------------------------------------------- game stats

def _situation_power_play(situation_code, scoring_is_home):
    """situationCode is four digits: away goalie, away skaters, home skaters,
    home goalie. A power-play goal is one scored by the side with more skaters."""
    if not situation_code or len(str(situation_code)) != 4:
        return False
    away_sk, home_sk = int(str(situation_code)[1]), int(str(situation_code)[2])
    return (home_sk > away_sk) if scoring_is_home else (away_sk > home_sk)


def load_game_stats(cur, conn):
    cur.execute(
        """
        SELECT id, nhl_game_id FROM games
        WHERE state = ANY(%s) AND NOT stats_loaded AND game_type IN (2, 3)
          AND start_time_utc > now() - make_interval(days => %s)
        ORDER BY start_time_utc
        """,
        (list(FINAL_STATES), LOOKBACK_DAYS),
    )
    todo = cur.fetchall()
    loaded = 0
    for game_id, nhl_game_id in todo:
        try:
            box = get_json(f"/gamecenter/{nhl_game_id}/boxscore")
            pbp = get_json(f"/gamecenter/{nhl_game_id}/play-by-play")
        except Exception as e:
            print(f"  game {nhl_game_id}: stats fetch failed: {e}")
            continue

        home_id, away_id = box["homeTeam"]["id"], box["awayTeam"]["id"]
        goals = [p for p in pbp.get("plays", []) if p.get("typeDescKey") == "goal"
                 and p.get("periodDescriptor", {}).get("periodType") != "SO"]
        goals.sort(key=lambda p: p.get("sortOrder", 0))

        pp_goals, pp_points = defaultdict(int), defaultdict(int)
        for g in goals:
            d = g.get("details", {})
            scoring_home = d.get("eventOwnerTeamId") == home_id
            if _situation_power_play(g.get("situationCode"), scoring_home):
                for key in ("scoringPlayerId", "assist1PlayerId", "assist2PlayerId"):
                    if d.get(key):
                        pp_points[d[key]] += 1
                if d.get("scoringPlayerId"):
                    pp_goals[d["scoringPlayerId"]] += 1
        first_goal = goals[0]["details"].get("scoringPlayerId") if goals else None

        cur.execute("SELECT nhl_id, id FROM players")
        player_ids = dict(cur.fetchall())

        for side in ("awayTeam", "homeTeam"):
            team = box[side]["abbrev"]
            groups = box["playerByGameStats"][side]
            for group in ("forwards", "defense", "goalies"):
                for p in groups.get(group, []):
                    nid = p["playerId"]
                    toi = p.get("toi") or "00:00"
                    is_goalie = group == "goalies"
                    cur.execute(
                        """
                        INSERT INTO player_game_stats (game_id, nhl_id, player_id, team, is_goalie, played,
                            goals, assists, points, shots, blocked_shots, pp_goals, pp_points, saves, shots_against)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (game_id, nhl_id) DO NOTHING
                        """,
                        (
                            game_id, nid, player_ids.get(nid), team, is_goalie, toi != "00:00",
                            p.get("goals", 0), p.get("assists", 0), p.get("points", 0), p.get("sog", 0),
                            p.get("blockedShots", 0), pp_goals.get(nid, 0), pp_points.get(nid, 0),
                            p.get("saves", 0), p.get("shotsAgainst", 0),
                        ),
                    )
        cur.execute(
            "UPDATE games SET last_period_type = %s, first_goal_nhl_id = %s, stats_loaded = true, "
            "away_score = %s, home_score = %s WHERE id = %s",
            (box.get("gameOutcome", {}).get("lastPeriodType"), first_goal,
             box["awayTeam"].get("score"), box["homeTeam"].get("score"), game_id),
        )
        conn.commit()
        loaded += 1
    return loaded


# ----------------------------------------------------------------- grading

def _goals_excluding_shootout(game, team):
    """Team goals as bets count them: a shootout 'goal' only decides the
    winner, it is not added to totals / puck lines."""
    score = game["away_score"] if team == game["away"] else game["home_score"]
    if game["last_period_type"] == "SO":
        other = game["home_score"] if team == game["away"] else game["away_score"]
        if score > other:  # this team won the shootout; strip the deciding goal
            score -= 1
    return score


def _compare(value, line, st):
    """('hit'|'miss'|'push') for an over/under stance."""
    if value == line:
        return "push"
    over = value > line
    return "hit" if (over and st == "over") or (not over and st == "under") else "miss"


def _flip(outcome):
    return {"hit": "miss", "miss": "hit"}.get(outcome, outcome)


def grade_one(r, st, game, stats):
    """(outcome, line, actual, detail) for one quote against a finished game.
    `stats` maps nhl_id -> player_game_stats row for this game."""
    bt = r["bet_type"]
    line = parse_line(r["line_context"])
    # Bets without a line can't be "over"/"under": read those as backing / fading.
    if bt not in LINED_BET_TYPES and st in ("over", "under"):
        st = "for" if st == "over" else "against"
    side = st if st in ("over", "under") else None

    def ungraded(why):
        return "ungraded", line, None, why

    # ---------------------------------------------------------- player props
    if r["subject_type"] == "player":
        nid = r["nhl_id"]
        s = stats.get(nid)
        if bt == "first_goal":
            if not nid:
                return ungraded("player not identified")
            hit = game["first_goal_nhl_id"] == nid
            if st == "against":
                hit = not hit
            return ("hit" if hit else "miss"), None, None, "scored first goal" if game["first_goal_nhl_id"] == nid else "did not score first"
        if bt == "anytime_goal":
            if not s or not s["played"]:
                return "void", None, None, "did not play"
            scored = s["goals"] >= 1
            hit = scored if st != "against" else not scored
            return ("hit" if hit else "miss"), None, s["goals"], f'{s["goals"]} goal(s)'
        stat_field = {
            "goals": "goals", "points": "points", "assists": "assists", "shots_on_goal": "shots",
            "blocked_shots": "blocked_shots", "power_play_points": "pp_points", "saves": "saves",
        }.get(bt)
        if not stat_field:
            return ungraded("bet type can't be settled from the box score")
        if not side or line is None or line < 0:
            return ungraded("no over/under line stated" if line is None else "over/under or line unclear")
        if not s or not s["played"]:
            return "void", line, None, "did not play"
        if bt == "saves" and not s["is_goalie"]:
            return ungraded("not a goalie")
        value = s[stat_field]
        out = _compare(value, line, side)
        return out, line, value, f"{value} {stat_field.replace('_', ' ')} vs line {line:g}"

    # ----------------------------------------------------------- team & game
    home, away = game["home"], game["away"]
    team = r["team"]
    hs, as_ = game["home_score"], game["away_score"]
    if hs is None or as_ is None:
        return ungraded("final score missing")

    if r["subject_type"] == "team":
        if team not in (home, away):
            return ungraded("team not in game")
        my, opp = (hs, as_) if team == home else (as_, hs)
        if bt == "moneyline":
            out = "hit" if my > opp else "miss"
            out = out if st != "against" else _flip(out)
            return out, None, my - opp, f"final {my}-{opp}"
        if bt == "regulation_win":
            won_reg = my > opp and game["last_period_type"] == "REG"
            out = "hit" if won_reg else "miss"
            out = out if st != "against" else _flip(out)
            return out, None, None, f"final {my}-{opp} ({game['last_period_type']})"
        if bt == "puck_line":
            if line is None or abs(line) % 1 != 0.5 or not r["line_context"] or not r["line_context"].strip().lstrip("(").startswith(("+", "-")):
                return ungraded("puck line sign/number unclear")
            margin = _goals_excluding_shootout(game, team) - _goals_excluding_shootout(game, away if team == home else home)
            covered = margin + line > 0
            out = "hit" if covered else "miss"
            out = out if st != "against" else _flip(out)
            return out, line, margin, f"won by {margin}" if margin > 0 else f"lost by {-margin}" if margin < 0 else "tied after OT"
        if bt == "team_total":
            if not side or line is None:
                return ungraded("no over/under line stated")
            v = _goals_excluding_shootout(game, team)
            return _compare(v, line, side), line, v, f"{v} goals vs line {line:g}"
        return ungraded("bet type can't be settled from the score")

    # game bets
    if bt == "game_total":
        if not side or line is None:
            return ungraded("no over/under line stated")
        v = _goals_excluding_shootout(game, home) + _goals_excluding_shootout(game, away)
        return _compare(v, line, side), line, v, f"{v} total goals vs line {line:g}"
    if bt == "both_teams_score":
        both = _goals_excluding_shootout(game, home) >= 1 and _goals_excluding_shootout(game, away) >= 1
        out = "hit" if both else "miss"
        out = out if st != "against" else _flip(out)
        return out, None, None, "both scored" if both else "shutout"
    return ungraded("bet type can't be settled from the score")


def grade_bets(cur, conn):
    cur.execute(
        """
        SELECT id, nhl_game_id, game_date, start_time_utc, away, home, away_score, home_score, last_period_type,
               first_goal_nhl_id, state
        FROM games
        WHERE start_time_utc > now() - make_interval(days => %s) AND game_type IN (2, 3)
        ORDER BY start_time_utc
        """,
        (LOOKBACK_DAYS + 3,),
    )
    cols = [d[0] for d in cur.description]
    games = [dict(zip(cols, row)) for row in cur.fetchall()]
    games_by_id = {g["id"]: g for g in games}
    games_by_team = defaultdict(list)
    for g in games:
        games_by_team[g["away"]].append(g)
        games_by_team[g["home"]].append(g)

    cur.execute(
        """
        SELECT bq.id, bq.subject_type, bq.player_id, p.full_name, p.nhl_id, p.team AS player_team,
               bq.team, bq.opponent, bq.raw_mention, bq.match_confidence, bq.bet_type, bq.lean,
               bq.timeframe, bq.line_context, e.published_at, t.name AS team_name
        FROM bet_quotes bq
        JOIN episodes e ON e.id = bq.episode_id
        LEFT JOIN players p ON p.id = bq.player_id
        LEFT JOIN teams t ON t.abbrev = bq.team
        LEFT JOIN bet_results br ON br.bet_quote_id = bq.id
        WHERE br.bet_quote_id IS NULL
          AND bq.lean IN ('favorable', 'unfavorable')
          AND COALESCE(bq.timeframe, '') NOT IN ('past', 'futures')
          AND bq.bet_type <> 'futures'
          AND e.published_at > now() - make_interval(days => %s)
        ORDER BY e.published_at
        """,
        (LOOKBACK_DAYS,),
    )
    qcols = [d[0] for d in cur.description]
    quotes = [dict(zip(qcols, row)) for row in cur.fetchall()]

    stats_cache = {}

    def stats_for(game_id):
        if game_id not in stats_cache:
            cur.execute("SELECT * FROM player_game_stats WHERE game_id = %s", (game_id,))
            c = [d[0] for d in cur.description]
            stats_cache[game_id] = {row[c.index("nhl_id")]: dict(zip(c, row)) for row in cur.fetchall()}
        return stats_cache[game_id]

    graded = 0
    for r in quotes:
        sub = usable_subject(r, plausible_low_match)
        if not sub:
            continue
        _, _, team = sub
        if not team:
            continue
        game = find_game(games_by_team, team, r["opponent"], r["published_at"])
        if not game or game["state"] not in FINAL_STATES or not game["last_period_type"]:
            continue  # not over yet (or stats not loaded): try again next run
        st = make_stance(side_from_line(r["line_context"]), r["lean"])
        outcome, line, actual, detail = grade_one(r, st, game, stats_for(game["id"]))
        cur.execute(
            """
            INSERT INTO bet_results (bet_quote_id, game_id, stance, line, actual, outcome, detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (bet_quote_id) DO NOTHING
            """,
            (r["id"], game["id"], st, line, actual, outcome, detail),
        )
        graded += 1
    conn.commit()
    return graded


def main():
    conn = get_conn()
    cur = conn.cursor()
    n_games = load_game_stats(cur, conn)
    print(f"Game stats loaded for {n_games} finished game(s).")
    n_bets = grade_bets(cur, conn)
    print(f"Graded {n_bets} bet quote(s).")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
