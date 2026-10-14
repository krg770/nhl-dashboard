"""
Processes episodes with status='new' (or 'transcribed' - resumed after an
interrupted run): downloads audio, speeds it up, transcribes with
faster-whisper, runs the Claude Code CLI extraction prompt, and inserts the
bets into bet_quotes.

Usage:
    python worker.py
"""
import os
import re
import atexit
import fcntl
import json
import difflib
import subprocess
import requests
from pathlib import Path
from datetime import datetime, timezone
from faster_whisper import WhisperModel
from db import get_conn
import run_history
from extraction_parse import parse_extraction_output
from fast_transcribe import transcribe_parallel

WORK_DIR = Path(os.getenv("WORK_DIR", "./work"))
WORK_DIR.mkdir(parents=True, exist_ok=True)

EXTRACTION_PROMPT_PATH = Path(__file__).parent / "extraction_prompt.md"
WHISPER_MODEL_SIZE = "small"
SPEED_FACTOR = "1.5"
TRANSCRIBE_LOCK = "/tmp/whisper_transcribe.lock"
# NHL picks are only useful before puck drop, so while this worker has episodes
# to process it raises this flag; the football and stock workers check it
# between episodes and wait their turn (they are only ever delayed, and the flag
# is ignored once it is 30 minutes stale, e.g. if this process was killed).
PRIORITY_FLAG = "/tmp/whisper_nhl_priority"


def _raise_priority():
    Path(PRIORITY_FLAG).touch()


def _drop_priority():
    Path(PRIORITY_FLAG).unlink(missing_ok=True)
# A timeout leaves the episode at status=error, retryable from the dashboard
# (the transcript is kept, so a retry only re-runs extraction).
EXTRACTION_TIMEOUT_SEC = int(os.getenv("EXTRACTION_TIMEOUT_SEC", "1200"))
# Daily-slate shows: cap what one run takes on so a backlog can't monopolise the CPU.
MAX_EPISODES_PER_RUN = 6

_NAME_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v"}


def download_audio(url: str, dest: Path):
    resp = requests.get(url, stream=True, timeout=120)
    resp.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)


def speed_up_audio(src: Path, dest: Path, factor: str = SPEED_FACTOR):
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-filter:a", f"atempo={factor}", str(dest)],
        check=True,
        capture_output=True,
    )


