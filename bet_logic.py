"""
Rules shared by the Focus List (main.py) and the result grader
(grade_results.py). Kept in one place so a bet is always assigned to the same
game, attributed to the same analyst and read with the same side whether it is
being ranked beforehand or graded afterwards.
"""
import difflib
import re
from datetime import timedelta

GENERIC_SPEAKERS = {"", "host", "hosts", "co-host", "cohost", "co host", "house", "guest", "unknown", "panelist"}
NAME_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v"}


def analyst_key(speaker, show_name):
    """One identity per analyst, so the same person isn't counted twice for
    the same bet. Generic labels ("co-host") fall back to the show."""
    s = re.sub(r"\(.*?\)", "", speaker or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    if s in GENERIC_SPEAKERS or "panelist" in s:
        return f"{show_name} (host)"
    return s


def plausible_low_match(raw, full_name):
    """A low-confidence fuzzy player match is either a transcription
    misspelling of the right player or a different player. Requires first
    AND last name to each be individually close. Last-name-only mentions
    are common in hockey talk, so a single-token mention is accepted only
    when it closely matches the last name."""
    def parts(name):
        return [t for t in re.sub(r"[^a-z' .-]", "", (name or "").lower()).split() if t not in NAME_SUFFIXES]
    r, f = parts(raw), parts(full_name)
    if not r or len(f) < 2:
        return False
    last = difflib.SequenceMatcher(None, r[-1], f[-1]).ratio()
    if len(r) == 1:
        return last >= 0.85
    first = difflib.SequenceMatcher(None, r[0], f[0]).ratio()
    return first >= 0.6 and last >= 0.8


def side_from_line(line_context):
    """The worker folds an over/under side into line_context ("over 3.5")."""
    lc = (line_context or "").strip().lower()
    return "over" if lc.startswith("over") else "under" if lc.startswith("under") else None


def stance(side, lean):
    """Which side of the bet this analyst is on. 'over'/'under' when the bet
    has a line, otherwise 'for'/'against'. A neutral mention is counted as
    talk about the default side but never as a lean."""
    if side in ("over", "under"):
        if lean == "unfavorable":
            return "under" if side == "over" else "over"
        return side
    return "against" if lean == "unfavorable" else "for"


def opposite(st):
    return {"over": "under", "under": "over", "for": "against", "against": "for"}[st]


def find_game(games_by_team, team, opponent, published_at):
    """The team's next game that hasn't started when the take was recorded
    (an hour of grace, since shows are sometimes posted just after puck
    drop). If the analyst named the opponent, require that matchup."""
    cutoff = published_at - timedelta(hours=1)
    for g in games_by_team.get(team, []):
        if g["start_time_utc"] < cutoff:
            continue
        if opponent and opponent not in (g["away"], g["home"]):
            continue
        return g
    return None


def parse_line(line_context):
    """The numeric line in a line_context string, or None. Odds ("+205",
    "-160", "-105 to -120") are NOT lines and are ignored: anything with a
    sign and a magnitude of 100+, or any bare number of 100+, is odds.
    A signed small number ("-1.5") is kept signed - it is a puck line."""
    for tok in re.findall(r"[+-]?\d+(?:\.\d+)?", line_context or ""):
        val = float(tok)
        if abs(val) >= 100:
            continue
        return val
    return None


def usable_subject(r, plausible=plausible_low_match):
    """(subject_key, display_name, team) for a bet_quotes row (joined with
    players.full_name / player_team / teams.name), or None if the subject
    can't be identified well enough to rank or grade."""
    stype = r["subject_type"]
    if stype == "player":
        if not r["player_id"]:
            return None
        if r["match_confidence"] == "low" and not plausible(r["raw_mention"], r["full_name"]):
            return None
        team = r["player_team"] or r["team"]
        return ("p", r["player_id"]), r["full_name"], team
    if stype == "team":
        if not r["team"]:
            return None
        return ("t", r["team"]), r.get("team_name") or r["raw_mention"], r["team"]
    if not (r["team"] and r["opponent"]):
        return None
    return ("g", frozenset((r["team"], r["opponent"]))), r["raw_mention"], r["team"]
