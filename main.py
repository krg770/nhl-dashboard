"""
FastAPI backend for the NHL betting-podcast dashboard.

Usage:
    uvicorn main:app --port 8002
Then open http://localhost:8002 in a browser.

Everything here is informational comparison of what podcast analysts are
saying - never a recommendation to bet.
"""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from db import get_conn
import run_history
from bet_logic import (
    analyst_key as _analyst_key,
    plausible_low_match as _plausible_low_match,
    stance as _stance,
    opposite as _opposite,
    find_game as _find_game,
)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BET_TYPE_NAMES = {
    "anytime_goal": "Anytime Goal", "first_goal": "First Goal", "goals": "Goals",
    "shots_on_goal": "Shots on Goal", "points": "Points", "assists": "Assists",
    "power_play_points": "PP Points", "blocked_shots": "Blocked Shots", "saves": "Saves",
    "player_other": "Other Prop", "moneyline": "Moneyline", "puck_line": "Puck Line",
    "regulation_win": "Regulation Win", "team_total": "Team Total", "team_other": "Team Bet",
    "game_total": "Game Total", "first_period": "1st Period", "both_teams_score": "Both Teams Score",
    "game_other": "Game Bet", "futures": "Futures",
}

def dict_rows(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ------------------------------------------------------------ pipeline / ops

@app.get("/api/pipeline_status")
def get_pipeline_status():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM pipeline_status WHERE id = 1")
    rows = dict_rows(cur)
    cur.execute("SELECT count(*) FROM episodes WHERE status IN ('new', 'transcribed')")
    queued = cur.fetchone()[0]
    cur.close()
    conn.close()

    status = rows[0] if rows else {}
    status["episodes_queued_total"] = queued
    total = status.get("episodes_total_this_run")
    done = status.get("episodes_done_this_run")
    status["episodes_remaining_this_run"] = (total - done) if (total is not None and done is not None) else None
    return status


@app.get("/api/pipeline_runs")
def get_pipeline_runs(limit: int = Query(10, ge=1, le=100)):
    """Dated history of scheduled runs, newest first (see run_history.py)."""
    conn = get_conn()
    cur = conn.cursor()
    run_history.ensure_schema(cur, conn)
    cur.execute(
        """
        SELECT id, started_at, finished_at, outcome, episodes_found, episodes_planned,
               episodes_processed, episodes_errored, items_added, note
        FROM pipeline_runs ORDER BY id DESC LIMIT %s
        """,
        (limit,),
    )
    runs = dict_rows(cur)
    cur.close()
    conn.close()
    return {"runs": runs}


@app.get("/api/error_episodes")
def get_error_episodes():
    """Episodes stuck in status=error, with why, for the retry panel."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT e.id, pod.name AS podcast, e.title, e.processed_at AS failed_at, e.error_message,
               (e.transcript IS NOT NULL AND e.transcript <> '') AS has_transcript
        FROM episodes e JOIN podcasts pod ON pod.id = e.podcast_id
        WHERE e.status = 'error'
        ORDER BY e.processed_at DESC NULLS LAST
        """
    )
    rows = dict_rows(cur)
    cur.close()
    conn.close()
    for r in rows:
        r["error_message"] = r["error_message"] or "processing failed (see pipeline.log)"
    return {"episodes": rows}


def _requeue_errors(episode_id=None):
    conn = get_conn()
    cur = conn.cursor()
    if episode_id is None:
        cur.execute("UPDATE episodes SET status = 'new', error_message = NULL WHERE status = 'error'")
    else:
        cur.execute(
            "UPDATE episodes SET status = 'new', error_message = NULL WHERE id = %s AND status = 'error'",
            (episode_id,),
        )
    n = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return n


@app.post("/api/episodes/{episode_id}/retry")
def retry_episode(episode_id: int):
    """Requeues one failed episode for the next scheduled run."""
    n = _requeue_errors(episode_id)
    return {"ok": n == 1, "requeued": n}


@app.post("/api/retry_errors")
def retry_all_errors():
    """Requeues every failed episode for the next scheduled run."""
    return {"ok": True, "requeued": _requeue_errors()}


# ------------------------------------------------------------------ ranking

