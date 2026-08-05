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
MATCH_SCORE_THRESHOLD = 0.55  # raw title-similarity fallback bar when there's no keyword overlap

# Common English function words — filtered out of title-derived keywords so
# things like "Has", "Use", "To", "His" don't count as false "named entity"
# overlaps between unrelated clips.
STOPWORDS = {
    "the", "is", "are", "was", "were", "has", "have", "had", "use", "used",
    "to", "of", "in", "on", "at", "by", "for", "with", "and", "or", "but",
    "his", "her", "their", "its", "he", "she", "it", "they", "them", "him",
    "this", "that", "from", "as", "be", "been", "being", "a", "an", "will",
    "would", "can", "could", "should", "not", "no", "yes", "you", "your",
    "i", "we", "us", "our", "do", "does", "did", "get", "gets", "got", "go",
    "goes", "went", "when", "why", "how", "what", "who", "there",
}

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
    # Sort by frequency first, then by length descending — when tags tie on
    # frequency (common: a channel's own branding tag coincidentally being a
    # real but unrelated Twitch username, e.g. 'core' vs 'stableronaldo'),
    # prefer the longer/more specific one.
    ranked = sorted(tag_counts.items(), key=lambda kv: (kv[1], len(kv[0])), reverse=True)

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


def _extract_keywords(clip: dict, broadcaster_login: str) -> set:
    """
    Pulls 'named entity'-ish signal out of a clip: non-generic hashtags
    (guest names etc.) plus capitalized words from the title, minus
    stopwords and the broadcaster's own name. This is what we require
    actual overlap on — raw title-string similarity alone proved too weak
    a signal (Twitch clip titles are often generic/auto-generated and
    unrelated in wording to the YouTube repost's rewritten title).
    """
    tags = set(_extract_hashtags(clip.get("description", ""))) - GENERIC_TAGS - {broadcaster_login}
    title_words = {w.lower() for w in re.findall(r"\b[A-Za-z']{3,}\b", clip["title"])}
    title_words -= STOPWORDS
    title_words -= GENERIC_TAGS
    title_words.discard(broadcaster_login)
    return tags | title_words


def find_twitch_original(clip: dict, broadcaster_login: str):
    """
    Searches broadcaster_login's Twitch clips in a window before the
    YouTube repost date and returns the best match — but ONLY if it clears
    a real evidence bar: either a shared keyword (guest name, distinctive
    word) with the Twitch clip's title, or a very high raw title similarity
    as a fallback. Returns None otherwise — a wrong match is worse than no
    match, so this errs toward skipping over guessing.
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

    keywords = _extract_keywords(clip, broadcaster_login)

    best_match, best_ratio, best_overlap = None, 0.0, 0
    for c in candidates:
        c_title_lower = c["title"].lower()
        overlap = sum(1 for kw in keywords if kw in c_title_lower)
        ratio = _title_similarity(clip["title"], c["title"])
        # Rank primarily by keyword overlap (strongest signal), then ratio.
        if (overlap, ratio) > (best_overlap, best_ratio):
            best_match, best_ratio, best_overlap = c, ratio, overlap

    if not best_match:
        return None

    confident = best_overlap >= 1 or best_ratio >= MATCH_SCORE_THRESHOLD
    if confident:
        log.info(
            f"Matched (keywords={best_overlap}, ratio={best_ratio:.2f}): "
            f"'{clip['title'][:50]}' -> '{best_match['title'][:50]}'"
        )
        return {
            "twitch_clip_url": best_match["url"],
            "twitch_title": best_match["title"],
            "twitch_view_count": best_match["view_count"],
            "match_score": best_ratio,
            "keyword_overlap": best_overlap,
        }

    log.info(
        f"No confident Twitch match for '{clip['title'][:50]}' "
        f"(best keywords={best_overlap}, ratio={best_ratio:.2f})"
    )
    return None
