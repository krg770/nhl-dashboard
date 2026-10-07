"""
Relevance filter applied by the poller before an episode is queued. The
expensive step is transcription, so anything clearly not about NHL betting
is marked 'skipped' up front.

All shows currently configured are NHL-only, so nothing is title-filtered.
If a mixed-sport betting show is added later, list it in MIXED_SHOWS and
episodes will need "NHL" or "hockey" in the title to be processed.

Daily betting talk is worthless once the games are played, so episodes older
than MAX_AGE_HOURS are also skipped - this is what keeps a newly added show
(or a laptop that was off for days) from dumping its back catalog into the
queue.
"""
import re
from datetime import datetime, timedelta, timezone

MIXED_SHOWS = set()
# Daily-slate picks are about tonight's/tomorrow's games, so their useful life
# is roughly a day. The evening-before shows (published ~11pm for the next
# day's slate) are still comfortably inside this when picked up the next morning.
MAX_AGE_HOURS = 20

_HOCKEY = re.compile(r"\b(nhl|hockey|stanley cup|puck)\b", re.I)


def skip_reason(show_name, title, published_at):
    """Why this episode should not be processed, or None to process it."""
    if published_at is not None:
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - published_at > timedelta(hours=MAX_AGE_HOURS):
            return f"skipped: older than {MAX_AGE_HOURS}h (daily betting talk goes stale)"
    if show_name in MIXED_SHOWS and not _HOCKEY.search(title or ""):
        return "skipped: no hockey in title"
    return None
