"""Fills a genuinely idle slot with supplemental content instead of "No
Match Scheduled" -- Plat Chat VALORANT (a live weekly show) and official
match replays -- via yt-dlp's metadata-only extraction. Keyless, same
no-credentials approach as the youtubearr plugin ("Zero API Quota: uses
yt-dlp instead of the YouTube Data API"); no JS runtime is needed either,
since we only ever read title/duration/thumbnail metadata, never resolve a
playable format ourselves -- Dispatcharr's own stream profile plays the
plain youtube.com URL, exactly like it already does for LPL.

This only ever fills a slot that `assign_slots` left empty; it never
competes with or bumps a real esports match (see plugin.py's _run_sync)."""

from __future__ import annotations

import logging
import random
import re
from datetime import datetime, timezone
from typing import Any

import yt_dlp

logger = logging.getLogger(__name__)

PLAT_CHAT_CHANNEL_URL = "https://www.youtube.com/@PlatChatVALORANT/streams"
PLAT_CHAT_LEAGUE = "Plat Chat VALORANT"
PLAT_CHAT_CHANNEL = "PlatChatVALORANT"
# A live show with no real end time until it happens; 3.5h is the middle of
# the "three to four hours" range Arnaud gave (2026-07-30). Same
# "estimate, not truth" caveat as esports match durations.
PLAT_CHAT_DURATION_SECONDS = 3.5 * 3600

REPLAY_CATEGORY = "Replay"
LIVE_CATEGORY = "Live"

# Ends with "... Ep. 274" or "... Episode 274" -- confirmed against Plat
# Chat's real titles, e.g. "The BEST teams in VCT right now are..? --
# Plat Chat VALORANT Ep. 274".
EPISODE_PATTERN = re.compile(r"(?:Episode|Ep\.)\s*(\d+)\s*$", re.IGNORECASE)

# Metadata only, no download, no JS runtime needed for either.
_FLAT_LIST_OPTS = {"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": "in_playlist"}
_FULL_VIDEO_OPTS = {"quiet": True, "no_warnings": True, "skip_download": True, "ignore_no_formats_error": True}


def _thumbnail_url(video_id: str) -> str:
    return f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"


def _parse_episode(title: str) -> tuple[str, int | None]:
    """Splits a video title into (topic, episode_number). Episode is None if
    the title doesn't end with the expected "... Ep. N" suffix."""
    match = EPISODE_PATTERN.search(title)
    if not match:
        return title.strip(), None
    topic = title[: match.start()].strip(" -–—")
    return topic, int(match.group(1))


def fetch_plat_chat_live_info(now: datetime) -> dict[str, Any] | None:
    """None if Plat Chat isn't live (or about to start airing) right now.
    Otherwise a virtual match dict, ready for the same slot-assignment
    pipeline real Riot matches go through."""
    try:
        with yt_dlp.YoutubeDL(_FLAT_LIST_OPTS) as ydl:
            listing = ydl.extract_info(PLAT_CHAT_CHANNEL_URL, download=False)
    except Exception:
        logger.exception("esportsarr: failed to list Plat Chat VALORANT's streams")
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
        start = datetime.fromtimestamp(release_timestamp, tz=timezone.utc)
        if video.get("live_status") != "is_live" and start > now:
            continue  # scheduled but not actually airing yet -- nothing to show

        topic, episode = _parse_episode(video.get("title", ""))
        return {
            "league": PLAT_CHAT_LEAGUE,
            "game": "valorant",
            "start": now.isoformat(),
            "state": "in_progress",
            "title": PLAT_CHAT_LEAGUE,
            "stream_platform": "youtube",
            "stream_channel": PLAT_CHAT_CHANNEL,
            "description": f"{topic} · Episode {episode}" if episode else topic,
            "has_real_content": True,
            "best_of": None,
            "duration_seconds": PLAT_CHAT_DURATION_SECONDS,
            "category": LIVE_CATEGORY,
            "icon": _thumbnail_url(video["id"]),
        }
    return None


def fetch_replay_candidates(channel_url: str, limit: int = 20) -> list[dict[str, Any]]:
    """Recent completed uploads from a VOD channel/playlist URL, most-recent
    first. Entries still live/upcoming (no known duration yet) are skipped
    -- they aren't real VODs yet."""
    try:
        with yt_dlp.YoutubeDL({**_FLAT_LIST_OPTS, "playlistend": limit}) as ydl:
            listing = ydl.extract_info(channel_url, download=False)
    except Exception:
        logger.exception("esportsarr: failed to list replay candidates from %r", channel_url)
        return []

    candidates = []
    for entry in listing.get("entries") or []:
        duration = entry.get("duration")
        if not duration:
            continue
        candidates.append({"id": entry["id"], "title": entry.get("title", ""), "duration_seconds": duration})
    return candidates


def pick_replay(candidates: list[dict[str, Any]], seed: str) -> dict[str, Any] | None:
    """Deterministic for a given `seed` so the same replay holds for a whole
    idle stretch instead of changing every sync tick. Include the date (and
    ideally the slot index) in `seed` so it still varies day to day and
    across slots."""
    if not candidates:
        return None
    return random.Random(seed).choice(candidates)


def build_replay_match(game: str, league_name: str, candidate: dict[str, Any], now: datetime) -> dict[str, Any]:
    return {
        "league": league_name,
        "game": game,
        "start": now.isoformat(),
        "state": "in_progress",
        "title": league_name,
        "stream_platform": "youtube_vod",
        "stream_channel": candidate["id"],
        "description": candidate["title"],
        "has_real_content": True,
        "best_of": None,
        "duration_seconds": candidate["duration_seconds"],
        "category": REPLAY_CATEGORY,
        "icon": _thumbnail_url(candidate["id"]),
    }
