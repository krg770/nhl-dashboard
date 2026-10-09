# NHL Betting Buzz

A local dashboard that listens to NHL betting podcasts and ranks the bets
analysts are talking about — players, teams, and whole games — by how many
different analysts are discussing the same bet for a game that hasn't
started yet.

**Informational comparison only.** Nothing here is a recommendation, and no
output is used for any monetary decision. The 0–100 score measures how
strongly analysts *agree* (and how fresh / well-supported the talk is); it is
not a probability that a bet hits.

Sister project to `ff-dashboard` (port 8000) and `stock-dashboard` (port 8001);
this one runs on **port 8002** with its own `nhl_dashboard` database.

## How it works

```
Windows Task Scheduler (hourly, :40)
  -> wsl.exe -e run_pipeline.sh
       flock guard (one run at a time)
       run_history.py begin   open a row in pipeline_runs
       load_nhl.py            teams, rosters, schedule/scores (api-web.nhle.com)
       poller.py              RSS feeds -> episodes (status new / skipped)
       worker.py              download -> faster-whisper -> `claude -p` extraction -> bet_quotes
```

- **Shows** (seeded in `schema.sql`): NHL-only — Hockey Gambling Podcast,
  PuckTime, Sick Hockey Picks, The Ice Guys; multi-sport — The Daily Juice
  (BettingPros), whose episodes are processed only when the title mentions
  hockey. `episode_filter.py` also skips anything older than 20h — daily
  betting talk is worthless once the games are played.
- **Subjects**: `player` (goals, shots, points, saves, ...), `team`
  (moneyline, puck line, team total), `game` (total, first period, ...),
  plus season-long futures.
- **Ranking** (`/api/best_bets`): a pick is (subject, bet type, game, side).
  Side is over/under for lined bets or backing/fading otherwise, so analysts
  on opposite sides rank as competing picks. Analysts are counted once per
  bet. Takes about games that already started, and recaps of past results,
  are excluded.
- **Results & track record** (`grade_results.py`, run every pipeline run after
  `load_nhl.py`): finished games' box scores and play-by-play are stored, then
  every bet where the analyst took a side is graded hit / miss / push / void
  (player didn't play). Grading is deliberately conservative — a bet needs an
  explicit over/under with a number, a signed puck line, a moneyline, an
  anytime/first goal, etc. Odds alone ("+100") are not a line, so those bets are
  stored as `ungraded` with the reason and counted on the page rather than
  guessed. Totals and puck lines exclude the shootout-deciding goal.
  `/api/results` shows a night's graded bets per game; `/api/accuracy` rolls
  them up per show, per analyst and per bet type (each analyst counts once per
  distinct bet). Show-level records are dependable; analyst names come from
  transcripts without voice labelling, so individuals are best-effort. Shared
  rules (analyst identity, side, game assignment, line parsing) live in
  `bet_logic.py` so ranking and grading can never disagree.
- **Matching**: last-name-only mentions are common in hockey talk, so shared
  last names are disambiguated by the team the analyst mentioned; if that
  can't settle it the take is left unmatched rather than guessed.

## Setup

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
# .env: DB_* settings, WORK_DIR, CLAUDE_CODE_OAUTH_TOKEN (gitignored - never commit)
./venv/bin/python init_db.py     # creates nhl_dashboard + schema + show list
./venv/bin/python load_nhl.py    # teams, rosters, schedule
./start_server.sh                # http://localhost:8002
```

`start_server.sh` is idempotent, so the Windows Startup launcher and
`run_pipeline.sh` can both call it safely.
