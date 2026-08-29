from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

import yt_dlp

logger = logging.getLogger(__name__)

PLAT_CHAT_CHANNEL_URL = "https://www.youtube.com/@PlatChatVALORANT/streams"
PLAT_CHAT_LEAGUE = "Plat Chat VALORANT"
PLAT_CHAT_CHANNEL = "PlatChatVALORANT"
PLAT_CHAT_DURATION_SECONDS = 3.5 * 3600
MIN_REPLAY_DURATION_SECONDS = 1800
EPISODE_PATTERN = re.compile(r"(?:Episode|Ep\.)\s*(\d+)\s*$", re.IGNORECASE)

REPLAY_CHANNELS = {
    "lol": [
        "https://www.youtube.com/@LEC/videos", 
        "https://www.youtube.com/@LCS/videos",
        "https://www.youtube.com/@LCKglobal/videos",
        "https://www.youtube.com/@LPL_English/videos",
    ],
    "valorant": [
        "https://www.youtube.com/@VCTPacific/videos",
        "https://www.youtube.com/channel/UCifCesg-EUkjKyQedaB3hRg/videos",
        "https://www.youtube.com/channel/UCp6n8d8Y8r3MwKNw_MMaouQ/videos",
    ],
}

_FLAT_LIST_OPTS = {"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": "in_playlist"}
_FULL_VIDEO_OPTS = {"quiet": True, "no_warnings": True, "skip_download": True, "ignore_no_formats_error": True}


def _parse_episode(title: str) -> tuple[str, int | None]:
    match = EPISODE_PATTERN.search(title)
    if not match:
        return title.strip(), None
    return title[: match.start()].strip(" -"), int(match.group(1))


def _thumbnail_url(video_id: str) -> str:
    return f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"


def fetch_plat_chat_schedule() -> dict[str, Any] | None:
    try:
        with yt_dlp.YoutubeDL(_FLAT_LIST_OPTS) as ydl:
            listing = ydl.extract_info(PLAT_CHAT_CHANNEL_URL, download=False)
    except Exception:
        logger.exception("esportsarr: failed to list Plat Chat VALORANT streams")
        return None

    for entry in listing.get("entries") or []:
        if entry.get("live_status") not in ("is_upcoming", "is_live"):
            continue
        try:
            with yt_dlp.YoutubeDL(_FULL_VIDEO_OPTS) as ydl:
                video = ydl.extract_info(entry["url"], download=False)
        except Exception:
            logger.exception("esportsarr: failed to fetch Plat Chat video %r", entry.get("id"))
            continue
        release_timestamp = video.get("release_timestamp")
        if release_timestamp is None:
            continue
        topic, episode = _parse_episode(video.get("title", ""))
        return {
            "video_id": video["id"],
            "topic": topic,
            "episode": episode,
            "real_start": datetime.fromtimestamp(release_timestamp, tz=timezone.utc).isoformat(),
        }
    return None


def fetch_replay_candidates(channel_url: str, limit: int = 20) -> list[dict[str, Any]]:
    try:
        with yt_dlp.YoutubeDL({**_FLAT_LIST_OPTS, "playlistend": limit}) as ydl:
            listing = ydl.extract_info(channel_url, download=False)
    except Exception:
        logger.exception("esportsarr: failed to list replay candidates from %r", channel_url)
        return []

    return [
        {"id": entry["id"], "title": entry.get("title", ""), "duration_seconds": entry["duration"]}
        for entry in listing.get("entries") or []
        if entry.get("duration") and entry["duration"] >= MIN_REPLAY_DURATION_SECONDS
    ]


def fetch_supplemental_content() -> dict[str, Any]:
    """Fetch content metadata for the plugin's deterministic gap filler.

    Only metadata is downloaded. The plugin later turns these candidates into
    slot-sized schedule entries and plays each selected VOD by URL.
    """
    candidates: dict[str, list[dict[str, Any]]] = {}
    for game, channel_urls in REPLAY_CHANNELS.items():
        candidates[game] = [candidate for url in channel_urls for candidate in fetch_replay_candidates(url)]
    return {"plat_chat_schedule": fetch_plat_chat_schedule(), "replay_candidates": candidates}


def build_replay_match(game: str, candidate: dict[str, Any], start: datetime) -> dict[str, Any]:
    return {
        "league": "Replay",
        "game": game,
        "start": start.isoformat(),
        "state": "in_progress",
        "title": candidate["title"],
        "stream_platform": "youtube_vod",
        "stream_channel": candidate["id"],
        "description": candidate["title"],
        "has_real_content": True,
        "best_of": None,
        "duration_seconds": candidate["duration_seconds"],
        "categories": ["Esports"],
        "is_replay": True,
        "icon": _thumbnail_url(candidate["id"]),
    }
