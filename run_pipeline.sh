#!/bin/bash
# Runs the full NHL podcast pipeline unattended: starts Postgres, refreshes
# NHL rosters/schedule, polls for new episodes, processes them. Triggered by
# Windows Task Scheduler via:
#   wsl.exe -e /home/krg77/nhl-dashboard/run_pipeline.sh

export PATH=~/.npm-global/bin:~/.local/bin:$PATH

LOG_FILE=~/nhl-dashboard/pipeline.log
LOCK_FILE=/tmp/nhl_pipeline.lock

exec 200>"$LOCK_FILE"
flock -n 200 || { echo "=== Skipped (already running): $(date) ===" >> "$LOG_FILE"; exit 0; }

echo "=== Run started: $(date) ===" >> "$LOG_FILE"

sudo service postgresql start >> "$LOG_FILE" 2>&1

cd ~/nhl-dashboard || exit 1
./start_server.sh >> "$LOG_FILE" 2>&1
source venv/bin/activate

# worker.py shells out to the claude CLI, which needs CLAUDE_CODE_OAUTH_TOKEN
# in the environment when running unattended. .env is gitignored - never
# commit the token itself.
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# A killed prior run leaves is_running stuck at true; safe to reset here
# because the flock above guarantees nothing else is running.
python -c "from db import get_conn; c=get_conn(); cur=c.cursor(); cur.execute(\"UPDATE pipeline_status SET is_running=false WHERE id=1\"); c.commit()" >> "$LOG_FILE" 2>&1

# Opens this run's row in pipeline_runs (and marks any row left over from a
# killed run as interrupted).
python run_history.py begin >> "$LOG_FILE" 2>&1

# Rosters, aliases and game states change daily; a few seconds to refresh.
python load_nhl.py >> "$LOG_FILE" 2>&1

python poller.py >> "$LOG_FILE" 2>&1
python worker.py >> "$LOG_FILE" 2>&1

echo "=== Run finished: $(date) ===" >> "$LOG_FILE"
