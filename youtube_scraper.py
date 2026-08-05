"""
YouTube clip-channel scraper.

Stage 1 of the "source clips from YouTube" feature: resolves a list of
channel handles to channel IDs, pulls each channel's recent Shorts, and
filters/sorts by view count so we can see what's actually trending.

Stage 2 (not built yet): matching each viral clip back to its original,
unbranded Twitch clip — see tiktok_autopilot.py notes. Held off on that
until we can see real title formats from these specific channels, since
the matching heuristic needs to be tuned to real data, not guesses.

Auth: reads YOUTUBE_API_KEY from the environment (GitHub secret in CI,
or a git-ignored .env file locally via python-dotenv) — never hardcoded.
"""

import logging
import os
import re
from datetime import datetime, timezone, timedelta

import requests

log = logging.getLogger("youtube_scraper")

API_KEY = os.environ.get("YOUTUBE_API_KEY")
BASE_URL = "https://www.googleapis.com/youtube/v3"

# Channels to scrape — handle only (without the leading @ or trailing /shorts)
CHANNEL_HANDLES = [
    "Core.Clipperz",
    "StableRonaldoLive",
    "imgoochy",
    "core_fx",
    "Kaishowspeed_Shorts",
    "LudwinClips",
    "ClipMet",
    "RealPlugFinesser",
    "dailyspeedzone",
    "FaZeAdaptLive",
    "Coreclip5",
]

MAX_SHORT_SECONDS = 60
LOOKBACK_DAYS = 3          # how far back to consider "recent"
RESULTS_PER_CHANNEL = 25   # how many recent uploads to pull per channel before filtering


def _iso8601_duration_to_seconds(duration: str) -> int:
    """Parses YouTube's ISO 8601 duration format, e.g. 'PT1M5S' -> 65."""
    match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration)
    if not match:
        return 0
    hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def resolve_channel_id(handle: str):
    """Resolves a @handle to a channel ID, with a search fallback."""
    resp = requests.get(f"{BASE_URL}/channels", params={
        "part": "id",
        "forHandle": handle,
        "key": API_KEY,
    })
    data = resp.json()
    items = data.get("items", [])
    if items:
        return items[0]["id"]

    log.warning(f"forHandle lookup failed for '{handle}', trying search fallback")
    resp = requests.get(f"{BASE_URL}/search", params={
        "part": "snippet",
        "q": handle,
        "type": "channel",
        "maxResults": 1,
        "key": API_KEY,
    })
    items = resp.json().get("items", [])
    if items:
        return items[0]["snippet"]["channelId"]

    log.error(f"Could not resolve channel ID for handle '{handle}'")
    return None


def get_uploads_playlist_id(channel_id: str):
    resp = requests.get(f"{BASE_URL}/channels", params={
        "part": "contentDetails",
        "id": channel_id,
        "key": API_KEY,
    })
    items = resp.json().get("items", [])
    if not items:
        return None
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


def get_recent_video_ids(uploads_playlist_id: str, max_results: int):
    resp = requests.get(f"{BASE_URL}/playlistItems", params={
        "part": "contentDetails",
        "playlistId": uploads_playlist_id,
        "maxResults": max_results,
        "key": API_KEY,
    })
    items = resp.json().get("items", [])
    return [item["contentDetails"]["videoId"] for item in items]


def get_video_details(video_ids: list) -> list:
    """Batches video_ids into groups of 50 (API max per call)."""
    all_details = []
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        resp = requests.get(f"{BASE_URL}/videos", params={
            "part": "snippet,statistics,contentDetails",
            "id": ",".join(batch),
            "key": API_KEY,
        })
        for item in resp.json().get("items", []):
            all_details.append({
                "video_id": item["id"],
                "title": item["snippet"]["title"],
                "description": item["snippet"].get("description", ""),
                "published_at": item["snippet"]["publishedAt"],
                "view_count": int(item["statistics"].get("viewCount", 0)),
                "duration_seconds": _iso8601_duration_to_seconds(item["contentDetails"]["duration"]),
                "url": f"https://www.youtube.com/shorts/{item['id']}",
            })
    return all_details


def scrape_channel(handle: str) -> list:
    channel_id = resolve_channel_id(handle)
    if not channel_id:
        return []

    uploads_playlist_id = get_uploads_playlist_id(channel_id)
    if not uploads_playlist_id:
        log.error(f"No uploads playlist found for '{handle}'")
        return []

    video_ids = get_recent_video_ids(uploads_playlist_id, RESULTS_PER_CHANNEL)
    if not video_ids:
        return []

    details = get_video_details(video_ids)

    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    recent_shorts = [
        v for v in details
        if v["duration_seconds"] <= MAX_SHORT_SECONDS
        and datetime.fromisoformat(v["published_at"].replace("Z", "+00:00")) >= cutoff
    ]

    for v in recent_shorts:
        v["source_channel"] = handle

    log.info(f"'{handle}': {len(recent_shorts)} recent shorts (of {len(details)} pulled)")
    return recent_shorts


def scrape_all_channels() -> list:
    """Scrapes every configured channel and returns results sorted by view count, descending."""
    if not API_KEY:
        log.error("YOUTUBE_API_KEY is not set — cannot scrape")
        return []

    all_clips = []
    for handle in CHANNEL_HANDLES:
        all_clips.extend(scrape_channel(handle))

    all_clips.sort(key=lambda v: v["view_count"], reverse=True)
    return all_clips


if __name__ == "__main__":
    # Quick manual test: prints top 20 viral shorts across all channels.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    clips = scrape_all_channels()
    print(f"\n=== Top {min(20, len(clips))} of {len(clips)} recent shorts across all channels ===")
    for c in clips[:20]:
        print(f"{c['view_count']:>10,} views | {c['source_channel']:>20} | {c['title'][:60]}")
