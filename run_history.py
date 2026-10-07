"""
Run history for the pipeline: one row in pipeline_runs per scheduled run,
so "did it actually collect anything while I was away?" has a dated answer.

Lifecycle:
  - run_pipeline.sh calls `python run_history.py begin` before the poller.
    That closes out any row still marked 'running' as 'interrupted' (safe:
    the flock in run_pipeline.sh guarantees no other run is alive), then
    opens a fresh row.
  - poller.py records how many new episodes it found.
  - worker.py fills in planned/processed/errored/items and closes the row.
  - A row left 'running' by a killed process shows up as 'interrupted'
    with no finished_at - the "started but never finished" case.

Identical copy lives in both ff-dashboard and stock-dashboard.
"""
import sys
from datetime import datetime, timezone
from db import get_conn

SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id SERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    outcome TEXT NOT NULL DEFAULT 'running',
    episodes_found INTEGER NOT NULL DEFAULT 0,
    episodes_planned INTEGER NOT NULL DEFAULT 0,
    episodes_processed INTEGER NOT NULL DEFAULT 0,
    episodes_errored INTEGER NOT NULL DEFAULT 0,
    items_added INTEGER NOT NULL DEFAULT 0,
    note TEXT
);
ALTER TABLE episodes ADD COLUMN IF NOT EXISTS error_message TEXT;
"""

_COUNTERS = {"episodes_processed", "episodes_errored", "items_added"}


def ensure_schema(cur, conn):
    # Checked first because ALTER TABLE takes an exclusive lock on episodes
    # even when IF NOT EXISTS makes it a no-op - not something to do on every
    # API request or poll while workers hold the table.
    cur.execute(
        "SELECT to_regclass('pipeline_runs') IS NOT NULL AND EXISTS ("
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'episodes' AND column_name = 'error_message')"
    )
    if cur.fetchone()[0]:
        conn.commit()
        return
    cur.execute(SCHEMA)
    conn.commit()


def begin_run(cur, conn):
    ensure_schema(cur, conn)
    cur.execute(
        "UPDATE pipeline_runs SET outcome = 'interrupted' WHERE outcome = 'running'"
    )
    cur.execute(
        "INSERT INTO pipeline_runs (started_at) VALUES (%s) RETURNING id",
        (datetime.now(timezone.utc),),
    )
    run_id = cur.fetchone()[0]
    conn.commit()
    return run_id


def open_run(cur, conn):
    """The run currently in flight, or a new one if worker.py was started standalone."""
    ensure_schema(cur, conn)
    cur.execute("SELECT id FROM pipeline_runs WHERE outcome = 'running' ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    return row[0] if row else begin_run(cur, conn)


def note_episodes_found(cur, conn, n):
    ensure_schema(cur, conn)
    cur.execute(
        "UPDATE pipeline_runs SET episodes_found = episodes_found + %s "
        "WHERE id = (SELECT id FROM pipeline_runs WHERE outcome = 'running' ORDER BY id DESC LIMIT 1)",
        (n,),
    )
    conn.commit()


def set_planned(cur, conn, run_id, n):
    cur.execute("UPDATE pipeline_runs SET episodes_planned = %s WHERE id = %s", (n, run_id))
    conn.commit()


def add_progress(cur, conn, run_id, processed=0, errored=0, items=0):
    cur.execute(
        "UPDATE pipeline_runs SET episodes_processed = episodes_processed + %s, "
        "episodes_errored = episodes_errored + %s, items_added = items_added + %s WHERE id = %s",
        (processed, errored, items, run_id),
    )
    conn.commit()


def finish_run(cur, conn, run_id, outcome=None, note=None):
    """outcome=None derives it: 'idle' (nothing to do), 'errors', or 'ok'."""
    if outcome is None:
        cur.execute(
            "SELECT episodes_planned, episodes_errored FROM pipeline_runs WHERE id = %s", (run_id,)
        )
        planned, errored = cur.fetchone()
        outcome = "idle" if planned == 0 else ("errors" if errored else "ok")
    cur.execute(
        "UPDATE pipeline_runs SET finished_at = %s, outcome = %s, note = %s WHERE id = %s",
        (datetime.now(timezone.utc), outcome, note, run_id),
    )
    conn.commit()


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "begin":
        c = get_conn()
        k = c.cursor()
        print(f"Run history: opened run {begin_run(k, c)}")
        k.close()
        c.close()
    else:
        print("usage: python run_history.py begin")
        sys.exit(2)
