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

# Fixed daily posting slots, in the account's local hour (24h). The bot runs
# early each day and assigns each post to the first of these slots that isn't
# already taken today — so 3 posts spread across the day and never collide on
# the same minute (Publer rejects posts <1 min apart).
FIXED_SLOTS_LOCAL_HOURS = [12, 15, 18]   # 12pm, 3pm, 6pm

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


def _todays_scheduled_hours() -> set:
    """
    Returns the set of local hours that already have a post scheduled for
    today, by asking Publer for this account's scheduled posts. Used to avoid
    double-booking a fixed slot across separate runs. On any error, returns an
    empty set (better to risk a rare collision than to skip posting).
    """
    try:
        today = datetime.now(timezone.utc).date()
        r = requests.get(
            "https://app.publer.com/api/v1/posts",
            headers=_get_headers(),
            params={"state": "scheduled", "postType": "video"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        posts = data.get("posts", data if isinstance(data, list) else [])
        hours = set()
        for p in posts:
            sched = p.get("scheduled_at") or p.get("scheduledAt")
            if not sched:
                continue
            try:
                dt = datetime.fromisoformat(sched.replace("Z", "+00:00"))
            except ValueError:
                continue
            if dt.date() == today:
                hours.add((dt.hour + ACCOUNT_TZ_UTC_OFFSET) % 24)
        return hours
    except Exception as e:
        log.warning(f"Could not fetch today's scheduled posts from Publer: {e}")
        return set()


def next_best_free_slot(min_lead_minutes: int = 30) -> str:
    """
    Data-driven scheduling without collisions. Picks the highest-scoring hour
    from the Publer best-times heatmap that (a) isn't already taken by a post
    scheduled today and (b) is still at least min_lead_minutes in the future.

    Because each run checks Publer for what's already booked, three separate
    runs naturally land on the 1st, 2nd, and 3rd best hours of the day instead
    of all colliding on the single top hour (the bug that made posts fail with
    "There's another post at this time").

    Falls back to the fixed slots, then to now+3h, if there's no heatmap.
    """
    now = datetime.now(timezone.utc)
    earliest = now + timedelta(minutes=min_lead_minutes)
    taken = _todays_scheduled_hours()
    heatmap = load_heatmap()

    if not heatmap:
        log.info("No heatmap — falling back to fixed slots")
        return next_free_fixed_slot()

    # Score every remaining hour today, skip taken/past ones, pick the best.
    today_name = DAY_NAMES[now.weekday()]
    scores = heatmap.get(today_name) or []

    best_score, best_dt = -1, None
    for utc_hour in range(24):
        candidate = now.replace(hour=utc_hour, minute=0, second=0, microsecond=0)
        if candidate < earliest:
            continue
        local_hour = (utc_hour + ACCOUNT_TZ_UTC_OFFSET) % 24
        if local_hour in taken:
            continue
        if local_hour >= len(scores):
            continue
        score = scores[local_hour]
        if score > best_score:
            best_score, best_dt = score, candidate

    if best_dt is None or best_score <= 0:
        log.info("No good free hour left today from heatmap — using fixed-slot fallback")
        return next_free_fixed_slot()

    log.info(f"Assigned best free slot: {best_dt.isoformat()} "
             f"(local {(best_dt.hour + ACCOUNT_TZ_UTC_OFFSET) % 24}:00, score {best_score})")
    return best_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def next_free_fixed_slot() -> str:
    """
    Picks the first FIXED_SLOTS_LOCAL_HOURS slot today that (a) isn't already
    taken by another scheduled post and (b) is still at least a few minutes in
    the future. Returns an ISO 8601 UTC timestamp for Publer/YouTube.

    Because the bot runs early (midnight–2am), all afternoon slots are in the
    future, so there's no 'scheduled in the past' risk. If every slot is taken
    or passed, falls back to the next open slot tomorrow.
    """
    now = datetime.now(timezone.utc)
    taken = _todays_scheduled_hours()

    def utc_for(local_hour: int, day_offset: int = 0) -> datetime:
        utc_hour = (local_hour - ACCOUNT_TZ_UTC_OFFSET) % 24
        return (now + timedelta(days=day_offset)).replace(
            hour=utc_hour, minute=0, second=0, microsecond=0)

    for day_offset in (0, 1):
        for local_hour in FIXED_SLOTS_LOCAL_HOURS:
            if day_offset == 0 and local_hour in taken:
                continue
            candidate = utc_for(local_hour, day_offset)
            if candidate <= now + timedelta(minutes=5):
                continue
            log.info(f"Assigned fixed slot: {candidate.isoformat()} "
                     f"({local_hour}:00 local, day+{day_offset})")
            return candidate.strftime("%Y-%m-%dT%H:%M:%SZ")

    fallback = now + timedelta(hours=3)
    log.warning("No free fixed slot found — using now + 3h fallback")
    return fallback.strftime("%Y-%m-%dT%H:%M:%SZ")


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


# Tracks hours already claimed within a single run so 3 posts scheduled in
# one cycle don't all pile onto the same best hour. Reset each run start.
_claimed_hours: set = set()


def best_scheduled_time_today(min_lead_minutes: int = 30, horizon_hours: int = 18) -> str:
    """
    Finds the best-scoring hour in the next `horizon_hours` (default 18, i.e.
    the rest of the day when the bot runs in the early morning), rather than
    just the next few hours. Designed for the "run the bot early, let it
    schedule posts for their best times later that day" workflow.

    - Only considers hours at least `min_lead_minutes` in the future, so we
      never hand Publer a slot that's already passing (the bug that dropped
      the Aug 15 post).
    - Avoids reusing an hour already claimed earlier in the same run, so 3
      posts in one cycle spread out instead of stacking on one peak hour.
    - Falls back to a staggered default if there's no heatmap or no
      positive-scoring hour left in the window.
    """
    now = datetime.now(timezone.utc)
    earliest = now + timedelta(minutes=min_lead_minutes)

    # Candidate slots: top of each hour, from `earliest` out to horizon.
    first = earliest.replace(minute=0, second=0, microsecond=0)
    if first < earliest:
        first += timedelta(hours=1)
    candidates = [first + timedelta(hours=i) for i in range(horizon_hours)]

    heatmap = load_heatmap()
    if not heatmap:
        slot = earliest + timedelta(hours=len(_claimed_hours) * 3)
        log.info("No heatmap — using staggered default")
        return slot.strftime("%Y-%m-%dT%H:%M:%SZ")

    best_score, best_dt = -1, None
    for candidate in candidates:
        local_hour = (candidate.hour + ACCOUNT_TZ_UTC_OFFSET) % 24
        key = (candidate.date(), candidate.hour)
        if key in _claimed_hours:
            continue
        scores = heatmap.get(DAY_NAMES[candidate.weekday()])
        if not scores or local_hour >= len(scores):
            continue
        score = scores[local_hour]
        if score > best_score:
            best_score = score
            best_dt = candidate

    if best_dt is None or best_score <= 0:
        slot = earliest + timedelta(hours=len(_claimed_hours) * 3)
        log.info("No strong best_times signal left in window — using staggered default")
        return slot.strftime("%Y-%m-%dT%H:%M:%SZ")

    _claimed_hours.add((best_dt.date(), best_dt.hour))
    log.info(f"Best posting slot today: {best_dt.isoformat()} (score {best_score})")
    return best_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
