"""
FastAPI backend for the NHL betting-podcast dashboard.

Usage:
    uvicorn main:app --port 8002
Then open http://localhost:8002 in a browser.

Everything here is informational comparison of what podcast analysts are
saying - never a recommendation to bet.
"""
import difflib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from db import get_conn
import run_history

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

_GENERIC_SPEAKERS = {"", "host", "hosts", "co-host", "cohost", "co host", "house", "guest", "unknown", "panelist"}
_NAME_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v"}


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

def _analyst_key(speaker, show_name):
    """One identity per analyst, so the same person isn't counted twice for
    the same bet. Generic labels ("co-host") fall back to the show."""
    s = re.sub(r"\(.*?\)", "", speaker or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    if s in _GENERIC_SPEAKERS or "panelist" in s:
        return f"{show_name} (host)"
    return s


def _plausible_low_match(raw, full_name):
    """A low-confidence fuzzy player match is either a transcription
    misspelling of the right player or a different player. Requires first
    AND last name to each be individually close. Last-name-only mentions
    are common in hockey talk, so a single-token mention is accepted only
    when it closely matches the last name."""
    def parts(name):
        return [t for t in re.sub(r"[^a-z' .-]", "", (name or "").lower()).split() if t not in _NAME_SUFFIXES]
    r, f = parts(raw), parts(full_name)
    if not r or len(f) < 2:
        return False
    last = difflib.SequenceMatcher(None, r[-1], f[-1]).ratio()
    if len(r) == 1:
        return last >= 0.85
    first = difflib.SequenceMatcher(None, r[0], f[0]).ratio()
    return first >= 0.6 and last >= 0.8


def _stance(side, lean):
    """Which side of the bet this analyst is on. 'over'/'under' when the bet
    has a line, otherwise 'for'/'against'. A neutral mention is counted as
    talk about the default side but never as a lean."""
    if side in ("over", "under"):
        if lean == "unfavorable":
            return "under" if side == "over" else "over"
        return side
    return "against" if lean == "unfavorable" else "for"


def _opposite(stance):
    return {"over": "under", "under": "over", "for": "against", "against": "for"}[stance]


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


def _find_game(games_by_team, team, opponent, published_at):
    """The player's/team's next game that hasn't started when the take was
    recorded (an hour of grace, since shows are sometimes posted just after
    puck drop). If the analyst named the opponent, require that matchup."""
    cutoff = published_at - timedelta(hours=1)
    for g in games_by_team.get(team, []):
        if g["start_time_utc"] < cutoff:
            continue
        if opponent and opponent not in (g["away"], g["home"]):
            continue
        return g
    return None


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


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def serve_index():
    return FileResponse("static/index.html")
