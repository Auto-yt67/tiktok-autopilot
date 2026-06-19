"""
TikTok Autopilot — Trending Twitch Clips Edition
GitHub Actions version: runs once per invocation (no loop/scheduler needed)
"""

import os, json, logging, requests, random, re, subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger("autopilot")

MAX_CLIP_SECONDS = 90
MIN_CLIP_VIEWS   = 5_000
TOP_GAMES_COUNT  = 15
CLIPS_PER_GAME   = 10
DOWNLOAD_DIR     = Path("downloads")
POSTED_LOG       = Path("posted.json")
CAPTION_LIMIT    = 2200
FONT_PATH        = "/usr/share/fonts/truetype/MochiBoom.ttf"

BASE_TAGS = [
    "#fyp", "#foryou", "#foryoupage", "#viral", "#trending",
    "#twitch", "#twitchclips", "#streamer", "#streaming", "#livestreaming",
]

GAME_TAGS = {
    "just chatting":       ["#justchatting", "#IRL"],
    "fortnite":            ["#fortnite", "#fortniteclips", "#fn"],
    "minecraft":           ["#minecraft", "#minecraftclips"],
    "league of legends":   ["#leagueoflegends", "#lol", "#league"],
    "valorant":            ["#valorant", "#valorantclips", "#val"],
    "grand theft auto v":  ["#gta", "#gtav", "#gta5", "#gtarp"],
    "call of duty":        ["#callofduty", "#cod", "#warzone"],
    "apex legends":        ["#apex", "#apexlegends", "#apexclips"],
    "overwatch":           ["#overwatch", "#ow2"],
    "counter-strike":      ["#csgo", "#cs2", "#counterstrike"],
    "world of warcraft":   ["#wow", "#worldofwarcraft"],
    "elden ring":          ["#eldenring", "#fromsoftware"],
    "chess":               ["#chess", "#chesstwitch"],
    "slots":               ["#slots", "#casino", "#gambling"],
}

CAPTION_OPENERS = [
    "bro really said 💀",
    "no way this actually happened 😭",
    "stream moment of the year 🏆",
    "chat was NOT ready 😭",
    "this streamer just 💀",
    "i can't stop watching this 😂",
    "only on twitch 💀",
    "the chat reaction alone 😭",
]