def _consensus_score(analysts, shows, share_for, sharp, recent_hours, game_hours_away):
    """0-100 strength of analyst agreement - NOT a probability the bet hits.
    Every term is returned so the UI can show exactly why."""
    parts = [
        ("Analysts talking about it", round(min(analysts, 4) / 4 * 50), 50),
        ("Different shows", round(min(shows, 3) / 3 * 15), 15),
        ("Nobody on the other side", round(share_for * 20), 20),
        ("Called sharp money", 5 if sharp else 0, 5),
        ("Discussed in last 12h", 5 if recent_hours is not None and recent_hours <= 12 else 0, 5),
        ("Game is within 24h", 5 if game_hours_away is not None and game_hours_away <= 24 else 0, 5),
    ]
    total = max(0, min(100, sum(p[1] for p in parts)))
    return total, [{"label": l, "points": pts, "max": mx} for l, pts, mx in parts]


@app.get("/api/best_bets")
def get_best_bets(hours: int = Query(48, ge=6, le=120)):
    """
    A ranked focus list: which NHL bets are the most analysts talking about
    for a game that hasn't started yet, most-discussed first. A "pick" is one
    (subject, bet type, game, side). Informational comparison of what the
    podcasts are saying - never a recommendation.
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT bq.id, bq.subject_type, bq.player_id, p.full_name, p.team AS player_team, p.position,
               bq.team, bq.opponent, bq.raw_mention, bq.match_confidence, bq.bet_type, bq.lean,
               bq.sharp_or_public, bq.timeframe, bq.line_context, bq.quote_text, bq.speaker,
               bq.betting_relevance, bq.tags, e.published_at, pod.id AS pod_id, pod.name AS show_name,
               t.name AS team_name
        FROM bet_quotes bq
        JOIN episodes e ON e.id = bq.episode_id
        JOIN podcasts pod ON pod.id = e.podcast_id
        LEFT JOIN players p ON p.id = bq.player_id
        LEFT JOIN teams t ON t.abbrev = bq.team
        WHERE e.published_at >= now() - make_interval(hours => %s)
        ORDER BY e.published_at DESC
        """,
        (hours,),
    )
    rows = dict_rows(cur)

    cur.execute(
        """
        SELECT id, nhl_game_id, game_date, start_time_utc, away, home
        FROM games
        WHERE start_time_utc >= now() - interval '3 days' AND start_time_utc <= now() + interval '14 days'
        ORDER BY start_time_utc
        """
    )
    games = dict_rows(cur)
    cur.execute("SELECT abbrev, name FROM teams")
    team_names = dict(cur.fetchall())
    cur.close()
    conn.close()

    games_by_team = {}
    for g in games:
        games_by_team.setdefault(g["away"], []).append(g)
        games_by_team.setdefault(g["home"], []).append(g)

    now = datetime.now(timezone.utc)
    excluded = {"recap_of_past_game": 0, "unmatched_subject": 0, "game_already_started": 0, "no_scheduled_game": 0}
    groups = {}
    futures = {}

    for r in rows:
        if r["timeframe"] == "past":
            excluded["recap_of_past_game"] += 1
            continue

        stype = r["subject_type"]
        if stype == "player":
            if not r["player_id"] or (
                r["match_confidence"] == "low" and not _plausible_low_match(r["raw_mention"], r["full_name"])
            ):
                excluded["unmatched_subject"] += 1
                continue
            team = r["player_team"] or r["team"]
            subject_key = ("p", r["player_id"])
            name = r["full_name"]
        elif stype == "team":
            if not r["team"]:
                excluded["unmatched_subject"] += 1
                continue
            team = r["team"]
            subject_key = ("t", team)
            name = team_names.get(team, r["raw_mention"])
        else:
            if not (r["team"] and r["opponent"]):
                excluded["unmatched_subject"] += 1
                continue
            team = r["team"]
            subject_key = ("g", frozenset((r["team"], r["opponent"])))
            name = f'{team_names.get(r["team"], r["team"])} vs {team_names.get(r["opponent"], r["opponent"])}'

        # side is folded into line_context by the worker ("over 3.5"); recover it.
        lc = (r["line_context"] or "").lower()
        side = "over" if lc.startswith("over") else "under" if lc.startswith("under") else None
        stance = _stance(side, r["lean"])

        if r["timeframe"] == "futures" or r["bet_type"] == "futures":
            futures.setdefault((subject_key, name, r["bet_type"], stance), []).append((r, name, team))
            continue

        if not team:
            excluded["unmatched_subject"] += 1
            continue
        game = _find_game(games_by_team, team, r["opponent"], r["published_at"])
        if not game:
            excluded["no_scheduled_game"] += 1
            continue
        if game["start_time_utc"] <= now:
            excluded["game_already_started"] += 1
            continue
        groups.setdefault((subject_key, r["bet_type"], game["id"], stance), []).append((r, name, team, game))

    def build(items, stance, subject_key, bet_type, game):
        by_analyst = {}
        for it in items:
            by_analyst.setdefault(_analyst_key(it[0]["speaker"], it[0]["show_name"]), it)
        takes = list(by_analyst.values())
        analysts = len(takes)
        shows = len({t[0]["pod_id"] for t in takes})
        sharp = sum(1 for t in takes if t[0]["sharp_or_public"] == "sharp")
        newest = max(t[0]["published_at"] for t in takes)
        # Just the number: the over/under is already the pick's stance, and
        # "over 1.5" next to an UNDER badge reads as a contradiction.
        lines = []
        for it in items:
            lc = re.sub(r"^(over|under)\s+", "", (it[0]["line_context"] or "").strip(), flags=re.I)
            if lc and lc not in lines:
                lines.append(lc)
        first = items[0]
        return {
            "analysts": analysts, "shows": shows, "sharp_count": sharp, "newest": newest,
            "lines": lines[:4], "first": first, "game": game, "stance": stance,
            "takes": [
                {
                    "analyst": t[0]["speaker"] or t[0]["show_name"], "show": t[0]["show_name"],
                    "lean": t[0]["lean"], "line": t[0]["line_context"], "quote": t[0]["quote_text"],
                    "why": t[0]["betting_relevance"], "at": t[0]["published_at"],
                }
                for t in takes
            ],
        }

    built = {}
    for (subject_key, bet_type, game_id, stance), items in groups.items():
        built[(subject_key, bet_type, game_id, stance)] = build(items, stance, subject_key, bet_type, items[0][3])

    picks = []
    for (subject_key, bet_type, game_id, stance), b in built.items():
        r, name, team, game = b["first"]
        opposing = built.get((subject_key, bet_type, game_id, _opposite(stance)))
        opp_analysts = opposing["analysts"] if opposing else 0
        share_for = b["analysts"] / (b["analysts"] + opp_analysts)
        recent_hours = (now - b["newest"]).total_seconds() / 3600
        hours_away = (game["start_time_utc"] - now).total_seconds() / 3600
        score, breakdown = _consensus_score(b["analysts"], b["shows"], share_for, b["sharp_count"], recent_hours, hours_away)
        opponent = game["home"] if team == game["away"] else game["away"]
        picks.append({
            "subject_type": r["subject_type"],
            "name": name,
            "team": team,
            "position": r["position"],
            "bet_type": bet_type,
            "bet_label": BET_TYPE_NAMES.get(bet_type, bet_type),
            "stance": stance,
            "analysts": b["analysts"],
            "shows": b["shows"],
            "opposing_analysts": opp_analysts,
            "sharp_count": b["sharp_count"],
            "lines": b["lines"],
            "score": score,
            "score_breakdown": breakdown,
            "game_id": game["id"],
            "game_start": game["start_time_utc"],
            "away": game["away"],
            "home": game["home"],
            "opponent": opponent,
            "is_home": team == game["home"],
            "latest_at": b["newest"],
            "takes": b["takes"],
        })

    picks.sort(key=lambda p: (-p["analysts"], -p["shows"], -p["score"], -p["sharp_count"], -p["latest_at"].timestamp()))
    for i, p in enumerate(picks, 1):
        p["rank"] = i

    slate = {}
    for p in picks:
        g = slate.setdefault(p["game_id"], {
            "game_id": p["game_id"], "away": p["away"], "home": p["home"], "game_start": p["game_start"],
            "analyst_mentions": 0, "bets": 0, "top": [],
        })
        g["analyst_mentions"] += p["analysts"]
        g["bets"] += 1
        if len(g["top"]) < 3:
            g["top"].append(f'{p["name"]} {p["bet_label"]}')
    slate_list = sorted(slate.values(), key=lambda g: g["game_start"])

    future_list = []
    for (subject_key, name, bet_type, stance), items in futures.items():
        analysts = len({_analyst_key(i[0]["speaker"], i[0]["show_name"]) for i in items})
        future_list.append({
            "name": name, "stance": stance, "analysts": analysts,
            "lines": list(dict.fromkeys(i[0]["line_context"] for i in items if i[0]["line_context"]))[:3],
            "quote": items[0][0]["quote_text"], "show": items[0][0]["show_name"],
        })
    future_list.sort(key=lambda f: -f["analysts"])

    return {
        "window_hours": hours,
        "picks": picks,
        "slate": slate_list,
        "futures": future_list[:15],
        "excluded": excluded,
    }


