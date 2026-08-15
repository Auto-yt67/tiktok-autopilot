"""
Direct YouTube Shorts upload via the YouTube Data API v3.

Auth model: a long-lived OAuth2 refresh token (obtained ONCE by running
youtube_auth_setup.py locally — never in CI) is stored as a GitHub secret.
Each run exchanges it for a short-lived access token, no browser/consent
step needed after the initial setup.

Required secrets (see youtube_auth_setup.py for how to generate them):
    YOUTUBE_CLIENT_ID
    YOUTUBE_CLIENT_SECRET
    YOUTUBE_REFRESH_TOKEN

Quota note: each upload costs 1,600 of the free 10,000 daily units, so this
supports at most ~6 uploads/day on the free tier.
"""

import logging
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

log = logging.getLogger("youtube")

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CATEGORY_ID = "24"  # Entertainment


def _get_credentials() -> Credentials:
    creds = Credentials(
        token=None,
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YOUTUBE_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return creds


def upload_short(video_path: Path, title: str, description: str,
                  privacy: str = "public", publish_at: str = None) -> bool:
    """
    Uploads video_path as a YouTube Short. YouTube auto-detects Shorts
    treatment from aspect ratio (9:16) + duration (<=60s) — both already
    guaranteed upstream in process_video()/main(), so no #Shorts tag needed,
    though it doesn't hurt discoverability to have one in the description.

    If publish_at (an ISO 8601 UTC timestamp like "2026-08-16T20:00:00Z") is
    given, the video is uploaded as PRIVATE with a scheduled publish time, so
    YouTube automatically makes it public at that moment — matching the slot
    the bot picks for the TikTok post, so both go live together. Scheduling
    is a normal built-in YouTube feature and doesn't affect reach.
    """
    try:
        creds = _get_credentials()
        youtube = build("youtube", "v3", credentials=creds)

        status = {"selfDeclaredMadeForKids": False}
        if publish_at:
            # A scheduled video must be uploaded as private; YouTube flips it
            # to public at publishAt.
            status["privacyStatus"] = "private"
            status["publishAt"] = publish_at
        else:
            status["privacyStatus"] = privacy

        body = {
            "snippet": {
                "title": title[:100],
                "description": description,
                "categoryId": CATEGORY_ID,
            },
            "status": status,
        }

        media = MediaFileUpload(str(video_path), mimetype="video/mp4", resumable=True)
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

        response = None
        while response is None:
            _, response = request.next_chunk()

        video_id = response.get("id")
        if publish_at:
            log.info(f"✓ Uploaded to YouTube (scheduled for {publish_at}): https://youtube.com/shorts/{video_id}")
        else:
            log.info(f"✓ Uploaded to YouTube Shorts: https://youtube.com/shorts/{video_id}")
        return True
    except Exception as e:
        log.error(f"YouTube upload failed: {e}")
        return False