DOWNLOAD_DIR.mkdir(exist_ok=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_posted() -> dict:
    if POSTED_LOG.exists():
        return json.loads(POSTED_LOG.read_text())
    return {"urls": [], "clip_ids": [], "history": []}


def save_posted(clip: dict):
    data = load_posted()
    if clip["url"] not in data["urls"]:
        data["urls"].append(clip["url"])
    if clip["clip_id"] not in data.get("clip_ids", []):
        data.setdefault("clip_ids", []).append(clip["clip_id"])
    data["history"].append({
        "url": clip["url"],
        "clip_id": clip["clip_id"],
        "streamer": clip["streamer"],
        "game": clip.get("game", ""),
        "title": clip["title"],
        "twitch_views": clip.get("views", 0),
        "posted_at": datetime.now(timezone.utc).isoformat(),
    })
    POSTED_LOG.write_text(json.dumps(data, indent=2))


def already_posted(clip: dict) -> bool:
    data = load_posted()
    return (clip["url"] in data.get("urls", []) or
            clip["clip_id"] in data.get("clip_ids", []))


def build_caption(clip: dict) -> str:
    streamer = clip["streamer"]
    game = clip.get("game", "").lower()
    opener = random.choice(CAPTION_OPENERS)
    streamer_tag = f"#{re.sub(r'[^a-zA-Z0-9]', '', streamer.lower())}"
    extra_tags = []
    for game_key, tags in GAME_TAGS.items():
        if game_key in game:
            extra_tags = tags
            break
    all_tags = BASE_TAGS + [streamer_tag] + extra_tags
    caption = f"{opener} {streamer}"
    for tag in all_tags:
        candidate = caption + " " + tag
        if len(candidate) <= CAPTION_LIMIT:
            caption = candidate
    return caption


def hex_to_ffmpeg(hex_color: str) -> str:
    return "0x" + hex_color.lstrip("#")


def get_title_fontsize(title: str) -> int:
    length = len(title)
    if length <= 25:
        return 72
    elif length <= 40:
        return 58
    elif length <= 55:
        return 46
    elif length <= 70:
        return 38
    else:
        return 32


# ── Twitch ────────────────────────────────────────────────────────────────────

def get_twitch_token() -> str:
    r = requests.post("https://id.twitch.tv/oauth2/token", params={
        "client_id": os.environ["TWITCH_CLIENT_ID"],
        "client_secret": os.environ["TWITCH_CLIENT_SECRET"],
        "grant_type": "client_credentials",
    })
    r.raise_for_status()
    return r.json()["access_token"]


def get_twitch_headers() -> dict:
    return {
        "Client-ID": os.environ["TWITCH_CLIENT_ID"],
        "Authorization": f"Bearer {get_twitch_token()}",
    }


def get_trending_game_ids(headers: dict) -> list:
    try:
        r = requests.get(
            "https://api.twitch.tv/helix/games/top",
            headers=headers,
            params={"first": TOP_GAMES_COUNT},
            timeout=15,
        )
        r.raise_for_status()
        games = r.json().get("data", [])
        log.info(f"Trending: {[g['name'] for g in games]}")
        return [(g["id"], g["name"]) for g in games]
    except Exception as e:
        log.warning(f"Could not fetch trending games: {e}")
        return [
            ("509658", "Just Chatting"), ("33214", "Fortnite"),
            ("32982", "Grand Theft Auto V"), ("27471", "Minecraft"),
            ("516575", "Valorant"), ("29307", "League of Legends"),
        ]


def fetch_clips_for_game(game_id: str, game_name: str, headers: dict) -> list:
    posted = load_posted()
    posted_urls = set(posted.get("urls", []))
    posted_ids = set(posted.get("clip_ids", []))
    try:
        r = requests.get(
            "https://api.twitch.tv/helix/clips",
            headers=headers,
            params={"game_id": game_id, "first": CLIPS_PER_GAME},
            timeout=15,
        )
        r.raise_for_status()
        results = []
        for clip in r.json().get("data", []):
            if clip.get("language", "") != "en":
                continue
            if clip["duration"] > MAX_CLIP_SECONDS:
                continue
            if clip["view_count"] < MIN_CLIP_VIEWS:
                continue
            if clip["url"] in posted_urls or clip["id"] in posted_ids:
                continue
            results.append({
                "title": clip["title"],
                "url": clip["url"],
                "clip_id": clip["id"],
                "streamer": clip["broadcaster_name"],
                "game": game_name,
                "views": clip["view_count"],
                "duration": clip["duration"],
            })
        return results
    except Exception as e:
        log.warning(f"Error fetching clips for {game_name}: {e}")
        return []


def scrape_viral_clips() -> list:
    headers = get_twitch_headers()
    trending_games = get_trending_game_ids(headers)
    all_clips = []
    seen_ids = set()
    for game_id, game_name in trending_games:
        for clip in fetch_clips_for_game(game_id, game_name, headers):
            if clip["clip_id"] not in seen_ids:
                seen_ids.add(clip["clip_id"])
                all_clips.append(clip)
    all_clips.sort(key=lambda x: x["views"], reverse=True)
    seen_streamers = set()
    final = []
    for clip in all_clips:
        if clip["streamer"].lower() not in seen_streamers:
            seen_streamers.add(clip["streamer"].lower())
            final.append(clip)
        if len(final) >= 15:
            break
    log.info(f"Found {len(final)} clips")
    return final


# ── Download ──────────────────────────────────────────────────────────────────

def download_clip(clip: dict) -> Path | None:
    clip_id = clip["clip_id"]
    out_path = DOWNLOAD_DIR / f"{clip_id}.mp4"
    if out_path.exists():
        return out_path
    try:
        import yt_dlp
        log.info(f"Downloading: {clip['url']}")
        ydl_opts = {
            "outtmpl": str(DOWNLOAD_DIR / f"{clip_id}.%(ext)s"),
            "format": "mp4/best[ext=mp4]/best",
            "quiet": True,
            "no_warnings": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([clip["url"]])
        files = sorted(DOWNLOAD_DIR.glob(f"{clip_id}.*"), key=lambda f: f.stat().st_mtime)
        return files[-1] if files else None
    except Exception as e:
        log.error(f"Download failed: {e}")
        return None


# ── Video Processing ──────────────────────────────────────────────────────────

def get_video_dimensions(video_path: Path) -> tuple:
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", str(video_path)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        data = json.loads(result.stdout)
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                return stream["width"], stream["height"]
    except Exception:
        pass
    return 1080, 1920


def transcribe_audio(video_path: Path) -> list:
    try:
        import whisper
        model = whisper.load_model("tiny")
        result = model.transcribe(str(video_path), word_timestamps=True)
        words = []
        for segment in result.get("segments", []):
            for word_info in segment.get("words", []):
                words.append({
                    "word": word_info["word"].strip(),
                    "start": word_info["start"],
                    "end": word_info["end"],
                })
        return words
    except Exception as e:
        log.warning(f"Whisper failed: {e}")
        return []


def build_caption_filter(words: list, caption_y: int) -> str:
    if not words:
        return ""
    filters = []
    for i in range(0, len(words), 2):
        chunk = words[i:i+2]
        text = " ".join(w["word"] for w in chunk)
        start = chunk[0]["start"]
        end = chunk[-1]["end"]
        clean = ''.join(c for c in text if ord(c) < 128).strip()
        clean = clean.replace("'", "").replace('"', '').replace(':', '').replace('\\', '')
        if not clean.strip():
            continue
        filters.append(
            f"drawtext=text='{clean}':fontfile={FONT_PATH}:fontcolor=white:fontsize=56"
            f":borderw=4:bordercolor=black:x=(w-text_w)/2:y={caption_y}"
            f":enable='between(t,{start},{end})'"
        )
    return ",".join(filters)


def process_video(video_path: Path, title: str) -> Path:
    """
    Build a 1080x1920 vertical video:
      - Background: same clip, scaled up + blurred to fill the frame
      - Foreground: clip scaled to fit width (no stretch), centered
      - Title bar overlay: white text, black outline
      - Captions: 2-word groups, white text, black outline, near bottom of clip
    """
    try:
        src_w, src_h = get_video_dimensions(video_path)

        TARGET_W, TARGET_H = 1080, 1920

        clean_title = ''.join(c for c in title if ord(c) < 128).strip()
        clean_title = clean_title.replace("'", "").replace('"', '').replace(':', '').replace('\\', '')
        if len(clean_title) > 80:
            clean_title = clean_title[:80]

        title_fontsize = get_title_fontsize(clean_title)
        outline_fontsize = max(title_fontsize - 20, 24)
        log.info(f"Title: '{clean_title}' | fontsize: {title_fontsize}")

        # Foreground height once scaled to fill target width, used to find
        # where the actual clip content ends (so captions sit on top of it,
        # not down in the blurred letterbox area).
        fg_height = src_h * (TARGET_W / src_w)
        fg_top = max(0, (TARGET_H - fg_height) / 2)
        fg_bottom = fg_top + fg_height

        bar_height = max(130, int(TARGET_H * 0.07))
        title_y = int(bar_height / 2) - 30

        # Captions sit near the bottom of the actual visible clip (not the
        # blurred background), matching the original layout.
        caption_y = int(min(fg_bottom, TARGET_H) - 160)

        words = transcribe_audio(video_path)
        caption_filter = build_caption_filter(words, caption_y)

        # Foreground: scale to fit width, keep aspect ratio
        fg_scale = f"scale={TARGET_W}:-2"

        # Background: scale up to fill frame entirely + heavy blur
        bg_scale = f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,crop={TARGET_W}:{TARGET_H},gblur=sigma=20"

        # Title text filters: white fill, black outline only
        title_filters = []
        for dx, dy in [(-3,-3),(-3,0),(-3,3),(0,-3),(0,3),(3,-3),(3,0),(3,3)]:
            title_filters.append(
                f"drawtext=text='{clean_title}':fontfile={FONT_PATH}:fontcolor=black"
                f":fontsize={outline_fontsize}:x=(w-text_w)/2+{dx}:y={title_y}+{dy}"
            )
        title_filters.append(
            f"drawtext=text='{clean_title}':fontfile={FONT_PATH}:fontcolor=white"
            f":fontsize={title_fontsize}:x=(w-text_w)/2:y={title_y}"
        )

        overlay_filters = ",".join(title_filters)
        if caption_filter:
            overlay_filters += "," + caption_filter

        # Full filter graph:
        # 1. split source into bg/fg copies
        # 2. bg: scale+crop+blur to fill 1080x1920
        # 3. fg: scale to fit width, keep aspect
        # 4. overlay fg centered on bg
        # 5. draw title + captions on top
        filter_complex = (
            f"[0:v]split=2[bg_src][fg_src];"
            f"[bg_src]{bg_scale}[bg];"
            f"[fg_src]{fg_scale}[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2[merged];"
            f"[merged]{overlay_filters}[outv]"
        )

        out_path = video_path.parent / f"processed_{video_path.name}"
        cmd = [
            "ffmpeg", "-y", "-i", str(video_path),
            "-filter_complex", filter_complex,
            "-map", "[outv]", "-map", "0:a?",
            "-preset", "ultrafast",
            "-codec:a", "copy",
            str(out_path)
        ]

        log.info("Running ffmpeg...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0 and out_path.exists():
            log.info("Video processed successfully")
            return out_path
        else:
            log.warning(f"ffmpeg failed: {result.stderr[-500:]}, using original")
            return video_path
    except Exception as e:
        log.error(f"Video processing error: {e}")
        return video_path


# ── Publer ────────────────────────────────────────────────────────────────────

def get_publer_headers() -> dict:
    return {
        "Authorization": f"Bearer-API {os.environ['PUBLER_API_KEY']}",
        "Publer-Workspace-Id": os.environ["PUBLER_WORKSPACE_ID"],
        "Content-Type": "application/json",
    }


def upload_to_publer(video_path: Path) -> str | None:
    headers = {
        "Authorization": f"Bearer-API {os.environ['PUBLER_API_KEY']}",
        "Publer-Workspace-Id": os.environ["PUBLER_WORKSPACE_ID"],
    }
    try:
        with open(video_path, "rb") as f:
            r = requests.post(
                "https://app.publer.com/api/v1/media",
                headers=headers,
                files={"file": (video_path.name, f, "video/mp4")},
                timeout=120,
            )
        r.raise_for_status()
        data = r.json()
        return data.get("id") or data.get("media_id")
    except Exception as e:
        log.error(f"Publer upload failed: {e}")
        return None


def post_to_publer(video_path: Path, caption: str) -> bool:
    headers = get_publer_headers()
    try:
        log.info("Uploading to Publer...")
        media_id = upload_to_publer(video_path)
        if not media_id:
            raise Exception("No media ID returned")
        scheduled_at = (datetime.now(timezone.utc) + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = {
            "bulk": {
                "state": "scheduled",
                "posts": [{
                    "networks": {
                        "tiktok": {
                            "type": "video",
                            "text": caption,
                            "media": [{"id": media_id}],
                            "privacy_level": "PUBLIC_TO_EVERYONE",
                            "duet_disabled": False,
                            "stitch_disabled": False,
                            "comment_disabled": False,
                        }
                    },
                    "accounts": [{"id": os.environ["PUBLER_ACCOUNT_ID"], "scheduled_at": scheduled_at}],
                }],
            }
        }
        r = requests.post("https://app.publer.com/api/v1/posts/schedule",
                          headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        log.info(f"✓ Queued in Publer: {caption[:80]}")
        return True
    except Exception as e:
        log.error(f"Publer post failed: {e}")
        return False


# ── Main (single run) ─────────────────────────────────────────────────────────

def main():
    log.info("── Starting post cycle ──")
    clips = scrape_viral_clips()
    if not clips:
        log.info("No new clips found")
        return

    for clip in clips:
        if already_posted(clip):
            continue
        log.info(f"Trying: {clip['streamer']} — {clip['title'][:50]} ({clip['views']:,} views)")
        video_path = download_clip(clip)
        if not video_path:
            continue
        processed_path = process_video(video_path, clip["title"])
        caption = build_caption(clip)
        success = post_to_publer(processed_path, caption)
        if success:
            save_posted(clip)
            log.info("Done!")
            return

    log.warning("Could not post anything this cycle")


if __name__ == "__main__":
    main()
