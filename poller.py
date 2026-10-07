"""
Polls every active podcast's RSS feed for new episodes and inserts them into
the episodes table with status='new' (or 'skipped' when episode_filter says
it isn't worth transcribing). Run on a schedule via run_pipeline.sh.

Usage:
    python poller.py
"""
import feedparser
from datetime import datetime, timezone
from db import get_conn
import episode_filter
import run_history


def poll_all(podcast_id=None):
    """Check every active feed, or just `podcast_id` when given."""
    conn = get_conn()
    cur = conn.cursor()

    if podcast_id is not None:
        cur.execute("SELECT id, name, rss_url FROM podcasts WHERE active = true AND id = %s", (podcast_id,))
    else:
        cur.execute("SELECT id, name, rss_url FROM podcasts WHERE active = true")
    podcasts = cur.fetchall()

    total_new = 0
    for pid, name, rss_url in podcasts:
        print(f"Checking {name}...")
        feed = feedparser.parse(rss_url)

        for entry in feed.entries:
            guid = entry.get("id") or entry.get("link")
            if not guid:
                continue

            cur.execute("SELECT 1 FROM episodes WHERE podcast_id = %s AND guid = %s", (pid, guid))
            if cur.fetchone():
                continue  # already seen this episode

            title = entry.get("title", "")
            published_struct = entry.get("published_parsed")
            published_at = (
                datetime(*published_struct[:6], tzinfo=timezone.utc)
                if published_struct
                else datetime.now(timezone.utc)
            )

            audio_url = None
            for link in entry.get("links", []):
                if link.get("type", "").startswith("audio"):
                    audio_url = link.get("href")
                    break
            if not audio_url:
                for enc in entry.get("enclosures", []):
                    if enc.get("type", "").startswith("audio"):
                        audio_url = enc.get("href")
                        break

            reason = episode_filter.skip_reason(name, title, published_at)
            if reason:
                cur.execute(
                    """
                    INSERT INTO episodes (podcast_id, guid, title, published_at, audio_url, status, error_message)
                    VALUES (%s, %s, %s, %s, %s, 'skipped', %s)
                    """,
                    (pid, guid, title, published_at, audio_url, reason),
                )
                continue

            cur.execute(
                """
                INSERT INTO episodes (podcast_id, guid, title, published_at, audio_url, status)
                VALUES (%s, %s, %s, %s, %s, 'new')
                """,
                (pid, guid, title, published_at, audio_url),
            )
            total_new += 1
            print(f"  New episode: {title}")

    cur.execute(
        "UPDATE pipeline_status SET last_poll_at = %s, last_poll_new_count = %s WHERE id = 1",
        (datetime.now(timezone.utc), total_new),
    )
    conn.commit()
    run_history.note_episodes_found(cur, conn, total_new)
    cur.close()
    conn.close()
    print(f"Done. {total_new} new episode(s) found.")


if __name__ == "__main__":
    poll_all()