@app.get("/api/recent_takes")
def get_recent_takes(hours: int = Query(48, ge=6, le=120)):
    """Every extracted take in the window, newest first (the raw detail
    behind the Focus List)."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COALESCE(p.full_name, t.name, bq.raw_mention) AS subject, bq.subject_type, bq.bet_type,
               bq.line_context, bq.lean, bq.timeframe, bq.speaker, pod.name AS show_name,
               bq.quote_text, bq.betting_relevance, e.published_at
        FROM bet_quotes bq
        JOIN episodes e ON e.id = bq.episode_id
        JOIN podcasts pod ON pod.id = e.podcast_id
        LEFT JOIN players p ON p.id = bq.player_id
        LEFT JOIN teams t ON t.abbrev = bq.team AND bq.subject_type = 'team'
        WHERE e.published_at >= now() - make_interval(hours => %s)
        ORDER BY e.published_at DESC, bq.id
        LIMIT 300
        """,
        (hours,),
    )
    rows = dict_rows(cur)
    cur.close()
    conn.close()
    for r in rows:
        r["bet_label"] = BET_TYPE_NAMES.get(r["bet_type"], r["bet_type"])
    return rows


# ------------------------------------------------------------------ results

_RESULT_SELECT = """
    SELECT br.bet_quote_id, br.game_id, br.stance, br.line, br.actual, br.outcome, br.detail,
           bq.subject_type, bq.bet_type, bq.line_context, bq.lean, bq.speaker, bq.player_id,
           bq.team, bq.opponent, bq.raw_mention, p.full_name, t.name AS team_name, ot.name AS opp_name,
           pod.id AS pod_id, pod.name AS show_name, e.published_at,
           g.game_date, g.start_time_utc, g.away, g.home, g.away_score, g.home_score, g.last_period_type
    FROM bet_results br
    JOIN bet_quotes bq ON bq.id = br.bet_quote_id
    JOIN episodes e ON e.id = bq.episode_id
    JOIN podcasts pod ON pod.id = e.podcast_id
    JOIN games g ON g.id = br.game_id
    LEFT JOIN players p ON p.id = bq.player_id
    LEFT JOIN teams t ON t.abbrev = bq.team
    LEFT JOIN teams ot ON ot.abbrev = bq.opponent
"""


