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

import json
import logging
import os
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import yt_dlp

logger = logging.getLogger(__name__)

PLAT_CHAT_CHANNEL_URL = "https://www.youtube.com/@PlatChatVALORANT/streams"
PLAT_CHAT_LEAGUE = "Plat Chat VALORANT"
PLAT_CHAT_CHANNEL = "PlatChatVALORANT"
# A live show with no real end time until it happens; 3.5h is the middle of
# the "three to four hours" range Arnaud gave (2026-07-30). Same
# "estimate, not truth" caveat as esports match durations.
PLAT_CHAT_DURATION_SECONDS = 3.5 * 3600

# The live sync tick runs every 60s (poll_interval_seconds), but a replay
# candidate list and Plat Chat's schedule barely change within a day --
# refetching via yt-dlp on every single tick is wasteful and needlessly
# fragile (Arnaud, 2026-07-30: "preselect it... instead of doing constant
# fetches"). get_cached_replay_candidates/get_cached_plat_chat_schedule
# reuse a fetch from disk until it's older than this, so yt-dlp only
# actually runs roughly once a day.
CACHE_FILE_PATH = "/app/data/plugins/esportsarr/.state/supplemental-cache.json"
CACHE_TTL = timedelta(hours=24)
# A full match VOD runs well over an hour; anything under this is a clip,
# highlight reel, or recap, not a real replay (confirmed as a real bug,
# 2026-07-30: candidates as short as ~12 and ~43 minutes were being shown as
# full replay blocks).
MIN_REPLAY_DURATION_SECONDS = 1800
# A genuinely empty/null result (nothing found, or the fetch itself failed)
# gets a much shorter TTL than a real find -- caching "nothing" for the
# full 24h risks missing an episode that gets announced/goes live an hour
# after a check that happened to run too early (confirmed as a real bug,
# 2026-07-30: Plat Chat never showed up because the first, cold-cache check
# found nothing and that null result was then trusted for the rest of the
# day). Once something real IS found, its own timing is fixed and known,
# so it's safe to trust for the full CACHE_TTL.
NEGATIVE_CACHE_TTL = timedelta(hours=1)


def _read_cache() -> dict[str, Any]:
    try:
        with open(CACHE_FILE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_cache(cache: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(CACHE_FILE_PATH), exist_ok=True)
    with open(CACHE_FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f)


def _is_cache_entry_fresh(entry: dict[str, Any] | None, now: datetime, ttl: timedelta) -> bool:
    if not entry or "fetched_at" not in entry:
        return False
    return now - datetime.fromisoformat(entry["fetched_at"]) < ttl

# Both Plat Chat and replays are still esports-adjacent content, but
# neither is itself a live sports MATCH, so neither gets the "Sports"
# category real matches default to (channel_sync.DEFAULT_CATEGORIES) --
# whether something is live vs a rerun is instead the standard XMLTV
# <live/>/<previously-shown/> tags, not a category string.
SUPPLEMENTAL_CATEGORIES = ["Esports"]

# Ends with "... Ep. 274" or "... Episode 274" -- confirmed against Plat
# Chat's real titles, e.g. "The BEST teams in VCT right now are..? --
# Plat Chat VALORANT Ep. 274".
EPISODE_PATTERN = re.compile(r"(?:Episode|Ep\.)\s*(\d+)\s*$", re.IGNORECASE)

# A replay VOD's own title is free-text and inconsistent across leagues
# (confirmed against real titles: "FLY v C9 - PLAYOFFS 2025 LTA North Split
# 2 - W11D2 - Game 05" vs "G2 v MKOI | 2025 LEC Spring Playoffs | Grand..."
# -- different separators, different wording, same channel). Rather than
# parse that structure, search for a known league name as a substring and
# use it as the short display title, keeping the full original title as the
# description (Arnaud, 2026-07-30: "the title of lol1 should only be LTA
# North and the rest be the description"). Longer/more specific names are
# listed first so e.g. "LTA North" matches before a hypothetical bare
# "LTA" would. Not exhaustive -- extend as new replay sources surface
# leagues not covered here.
KNOWN_REPLAY_LEAGUES: dict[str, list[str]] = {
    "lol": [
        "Worlds",
        "MSI",
        "First Stand",
        "LTA North",
        "LTA South",
        "LCS",
        "LEC",
        "LCK",
        "LPL",
        "LCP",
        "CBLOL",
        "VCS",
        "PCS",
        "TCL",
        "LJL",
    ],
    "valorant": [
        "Champions",
        "VALORANT Masters",
        "Game Changers Championship",
        "Game Changers NA",
        "Game Changers EMEA",
        "Game Changers Pacific",
        "Last Chance Qualifier",
        "VCT Americas",
        "VCT EMEA",
        "VCT Pacific",
        "VCT China",
    ],
}


def _extract_replay_league(title: str, known_leagues: list[str]) -> str | None:
    title_lower = title.lower()
    for league in known_leagues:
        if league.lower() in title_lower:
            return league
    return None

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


def fetch_plat_chat_schedule() -> dict[str, Any] | None:
    """The next upcoming/live Plat Chat episode's schedule, independent of
    "now" (so it's cacheable -- see get_cached_plat_chat_schedule), or None
    if nothing's found or the fetch fails."""
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

        topic, episode = _parse_episode(video.get("title", ""))
        return {
            "video_id": video["id"],
            "topic": topic,
            "episode": episode,
            "real_start": datetime.fromtimestamp(release_timestamp, tz=timezone.utc).isoformat(),
        }
    return None


def get_cached_plat_chat_schedule(
    now: datetime, fetch: Callable[[], dict[str, Any] | None] = fetch_plat_chat_schedule
) -> dict[str, Any] | None:
    """Reuses a cached `fetch()` result from disk until it's older than
    CACHE_TTL."""
    cache = _read_cache()
    entry = cache.get("plat_chat_schedule")
    if entry is not None:
        ttl = CACHE_TTL if entry.get("schedule") is not None else NEGATIVE_CACHE_TTL
        if _is_cache_entry_fresh(entry, now, ttl):
            return entry["schedule"]

    schedule = fetch()
    cache["plat_chat_schedule"] = {"schedule": schedule, "fetched_at": now.isoformat()}
    _write_cache(cache)
    return schedule


def plat_chat_match_if_live(schedule: dict[str, Any] | None, now: datetime) -> dict[str, Any] | None:
    """None if `schedule` (from fetch/get_cached_plat_chat_schedule) isn't
    genuinely airing at `now`. Whether it's still airing is derived purely
    from its own real_start + PLAT_CHAT_DURATION_SECONDS, not YouTube's own
    live-status flag -- that flag can be stale once `schedule` came from a
    cache read hours ago, but the schedule's timing is trustworthy
    regardless of when it was fetched, same principle as trusting Riot's
    schedule timing over its own unreliable state flag elsewhere in this
    plugin. Otherwise a virtual match dict, ready for the same
    slot-assignment pipeline real Riot matches go through."""
    if schedule is None:
        return None
    real_start = datetime.fromisoformat(schedule["real_start"])
    real_end = real_start + timedelta(seconds=PLAT_CHAT_DURATION_SECONDS)
    if not (real_start <= now <= real_end):
        return None

    topic, episode = schedule["topic"], schedule["episode"]
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
        "categories": SUPPLEMENTAL_CATEGORIES,
        "is_replay": False,
        "episode_num": episode,
        "icon": _thumbnail_url(schedule["video_id"]),
    }


