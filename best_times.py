"""
best_times.py — Publer "Best Times to Post" integration.

Publer's best_times endpoint returns a heatmap (day-of-week -> 24 hourly
scores, 0-23) built from your account's historical performance. We cache
it locally (best_times_cache.json, refreshed at most once a week) and use
it to nudge each post's `scheduled_at` to the best-scoring hour within the
upcoming posting window, instead of always using "now + 2 minutes".

NOTE ON TIMEZONE: Publer's docs don't explicitly state which timezone the
returned hour indexes are in — almost certainly your workspace's configured
timezone. ACCOUNT_TZ_UTC_OFFSET below defaults to 0 (assumes it's already
UTC). Check Publer's workspace/account timezone setting; if your posts
start landing at an odd hour relative to what the heatmap suggests, set
this to (workspace_timezone_offset_from_UTC) in hours.
"""
import os, json, logging, requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

log = logging.getLogger("autopilot")

CACHE_PATH = Path("best_times_cache.json")
CACHE_MAX_AGE_DAYS = 7
LOOKBACK_DAYS = 90            # how much post history Publer should analyze
SEARCH_WINDOW_HOURS = 3       # how far ahead to look for the best slot (~matches post cadence)
ACCOUNT_TZ_UTC_OFFSET = 0     # hours; see NOTE above — adjust if needed

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _get_headers() -> dict:
    return {
        "Authorization": f"Bearer-API {os.environ['PUBLER_API_KEY']}",
        "Publer-Workspace-Id": os.environ["PUBLER_WORKSPACE_ID"],
    }


def _fetch_heatmap() -> dict | None:
    date_to = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    date_from = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    try:
        r = requests.get(
            f"https://app.publer.com/api/v1/analytics/{os.environ['PUBLER_ACCOUNT_ID']}/best_times",
            headers=_get_headers(),
            params={"from": date_from, "to": date_to},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"Could not fetch best_times from Publer: {e}")
        return None


def load_heatmap() -> dict | None:
    """Cached, refreshed at most once every CACHE_MAX_AGE_DAYS."""
    if CACHE_PATH.exists():
        try:
            cached = json.loads(CACHE_PATH.read_text())
            fetched_at = datetime.fromisoformat(cached["fetched_at"])
            if datetime.now(timezone.utc) - fetched_at < timedelta(days=CACHE_MAX_AGE_DAYS):
                return cached["heatmap"]
        except Exception as e:
            log.warning(f"best_times cache unreadable, will refetch: {e}")

    heatmap = _fetch_heatmap()
    if heatmap:
        CACHE_PATH.write_text(json.dumps({
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "heatmap": heatmap,
        }, indent=2))
        return heatmap

    # Fetch failed — fall back to a stale cache if one exists, else give up.
    if CACHE_PATH.exists():
        log.info("Using stale best_times cache (refresh failed)")
        return json.loads(CACHE_PATH.read_text())["heatmap"]
    return None


def best_scheduled_time(default_delay_minutes: int = 2) -> str:
    """
    Returns an ISO 8601 UTC timestamp to use as Publer's `scheduled_at`.
    Picks the highest-scoring hour from the heatmap within the next
    SEARCH_WINDOW_HOURS hours. Falls back to now + default_delay_minutes
    if no heatmap is available, or every candidate hour scores 0.
    """
    now = datetime.now(timezone.utc)
    fallback_dt = now + timedelta(minutes=default_delay_minutes)
    fallback = fallback_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    heatmap = load_heatmap()
    if not heatmap:
        return fallback

    best_score, best_dt = -1, None
    for h in range(SEARCH_WINDOW_HOURS + 1):
        candidate = now + timedelta(hours=h)
        local_hour = (candidate.hour + ACCOUNT_TZ_UTC_OFFSET) % 24
        day_name = DAY_NAMES[candidate.weekday()]
        scores = heatmap.get(day_name)
        if not scores or local_hour >= len(scores):
            continue
        score = scores[local_hour]
        if score > best_score:
            best_score = score
            best_dt = candidate.replace(minute=0, second=0, microsecond=0)

    if best_dt is None or best_score <= 0:
        log.info("No strong best_times signal in the search window — using default delay")
        return fallback

    if best_dt <= now:
        best_dt = fallback_dt

    log.info(f"Best posting slot in next {SEARCH_WINDOW_HOURS}h: {best_dt.isoformat()} (score {best_score})")
    return best_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