def _subject(r):
    """Display name + a stable key for what the bet is on."""
    if r["subject_type"] == "player":
        return r["full_name"] or r["raw_mention"], ("p", r["player_id"])
    if r["subject_type"] == "team":
        return r["team_name"] or r["raw_mention"], ("t", r["team"])
    name = f'{r["team_name"] or r["team"]} vs {r["opp_name"] or r["opponent"]}'
    return name, ("g", frozenset((r["team"], r["opponent"])))


@app.get("/api/results")
def get_results(date: str = Query(None, description="game date YYYY-MM-DD; default = latest with results")):
    """
    How the analysts' bets actually turned out, for one night of games.
    Bets are grouped per game, then per distinct bet (subject + bet type +
    side + line) with every analyst who made it. Only bets that could be
    settled unambiguously get a hit/miss; the rest are counted as ungraded.
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT g.game_date FROM bet_results br JOIN games g ON g.id = br.game_id
        WHERE g.game_date <= CURRENT_DATE ORDER BY g.game_date DESC LIMIT 14
        """
    )
    dates = [d[0] for d in cur.fetchall()]
    if not dates:
        cur.close()
        conn.close()
        return {"date": None, "available_dates": [], "games": [], "summary": {}, "ungraded": []}
    target = datetime.strptime(date, "%Y-%m-%d").date() if date else dates[0]

    cur.execute(_RESULT_SELECT + " WHERE g.game_date = %s ORDER BY g.start_time_utc, br.bet_quote_id", (target,))
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    cur.close()
    conn.close()

    games = {}
    ungraded = {}
    for r in rows:
        g = games.setdefault(r["game_id"], {
            "game_id": r["game_id"], "away": r["away"], "home": r["home"], "away_score": r["away_score"],
            "home_score": r["home_score"], "ended_in": r["last_period_type"], "start": r["start_time_utc"],
            "bets": {},
        })
        if r["outcome"] == "ungraded":
            ungraded[r["detail"]] = ungraded.get(r["detail"], 0) + 1
            continue
        name, skey = _subject(r)
        line = float(r["line"]) if r["line"] is not None else None
        key = (skey, r["bet_type"], r["stance"], line)
        b = g["bets"].setdefault(key, {
            "name": name, "bet_type": r["bet_type"], "bet_label": BET_TYPE_NAMES.get(r["bet_type"], r["bet_type"]),
            "stance": r["stance"], "line": line, "odds": None, "outcome": r["outcome"], "detail": r["detail"],
            "analysts": {},
        })
        raw_line = (r["line_context"] or "").strip()
        if raw_line and re.fullmatch(r"[+-]\d{3,}", raw_line):
            b["odds"] = raw_line
        who = _analyst_key(r["speaker"], r["show_name"])
        b["analysts"].setdefault(who, {"name": r["speaker"] or r["show_name"], "show": r["show_name"]})

    summary = {"hit": 0, "miss": 0, "push": 0, "void": 0}
    out_games = []
    for g in games.values():
        bets = []
        for b in g["bets"].values():
            summary[b["outcome"]] = summary.get(b["outcome"], 0) + 1
            b["analysts"] = list(b["analysts"].values())
            bets.append(b)
        order = {"hit": 0, "miss": 1, "push": 2, "void": 3}
        bets.sort(key=lambda b: (order.get(b["outcome"], 9), -len(b["analysts"]), b["name"]))
        g["bets"] = bets
        out_games.append(g)
    out_games.sort(key=lambda g: g["start"])
    return {
        "date": target.isoformat(),
        "available_dates": [d.isoformat() for d in dates],
        "games": out_games,
        "summary": summary,
        "ungraded": [{"reason": k, "count": v} for k, v in sorted(ungraded.items(), key=lambda kv: -kv[1])],
    }