def fetch_replay_candidates(channel_url: str, limit: int = 20) -> list[dict[str, Any]]:
    """Recent completed uploads from a VOD channel/playlist URL, most-recent
    first. Entries still live/upcoming (no known duration yet), and entries
    shorter than MIN_REPLAY_DURATION_SECONDS (clips/highlights, not full
    match VODs), are skipped."""
    try:
        with yt_dlp.YoutubeDL({**_FLAT_LIST_OPTS, "playlistend": limit}) as ydl:
            listing = ydl.extract_info(channel_url, download=False)
    except Exception:
        logger.exception("esportsarr: failed to list replay candidates from %r", channel_url)
        return []

    candidates = []
    for entry in listing.get("entries") or []:
        duration = entry.get("duration")
        if not duration or duration < MIN_REPLAY_DURATION_SECONDS:
            continue
        candidates.append({"id": entry["id"], "title": entry.get("title", ""), "duration_seconds": duration})
    return candidates


def get_cached_replay_candidates(
    game: str,
    channel_urls: list[str],
    now: datetime,
    fetch: Callable[[str], list[dict[str, Any]]] = fetch_replay_candidates,
) -> list[dict[str, Any]]:
    """Reuses yesterday's `fetch()`-ed candidate list (combined across all
    of `channel_urls`) from disk until it's older than CACHE_TTL."""
    cache = _read_cache()
    entry = cache.get("replay_candidates", {}).get(game)
    if entry is not None:
        ttl = CACHE_TTL if entry.get("candidates") else NEGATIVE_CACHE_TTL
        if _is_cache_entry_fresh(entry, now, ttl):
            return entry["candidates"]

    candidates: list[dict[str, Any]] = []
    for channel_url in channel_urls:
        candidates.extend(fetch(channel_url))
    cache.setdefault("replay_candidates", {})[game] = {"candidates": candidates, "fetched_at": now.isoformat()}
    _write_cache(cache)
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
    # `league_name` ("Replay") is an internal identifier only, not shown --
    # "this is a rerun" is the standard <previously-shown/> tag, never a
    # category string. The guide's displayed title is a short recognized
    # league name if one is found in the VOD's own title, full details
    # (teams, round, game number) moving to the description instead; if no
    # known league is recognized, the full title is used as-is with no
    # separate description (Arnaud, 2026-07-30).
    matched_league = _extract_replay_league(candidate["title"], KNOWN_REPLAY_LEAGUES.get(game, []))
    title = matched_league or candidate["title"]
    description = candidate["title"] if matched_league else ""
    return {
        "league": league_name,
        "game": game,
        "start": now.isoformat(),
        "state": "in_progress",
        "title": title,
        "stream_platform": "youtube_vod",
        "stream_channel": candidate["id"],
        "description": description,
        "has_real_content": True,
        "best_of": None,
        "duration_seconds": candidate["duration_seconds"],
        "categories": SUPPLEMENTAL_CATEGORIES,
        "is_replay": True,
        "icon": _thumbnail_url(candidate["id"]),
    }
