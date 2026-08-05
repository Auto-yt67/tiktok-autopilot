"""
Twitch-original matching.

Stage 2 of the "source clips from YouTube" feature. Two jobs:

1. determine_primary_broadcaster() — auto-detects which Twitch streamer a
   given YouTube clip-channel is actually reposting, by finding the hashtag
   that repeats across nearly all of that channel's clips (generic tags like
   #shorts/#funny are filtered out via GENERIC_TAGS). No hand-maintained
   name dictionary needed — confirmed against real scraped data where e.g.
   'imgoochy' carries #jasontheween on every clip, 'core_fx' carries
   #stableronaldo on every clip, etc.

2. find_twitch_original() — once we know the broadcaster, searches that
   broadcaster's Twitch clips around the YouTube video's publish date and
   fuzzy-matches titles to find the clean, unbranded original. Returns None
   (no match) rather than a low-confidence guess — per the requirement that
   we only ever use a confirmed-clean clip, never fall back to the branded
   YouTube repost.
"""

import difflib
import logging
import os
import re
import time
from datetime import datetime, timedelta

import requests

log = logging.getLogger("twitch_match")

TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET")

GENERIC_TAGS = {
    "shorts", "short", "funny", "viral", "viralclip", "viralclips", "clip",
    "clips", "streamer", "streamers", "reaction", "comedy", "gaming", "game",
    "sports", "fyp", "foryou", "foryoupage", "trending", "meme", "memes",
    "live", "highlight", "highlights", "twitch", "youtube", "subscribe",
    "fypage", "explorepage", "viralvideo", "epic", "lol", "lmao",
}

MATCH_LOOKBACK_DAYS = 14   # how far before the YT repost date to search Twitch clips
MATCH_SCORE_THRESHOLD = 0.30  # minimum title similarity to accept a match

_token_cache = {"access_token": None, "expires_at": 0}


def _get_twitch_token() -> str:
    if _token_cache["access_token"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["access_token"]

    resp = requests.post("https://id.twitch.tv/oauth2/token", params={
        "client_id": TWITCH_CLIENT_ID,
        "client_secret": TWITCH_CLIENT_SECRET,
        "grant_type": "client_credentials",
    })
    data = resp.json()
    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = time.time() + data.get("expires_in", 3600) - 60
    return _token_cache["access_token"]


def _twitch_headers() -> dict:
    return {
        "Client-Id": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {_get_twitch_token()}",
    }


def _extract_hashtags(description: str) -> list:
    return [tag.lower() for tag in re.findall(r"#(\w+)", description or "")]


def determine_primary_broadcaster(channel_clips: list) -> str:
    """
    channel_clips: all scraped clips belonging to a single source_channel.
    Returns the most likely Twitch login for that channel's primary
    broadcaster, or None if no confident candidate is found.
    """
    tag_counts = {}
    for clip in channel_clips:
        tags = set(_extract_hashtags(clip.get("description", ""))) - GENERIC_TAGS
        for tag in tags:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

    if not tag_counts:
        return None

    total_clips = len(channel_clips)
    ranked = sorted(tag_counts.items(), key=lambda kv: kv[1], reverse=True)

    for candidate, count in ranked:
        # Require the tag to show up on a clear majority of the channel's
        # clips, not just once or twice (guests only appear sporadically).
        if count / total_clips < 0.4:
            break
        if _twitch_login_exists(candidate):
            return candidate

    return None


def _twitch_login_exists(login: str) -> bool:
    resp = requests.get(
        "https://api.twitch.tv/helix/users",
        headers=_twitch_headers(),
        params={"login": login},
    )
    return bool(resp.json().get("data"))


def _get_broadcaster_id(login: str):
    resp = requests.get(
        "https://api.twitch.tv/helix/users",
        headers=_twitch_headers(),
        params={"login": login},
    )
    data = resp.json().get("data", [])
    return data[0]["id"] if data else None


def _title_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def find_twitch_original(clip: dict, broadcaster_login: str):
    """
    Searches broadcaster_login's Twitch clips in a window before the
    YouTube repost date and returns the best title-matching clip, or None
    if nothing clears MATCH_SCORE_THRESHOLD (no low-confidence fallback).
    """
    broadcaster_id = _get_broadcaster_id(broadcaster_login)
    if not broadcaster_id:
        log.warning(f"Could not resolve Twitch broadcaster '{broadcaster_login}'")
        return None

    published_at = datetime.fromisoformat(clip["published_at"].replace("Z", "+00:00"))
    started_at = published_at - timedelta(days=MATCH_LOOKBACK_DAYS)

    resp = requests.get(
        "https://api.twitch.tv/helix/clips",
        headers=_twitch_headers(),
        params={
            "broadcaster_id": broadcaster_id,
            "started_at": started_at.isoformat(),
            "ended_at": published_at.isoformat(),
            "first": 100,
        },
    )
    candidates = resp.json().get("data", [])
    if not candidates:
        log.info(f"No Twitch clips found for '{broadcaster_login}' in window")
        return None

    # Build a search string from the YT title plus any non-generic hashtags
    # (guest names etc.) to give the matcher more signal than title alone.
    tags = [t for t in _extract_hashtags(clip.get("description", "")) if t not in GENERIC_TAGS]
    search_text = clip["title"] + " " + " ".join(tags)

    best_match, best_score = None, 0.0
    for c in candidates:
        score = _title_similarity(search_text, c["title"])
        if score > best_score:
            best_match, best_score = c, score

    if best_match and best_score >= MATCH_SCORE_THRESHOLD:
        log.info(f"Matched (score={best_score:.2f}): '{clip['title'][:50]}' -> '{best_match['title'][:50]}'")
        return {
            "twitch_clip_url": best_match["url"],
            "twitch_title": best_match["title"],
            "twitch_view_count": best_match["view_count"],
            "match_score": best_score,
        }

    log.info(f"No confident Twitch match for '{clip['title'][:50]}' (best score={best_score:.2f})")
    return None