@app.get("/api/accuracy")
def get_accuracy(days: int = Query(30, ge=1, le=180)):
    """
    Track record per show and per analyst: of the bets they backed (or faded)
    that could be settled, how many hit. Each analyst counts once per distinct
    bet (same person, subject, bet type, game, side and line) so repeating a
    pick doesn't pad the record; pushes are shown but excluded from the rate.
    Speaker names come from transcripts without voice labelling, so a show's
    record is the dependable number and an individual's is best-effort.
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        _RESULT_SELECT + """
        WHERE br.outcome IN ('hit', 'miss', 'push') AND g.game_date >= CURRENT_DATE - %s
        ORDER BY e.published_at
        """,
        (days,),
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    cur.execute(
        "SELECT count(*) FROM bet_results br JOIN games g ON g.id = br.game_id "
        "WHERE br.outcome = 'ungraded' AND g.game_date >= CURRENT_DATE - %s",
        (days,),
    )
    ungraded = cur.fetchone()[0]
    cur.close()
    conn.close()

    seen = set()
    shows, analysts, types = {}, {}, {}
    overall = {"hit": 0, "miss": 0, "push": 0}
    names = {}
    for r in rows:
        who = _analyst_key(r["speaker"], r["show_name"])
        _, skey = _subject(r)
        dedupe = (who, r["show_name"], skey, r["bet_type"], r["game_id"], r["stance"], float(r["line"]) if r["line"] is not None else None)
        if dedupe in seen:
            continue
        seen.add(dedupe)
        o = r["outcome"]
        overall[o] += 1
        shows.setdefault(r["show_name"], {"hit": 0, "miss": 0, "push": 0})[o] += 1
        a = analysts.setdefault((who, r["show_name"]), {"hit": 0, "miss": 0, "push": 0})
        a[o] += 1
        names.setdefault((who, r["show_name"]), {}).setdefault(r["speaker"] or r["show_name"], 0)
        names[(who, r["show_name"])][r["speaker"] or r["show_name"]] += 1
        types.setdefault(r["bet_type"], {"hit": 0, "miss": 0, "push": 0})[o] += 1

    def finish(name, rec, **extra):
        decided = rec["hit"] + rec["miss"]
        return {"name": name, **extra, **rec, "graded": decided + rec["push"],
                "pct": round(rec["hit"] / decided * 100) if decided else None}

    analyst_list = []
    for (who, show), rec in analysts.items():
        spelled = max(names[(who, show)].items(), key=lambda kv: kv[1])[0]
        label = f"{show} host" if who.endswith("(host)") else spelled
        analyst_list.append(finish(label, rec, show=show))
    analyst_list.sort(key=lambda a: (-a["graded"], a["name"]))
    show_list = sorted((finish(n, rec) for n, rec in shows.items()), key=lambda s: -s["graded"])
    type_list = sorted(
        (finish(BET_TYPE_NAMES.get(t, t), rec) for t, rec in types.items()), key=lambda s: -s["graded"]
    )
    return {
        "window_days": days,
        "overall": finish("All shows", overall),
        "shows": show_list,
        "analysts": analyst_list,
        "by_bet_type": type_list,
        "ungraded": ungraded,
    }


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def serve_index():
    return FileResponse("static/index.html")
