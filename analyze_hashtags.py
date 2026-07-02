"""
Weekly hashtag A/B test analysis.

Pulls Publer Post Insights for the recent window, matches each published
post back to a hashtag set via its fingerprint tag (see hashtag_manager.py),
and — once every set has POSTS_PER_SET posts *with* insight data available —
ranks the sets on a composite score across views, engagement rate, and raw
engagement (likes+comments+shares), then locks in the winner.

Safe to run repeatedly (e.g. weekly via .github/workflows/analyze.yml, or
manually via workflow_dispatch) — it's a no-op once a winner is locked, and
just logs a status update if there isn't enough data yet.
"""
import os, logging, requests
from datetime import datetime, timezone, timedelta
import hashtag_manager as hm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("analyze")

ANALYSIS_WINDOW_DAYS = 45  # how far back to pull posts from Publer


def get_publer_headers() -> dict:
    return {
        "Authorization": f"Bearer-API {os.environ['PUBLER_API_KEY']}",
        "Publer-Workspace-Id": os.environ["PUBLER_WORKSPACE_ID"],
    }


def fetch_all_post_insights(account_id: str, date_from: str, date_to: str) -> list:
    headers = get_publer_headers()
    posts, page = [], 0
    while True:
        r = requests.get(
            f"https://app.publer.com/api/v1/analytics/{account_id}/post_insights",
            headers=headers,
            params={
                "from": date_from,
                "to": date_to,
                "page": page,
                "postType": "video",
                "sort_by": "scheduled_at",
                "sort_type": "DESC",
            },
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        batch = data.get("posts", [])
        posts.extend(batch)
        if not batch or len(posts) >= data.get("total", len(posts)):
            break
        page += 1
    return posts


def match_set(post_text: str) -> str | None:
    text = (post_text or "").lower()
    for name, info in hm.HASHTAG_SETS.items():
        if info["fingerprint"].lower() in text:
            return name
    return None


def main():
    state = hm.load_state()
    if state["phase"] == "locked":
        log.info(f"Already locked in on '{state['winner']}' — nothing to do.")
        return

    date_to = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    date_from = (datetime.now(timezone.utc) - timedelta(days=ANALYSIS_WINDOW_DAYS)).strftime("%Y-%m-%d")

    try:
        posts = fetch_all_post_insights(os.environ["PUBLER_ACCOUNT_ID"], date_from, date_to)
    except Exception as e:
        log.error(f"Failed to fetch Publer post insights: {e}")
        return
    log.info(f"Pulled {len(posts)} posts from Publer ({date_from} to {date_to})")

    per_set = {name: [] for name in hm.SET_ORDER}
    for post in posts:
        name = match_set(post.get("text", ""))
        if not name:
            continue
        a = post.get("analytics", {}) or {}
        per_set[name].append({
            "views": a.get("video_views", 0) or 0,
            "engagement_rate": a.get("engagement_rate", 0) or 0,
            "raw_engagement": (a.get("likes", 0) or 0)
                + (a.get("comments", 0) or 0)
                + (a.get("shares", 0) or 0),
        })

    summary = {}
    for name, rows in per_set.items():
        n = len(rows)
        summary[name] = {
            "matched_posts": n,
            "avg_views": sum(r["views"] for r in rows) / n if n else 0,
            "avg_engagement_rate": sum(r["engagement_rate"] for r in rows) / n if n else 0,
            "avg_raw_engagement": sum(r["raw_engagement"] for r in rows) / n if n else 0,
        }

    log.info("── Hashtag set performance (matched posts w/ insights) ──")
    for name, s in summary.items():
        log.info(
            f"{name}: matched={s['matched_posts']}/{hm.POSTS_PER_SET} "
            f"avg_views={s['avg_views']:.0f} "
            f"avg_engagement_rate={s['avg_engagement_rate']:.2f}% "
            f"avg_raw_engagement={s['avg_raw_engagement']:.1f}"
        )
    log.info(f"Posts sent so far (by set, from hashtag_state.json): {state['counts']}")

    if not hm.testing_complete():
        log.info(f"Still rotating — not every set has reached {hm.POSTS_PER_SET} posted yet.")
        return

    ready = [name for name, s in summary.items() if s["matched_posts"] >= hm.POSTS_PER_SET]
    if len(ready) < len(hm.SET_ORDER):
        log.info(
            "All sets have been posted enough times, but Publer's insight data "
            "hasn't caught up for every set yet (insights sync ~daily). Try again in a day or two."
        )
        return

    # Composite rank across all three metrics — lower total rank is better.
    ranks = {name: 0 for name in hm.SET_ORDER}
    for metric in ("avg_views", "avg_engagement_rate", "avg_raw_engagement"):
        ordered = sorted(hm.SET_ORDER, key=lambda n: summary[n][metric], reverse=True)
        for i, name in enumerate(ordered):
            ranks[name] += i

    winner = min(ranks, key=ranks.get)
    log.info(f"Composite ranks (lower=better): {ranks}")
    log.info(f"Winner: {winner}")
    hm.lock_winner(winner)
    log.info(f"Locked in '{winner}' — all future posts will use this hashtag set only.")


if __name__ == "__main__":
    main()