def transcribe(audio_path: Path, model: WhisperModel) -> str:
    # One transcription at a time across ALL dashboards (ff, stock, nhl share
    # this lock file). Each worker uses most of the CPU on its own; running
    # two or three at once oversubscribes the cores and every one of them
    # slows to a crawl (observed: a ~5 min episode taking 40+ min).
    with open(TRANSCRIBE_LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # NHL gets the whole machine: split the episode and transcribe the chunks
        # in parallel (see fast_transcribe.py), several times faster than one
        # process. Falls back to the single-process path if anything goes wrong.
        try:
            return transcribe_parallel(audio_path, WHISPER_MODEL_SIZE)
        except Exception as e:
            print(f"  parallel transcription failed ({e}); falling back to a single process")
            segments, _info = model.transcribe(str(audio_path))
            return "\n".join(f"[{seg.start:.0f}s] {seg.text.strip()}" for seg in segments)


def build_context(cur) -> str:
    """Teams (with the nicknames people say) and the active roster, so the
    model can recognise garbled names and attach the right team."""
    cur.execute(
        "SELECT t.abbrev, t.name, string_agg(a.alias, ', ' ORDER BY a.alias) "
        "FROM teams t LEFT JOIN team_aliases a ON a.team = t.abbrev GROUP BY t.abbrev, t.name ORDER BY t.abbrev"
    )
    teams = "\n".join(f"{ab} = {name} (also: {aliases})" for ab, name, aliases in cur.fetchall())
    cur.execute("SELECT full_name, team, position FROM players WHERE active = true ORDER BY team, full_name")
    roster = "\n".join(f"{n} ({t} - {p})" for n, t, p in cur.fetchall())
    return f"---TEAMS---\n{teams}\n\n---ROSTER---\n{roster}"


def run_extraction(transcript: str, context: str) -> list:
    prompt = EXTRACTION_PROMPT_PATH.read_text()
    stdin_content = f"{context}\n\n---TRANSCRIPT---\n{transcript}"
    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            input=stdin_content,
            capture_output=True,
            text=True,
            timeout=EXTRACTION_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        # Re-raised without the original message, which embeds the whole prompt.
        raise RuntimeError(f"claude extraction timed out after {EXTRACTION_TIMEOUT_SEC}s")
    # Raises (-> episode is recorded as an error and can be retried) rather than
    # returning [] when the output is unusable; see extraction_parse.py.
    items = parse_extraction_output(result.stdout)
    if not items and len(transcript) > 40000:
        print(f"NOTE: empty extraction for a long transcript ({len(transcript)} chars) - worth a look.")
    return items


def update_pipeline_status(cur, conn, **fields):
    if not fields:
        return
    set_clause = ", ".join(f"{k} = %s" for k in fields)
    cur.execute(f"UPDATE pipeline_status SET {set_clause} WHERE id = 1", list(fields.values()))
    conn.commit()


# ---------------------------------------------------------------- matching

def resolve_team(cur, raw):
    """(abbrev, confidence) for a team as said ("Oilers", "Habs", "EDM")."""
    if not raw:
        return None, "unreviewed"
    cur.execute("SELECT DISTINCT team FROM team_aliases WHERE lower(alias) = lower(%s)", (raw.strip(),))
    rows = cur.fetchall()
    if len(rows) == 1:
        return rows[0][0], "high"
    cur.execute(
        "SELECT team, similarity(alias, %s) AS sim FROM team_aliases ORDER BY sim DESC LIMIT 1", (raw.strip(),)
    )
    row = cur.fetchone()
    if row and row[1] and row[1] > 0.5:
        return row[0], "low"
    return None, "unreviewed"


def resolve_player(cur, raw, team_hint=None):
    """(player_id, confidence). Last-name-only mentions are common in hockey
    talk, so an alias shared by several players is disambiguated by the team
    the analyst mentioned; if that can't settle it we leave it unmatched
    rather than guess."""
    if not raw:
        return None, "unreviewed"
    raw = raw.strip()
    cur.execute(
        "SELECT DISTINCT p.id, p.team FROM player_aliases a JOIN players p ON p.id = a.player_id "
        "WHERE lower(a.alias) = lower(%s) AND p.active = true",
        (raw,),
    )
    rows = cur.fetchall()
    if len(rows) == 1:
        return rows[0][0], "high"
    if len(rows) > 1:
        if team_hint:
            same_team = [r for r in rows if r[1] == team_hint]
            if len(same_team) == 1:
                return same_team[0][0], "high"
        return None, "unreviewed"

    cur.execute(
        """
        SELECT p.id, p.full_name, similarity(a.alias, %s) AS sim
        FROM player_aliases a JOIN players p ON p.id = a.player_id
        WHERE p.active = true
        ORDER BY sim DESC LIMIT 1
        """,
        (raw,),
    )
    row = cur.fetchone()
    if row and row[2] and row[2] > 0.4:
        return row[0], "low"
    return None, "unreviewed"


def split_game_teams(raw):
    parts = re.split(r"\s*(?:/|\bvs\.?\b|\bversus\b|\bat\b|@|\band\b|-)\s*", raw or "", flags=re.I)
    return [p for p in parts if p]


def insert_bet_quotes(cur, episode_id, items):
    n = 0
    for q in items:
        if not isinstance(q, dict) or not q.get("quote_text") or not q.get("raw_mention"):
            continue
        subject = q.get("subject_type")
        if subject not in ("player", "team", "game"):
            continue
        raw = q["raw_mention"]
        team_hint, _ = resolve_team(cur, q.get("team_hint"))
        opp, _ = resolve_team(cur, q.get("opponent_hint"))
        player_id, confidence, team = None, "unreviewed", team_hint

        if subject == "player":
            player_id, confidence = resolve_player(cur, raw, team_hint)
            if player_id:
                cur.execute("SELECT team FROM players WHERE id = %s", (player_id,))
                team = cur.fetchone()[0] or team_hint
        elif subject == "team":
            team, confidence = resolve_team(cur, raw)
            team = team or team_hint
            if team_hint and not team:
                confidence = "low"
        else:  # game
            teams = [resolve_team(cur, p)[0] for p in split_game_teams(raw)]
            teams = [t for t in teams if t]
            if len(teams) >= 2:
                team, opp, confidence = teams[0], teams[1], "high"
            elif team_hint and opp:
                team, confidence = team_hint, "high"
            elif team_hint or teams:
                team, confidence = (teams[0] if teams else team_hint), "low"

        side = q.get("side")
        line = q.get("line_context")
        if side in ("over", "under") and line and side not in str(line).lower():
            line = f"{side} {line}"
        elif side in ("over", "under") and not line:
            line = side

        cur.execute(
            """
            INSERT INTO bet_quotes (
                episode_id, subject_type, player_id, team, opponent, raw_mention, match_confidence,
                bet_type, lean, sharp_or_public, timeframe, line_context, quote_text, speaker,
                timestamp_sec, tags, betting_relevance
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (episode_id, quote_text, raw_mention) DO NOTHING
            """,
            (
                episode_id, subject, player_id, team, opp, raw, confidence,
                q.get("bet_type") or f"{subject}_other",
                q.get("lean") if q.get("lean") in ("favorable", "unfavorable", "neutral") else "neutral",
                q.get("sharp_or_public") if q.get("sharp_or_public") in ("sharp", "public") else None,
                q.get("timeframe"), line, q["quote_text"], q.get("speaker"),
                q.get("timestamp_sec") if isinstance(q.get("timestamp_sec"), int) else None,
                json.dumps(q.get("tags", [])), q.get("betting_relevance"),
            ),
        )
        n += cur.rowcount
    return n


# ---------------------------------------------------------------- pipeline

def process_episode(episode_id, title, audio_url, model, cur, conn):
    raw_path = WORK_DIR / f"{episode_id}_raw.mp3"
    fast_path = WORK_DIR / f"{episode_id}_fast.mp3"

    update_pipeline_status(
        cur, conn,
        current_episode_id=episode_id, current_episode_title=title,
        stage="downloading", stage_started_at=datetime.now(timezone.utc),
    )

    # A retried/resumed episode already has its transcript.
    cur.execute("SELECT transcript FROM episodes WHERE id = %s", (episode_id,))
    transcript = (cur.fetchone() or [None])[0]

    if transcript:
        print(f"  Reusing saved transcript for episode {episode_id} (skipping download/transcribe).")
    else:
        print(f"  Downloading episode {episode_id}...")
        download_audio(audio_url, raw_path)
        print("  Speeding up audio...")
        speed_up_audio(raw_path, fast_path)
        update_pipeline_status(cur, conn, stage="transcribing", stage_started_at=datetime.now(timezone.utc))
        print("  Transcribing...")
        transcript = transcribe(fast_path, model)
        cur.execute(
            "UPDATE episodes SET status = 'transcribed', transcript = %s WHERE id = %s",
            (transcript, episode_id),
        )
        conn.commit()

    update_pipeline_status(cur, conn, stage="extracting", stage_started_at=datetime.now(timezone.utc))
    print("  Extracting NHL bets...")
    items = run_extraction(transcript, build_context(cur))
    n = insert_bet_quotes(cur, episode_id, items)

    cur.execute("UPDATE episodes SET status = 'extracted' WHERE id = %s", (episode_id,))
    conn.commit()
    print(f"  Done. {n} bet quote(s) extracted.")

    raw_path.unlink(missing_ok=True)
    fast_path.unlink(missing_ok=True)
    return n


def run():
    run_timestamp = datetime.now(timezone.utc)

    conn = get_conn()
    cur = conn.cursor()

    # Newest first: for daily picks shows, today's episode matters far more
    # than one from yesterday that is still waiting.
    cur.execute(
        """
        SELECT id, title, audio_url FROM episodes
        WHERE status IN ('new', 'transcribed') AND audio_url IS NOT NULL
        ORDER BY published_at DESC
        LIMIT %s
        """,
        (MAX_EPISODES_PER_RUN,),
    )
    episodes = cur.fetchall()

    run_id = run_history.open_run(cur, conn)
    run_history.set_planned(cur, conn, run_id, len(episodes))

    if not episodes:
        print("No new episodes to process.")
        run_history.finish_run(cur, conn, run_id)
        cur.close()
        conn.close()
        return

    update_pipeline_status(
        cur, conn,
        is_running=True, run_started_at=run_timestamp,
        episodes_total_this_run=len(episodes), episodes_done_this_run=0,
        current_episode_id=None, current_episode_title=None,
        stage="loading_model", stage_started_at=run_timestamp,
        avg_seconds_per_episode=None,
    )

    _raise_priority()
    atexit.register(_drop_priority)  # also covers a crash; a kill is covered by the 30 min staleness check
    print(f"Loading Whisper model ({WHISPER_MODEL_SIZE})...")
    model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8", cpu_threads=8)

    done = 0
    for episode_id, title, audio_url in episodes:
        _raise_priority()  # heartbeat: keeps the flag fresh for the other workers
        try:
            n = process_episode(episode_id, title, audio_url, model, cur, conn)
            # Stamped when the episode actually finishes.
            cur.execute(
                "UPDATE episodes SET processed_at = %s, error_message = NULL WHERE id = %s",
                (datetime.now(timezone.utc), episode_id),
            )
            conn.commit()
            run_history.add_progress(cur, conn, run_id, processed=1, items=n)
        except Exception as e:
            print(f"  ERROR processing episode {episode_id}: {e}")
            conn.rollback()
            cur.execute(
                "UPDATE episodes SET status = 'error', error_message = %s, processed_at = %s WHERE id = %s",
                (str(e)[:500], datetime.now(timezone.utc), episode_id),
            )
            conn.commit()
            run_history.add_progress(cur, conn, run_id, errored=1)
        finally:
            done += 1
            elapsed_total = (datetime.now(timezone.utc) - run_timestamp).total_seconds()
            update_pipeline_status(
                cur, conn,
                episodes_done_this_run=done,
                avg_seconds_per_episode=elapsed_total / done,
            )

    update_pipeline_status(
        cur, conn,
        is_running=False, current_episode_id=None, current_episode_title=None,
        stage=None, last_finished_at=datetime.now(timezone.utc),
    )
    run_history.finish_run(cur, conn, run_id)
    _drop_priority()

    cur.close()
    conn.close()


if __name__ == "__main__":
    run()
