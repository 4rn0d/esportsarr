"""Dispatcharr plugin: consolidates per-league esports channels (Twitch,
plus YouTube for leagues like LPL) into a fixed number of generic channels
per game, switching the active stream to whichever live match currently
holds priority (see allocator.py for the policy, channel_sync.py for the
Dispatcharr-side writes).

Dispatcharr has no periodic-task hook for plugins, so the background
scheduler thread here (self-rolled, DB-backed settings, file-based job lock,
web-process detection) mirrors the Twitcharr plugin's own pattern.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from . import channel_sync, stream_verification, supplemental_content
from .allocator import assign_slots, project_schedule

logger = logging.getLogger(__name__)

PLUGIN_KEY = "esportsarr"

# Read from plugin.json rather than duplicated as literals, so release-please
# patching plugin.json's version can't drift from what this class reports.
_PLUGIN_MANIFEST = json.loads((Path(__file__).parent / "plugin.json").read_text(encoding="utf-8"))

DEFAULT_SETTINGS = {
    "schedule_url": "",
    "poll_interval_seconds": 60,
    "slots_per_game": 3,
    "channel_group_name": "Esports Multiplex",
    "league_priority_lol_international": "Worlds,MSI,First Stand",
    "league_priority_lol_regional": "LCS,LEC,LCK,LPL",
    "league_priority_lol_qualifiers": "",
    "league_priority_valorant_international": "Champions,VALORANT Masters,Game Changers Championship",
    "league_priority_valorant_regional": "VCT Americas,VCT EMEA,VCT Pacific",
    "league_priority_valorant_qualifiers": (
        "Last Chance Qualifier Americas,Last Chance Qualifier EMEA,Last Chance Qualifier Pacific,"
        "Game Changers NA,Game Changers EMEA,Game Changers Pacific"
    ),
    # lookahead: how far ahead a slot can be reserved at all. priority: how
    # close to start it actually competes for a contested slot (see allocator.py).
    # Wide enough to cover typical multi-hour gaps between back-to-back
    # matches on a shared regional channel (e.g. VCT then Game Changers).
    "reservation_lookahead_minutes": 180,
    "reservation_priority_minutes": 120,
    "schedule_projection_days": 7,
    # StreamProfile names are settings, not hardcoded, since they're external
    # identifiers this plugin doesn't own. YouTube's default is an unverified
    # guess (no equivalent to Twitcharr exists for it yet) -- see plugin/README.md.
    "twitch_stream_profile_name": "Twitcharr (ad-free, low-latency)",
    "youtube_stream_profile_name": "Twitcharr (ad-free, low-latency)",
    # Off by default: needs yt-dlp installed in the Dispatcharr environment,
    # a new dependency this plugin didn't previously need -- see plugin/README.md.
    "enable_supplemental_content": False,
    "replay_channels_lol": "https://www.youtube.com/@lolesportsvods/videos",
    "replay_channels_valorant": (
        "https://www.youtube.com/@VCTPacific/videos,"
        "https://www.youtube.com/channel/UCifCesg-EUkjKyQedaB3hRg/videos,"
        "https://www.youtube.com/channel/UCp6n8d8Y8r3MwKNw_MMaouQ/videos"
    ),
}
REPLAY_CHANNELS_SETTING_BY_GAME = {
    "lol": "replay_channels_lol",
    "valorant": "replay_channels_valorant",
}

# Priority is tiered (International > Regional > Qualifiers) across separate
# settings fields rather than one long comma list, so growing either list
# stays readable in the settings UI. Order within a tier still matters; order
# across tiers is fixed by this key order, concatenated by _combined_priority.
GAME_PRIORITY_TIER_KEYS = {
    "lol": ["league_priority_lol_international", "league_priority_lol_regional", "league_priority_lol_qualifiers"],
    "valorant": [
        "league_priority_valorant_international",
        "league_priority_valorant_regional",
        "league_priority_valorant_qualifiers",
    ],
}

REQUEST_TIMEOUT_SECONDS = 10
JOB_LOCK_TTL_SECONDS = 300  # a stuck tick shouldn't block the scheduler forever
PLUGIN_STATE_DIR = f"/app/data/plugins/{PLUGIN_KEY}/.state"

# Caps how far a delayed "unstarted" match's start can slip into the past
# and still count as a reservation candidate.
RESERVATION_GRACE_MINUTES = 30

# Riot's live-state flag isn't reliable for every league tier (Game Changers
# events have been observed staying "unstarted" well past their real start
# while actually airing). An "unstarted" match already past its start is
# treated as live until this long after start, rather than trusting the flag.
STALE_LIVE_GRACE_MINUTES = 720

_last_assignment: dict[str, list[dict | None]] = {}  # in-memory, reset on process restart
_last_channel_by_slot: dict[str, dict[int, str]] = {}  # per game, survives a slot sitting idle between two matches


def _merge_defaults(settings: dict) -> dict:
    merged = dict(DEFAULT_SETTINGS)
    merged.update({key: value for key, value in (settings or {}).items() if value not in (None, "")})
    return merged


def _load_settings() -> dict:
    try:
        from apps.plugins.models import PluginConfig

        config = PluginConfig.objects.filter(key=PLUGIN_KEY).first()
        if config and isinstance(config.settings, dict):
            return _merge_defaults(config.settings)
    except Exception:
        logger.exception("esportsarr: failed to load settings from DB")
    return _merge_defaults({})


def _parse_priority(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _combined_priority(settings: dict, tier_keys: list[str]) -> list[str]:
    combined: list[str] = []
    for key in tier_keys:
        combined.extend(_parse_priority(settings[key]))
    return combined


def _unranked_live_leagues(matches: list[dict[str, Any]], game: str, priority: list[str]) -> list[str]:
    """Leagues seen live for `game` in this fetch that aren't in any priority
    tier, so they're silently sorting last -- either a forgotten entry or a
    typo elsewhere in the list (e.g. 'Game Changers Americas' instead of the
    real 'Game Changers NA')."""
    seen = {match["league"] for match in matches if match.get("game") == game and match.get("league")}
    return sorted(seen - set(priority))


def _job_lock(name: str, ttl_seconds: int = JOB_LOCK_TTL_SECONDS) -> str:
    os.makedirs(PLUGIN_STATE_DIR, exist_ok=True)
    path = os.path.join(PLUGIN_STATE_DIR, f".{name}.lock")
    try:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) > ttl_seconds:
            os.unlink(path)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()} {int(time.time())}\n")
        return path
    except FileExistsError:
        return ""


def _release_job_lock(path: str) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def _fetch_schedule(settings: dict) -> list[dict[str, Any]]:
    schedule_url = settings["schedule_url"]
    if not schedule_url:
        logger.warning("esportsarr: schedule_url is not configured, skipping tick")
        return []
    response = requests.get(schedule_url, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()["matches"]


def _classify_matches(
    matches: list[dict[str, Any]],
    now: datetime,
    wide_lookahead: timedelta,
    narrow_lookahead: timedelta,
    grace: timedelta,
    stale_live_grace: timedelta,
    projection_end: datetime,
    history_window: timedelta,
) -> tuple[dict[str, list[dict]], dict[str, list[dict]], dict[str, list[dict]], dict[str, list[dict]]]:
    """Buckets matches into live/near-upcoming/far-upcoming/projectable per game."""
    live_by_game: dict[str, list[dict]] = {}
    upcoming_by_game: dict[str, list[dict]] = {}  # "near": competes for contested slots
    far_upcoming_by_game: dict[str, list[dict]] = {}  # "far": preview-only, never displaces
    projectable_by_game: dict[str, list[dict]] = {}  # broader set fed to the week-ahead projection
    for match in matches:
        if not match.get("stream_platform") or not match.get("stream_channel"):
            continue

        state = match.get("state")
        start = datetime.fromisoformat(match["start"]) if match.get("start") else None
        already_live = state == "unstarted" and start is not None and start <= now <= start + stale_live_grace

        if state == "in_progress" or already_live:
            live_by_game.setdefault(match["game"], []).append(match)
            projectable_by_game.setdefault(match["game"], []).append(match)
        elif state == "unstarted":
            if now - grace <= start <= now + narrow_lookahead:
                upcoming_by_game.setdefault(match["game"], []).append(match)
            elif now - grace <= start <= now + wide_lookahead:
                far_upcoming_by_game.setdefault(match["game"], []).append(match)
            if now - grace <= start < projection_end:
                projectable_by_game.setdefault(match["game"], []).append(match)
        elif state == "completed":
            # Otherwise the guide has zero record of what actually aired in a
            # slot once its match ends, so the historical portion of the
            # guide degrades to a giant "No Match Scheduled" the moment
            # nothing's currently live there -- confirmed as a real bug,
            # 2026-07-29 -- even though real matches did air there.
            if now - history_window <= start < projection_end:
                projectable_by_game.setdefault(match["game"], []).append(match)

    return live_by_game, upcoming_by_game, far_upcoming_by_game, projectable_by_game


def _fill_supplemental_content(
    game: str,
    assignment: list[dict | None],
    now: datetime,
    settings: dict,
    fetch_plat_chat: Callable[[datetime], dict | None] = supplemental_content.fetch_plat_chat_live_info,
    fetch_replays: Callable[[str], list[dict]] = supplemental_content.fetch_replay_candidates,
) -> list[dict | None]:
    """Fills whatever `assignment` slots are still empty after real
    esports-match assignment with Plat Chat VALORANT (if genuinely live) or
    an official match replay -- never competes with or bumps a real match,
    since it only ever touches a slot `assign_slots` already left `None`."""
    if not settings.get("enable_supplemental_content"):
        return assignment

    filled = list(assignment)
    remaining_slots = [i for i, occupant in enumerate(filled) if occupant is None]
    if not remaining_slots:
        return filled

    if game == "valorant":
        plat_chat = fetch_plat_chat(now)
        if plat_chat is not None:
            filled[remaining_slots.pop(0)] = plat_chat

    if remaining_slots:
        channel_setting = REPLAY_CHANNELS_SETTING_BY_GAME.get(game)
        channel_urls = _parse_priority(settings.get(channel_setting, "")) if channel_setting else []
        candidates: list[dict] = []
        for channel_url in channel_urls:
            candidates.extend(fetch_replays(channel_url))
        for slot_index in remaining_slots:
            candidate = supplemental_content.pick_replay(candidates, seed=f"{now.date().isoformat()}-{game}-{slot_index}")
            if candidate is not None:
                filled[slot_index] = supplemental_content.build_replay_match(game, "Replay", candidate, now)

    return filled


def _run_sync(settings: dict) -> dict:
    lock_path = _job_lock("sync")
    if not lock_path:
        return {"status": "skipped", "message": "Another sync is already running."}

    try:
        matches = _fetch_schedule(settings)
        # Only genuinely live matches in a league known to sometimes split
        # concurrent games onto a secondary channel (see
        # stream_verification.LIVE_CHANNEL_CANDIDATES) are worth a live
        # Twitch title check -- nothing to verify against before a match
        # actually starts airing.
        matches = [
            stream_verification.verify_stream_channel(match, stream_verification.fetch_twitch_stream_title)
            if match.get("state") == "in_progress" and match.get("league") in stream_verification.LIVE_CHANNEL_CANDIDATES
            else match
            for match in matches
        ]
        now = datetime.now(timezone.utc)
        wide_lookahead = timedelta(minutes=int(settings["reservation_lookahead_minutes"]))
        narrow_lookahead = timedelta(minutes=int(settings["reservation_priority_minutes"]))
        grace = timedelta(minutes=RESERVATION_GRACE_MINUTES)
        stale_live_grace = timedelta(minutes=STALE_LIVE_GRACE_MINUTES)
        projection_end = now + timedelta(days=int(settings["schedule_projection_days"]))
        # Same window the guide itself renders before "now" (channel_sync.py)
        # -- no point knowing about completed matches further back than the
        # guide would ever display them.
        history_window = timedelta(hours=channel_sync.GUIDE_LOOKBACK_HOURS)

        live_by_game, upcoming_by_game, far_upcoming_by_game, projectable_by_game = _classify_matches(
            matches, now, wide_lookahead, narrow_lookahead, grace, stale_live_grace, projection_end, history_window
        )

        results: dict[str, list[str | None] | str] = {}
        priority_warnings: dict[str, list[str]] = {}
        guide_entries: list[dict] = []
        for game, tier_keys in GAME_PRIORITY_TIER_KEYS.items():
            try:
                priority = _combined_priority(settings, tier_keys)
                unranked_live = _unranked_live_leagues(matches, game, priority)
                if unranked_live:
                    priority_warnings[game] = unranked_live
                slots = int(settings["slots_per_game"])
                previous = _last_assignment.get(game)
                previous_channel_by_slot = _last_channel_by_slot.get(game)

                assignment, _reserved_for, _overflow, channel_by_slot = assign_slots(
                    live_matches=live_by_game.get(game, []),
                    slots=slots,
                    league_priority=priority,
                    previous_assignment=previous,
                    upcoming_matches=upcoming_by_game.get(game, []),
                    far_upcoming_matches=far_upcoming_by_game.get(game, []),
                    last_channel_by_slot=previous_channel_by_slot,
                )

                augmented_assignment = _fill_supplemental_content(game, assignment, now, settings)
                for slot_index, occupant in enumerate(augmented_assignment):
                    if assignment[slot_index] is None and occupant is not None:
                        # Newly filled by supplemental content this tick --
                        # add its own interval so project_schedule's future
                        # ticks keep it sticky until its own real end.
                        channel_by_slot[slot_index] = occupant.get("stream_channel")
                        projectable_by_game.setdefault(game, []).append(occupant)
                assignment = augmented_assignment

                channel_sync.apply_assignment(settings, game, assignment)
                _last_assignment[game] = assignment
                _last_channel_by_slot[game] = channel_by_slot

                projected_by_slot = project_schedule(
                    matches=projectable_by_game.get(game, []),
                    slots=slots,
                    league_priority=priority,
                    duration_fn=channel_sync.duration_for_match,
                    now=now,
                    initial_assignment=assignment,
                    narrow_lookahead=narrow_lookahead,
                    wide_lookahead=wide_lookahead,
                    initial_channel_by_slot=channel_by_slot,
                )
                guide_entries.extend(channel_sync.build_guide_entries(game, projected_by_slot, now, projection_end))

                results[game] = [match["title"] if match else None for match in assignment]
            except Exception as exc:
                logger.exception("esportsarr: sync failed for game %r", game)
                results[game] = f"error: {exc}"

        # Skip the write if every game failed above, an empty-but-valid
        # XMLTV write would wipe the last good guide.
        if guide_entries:
            try:
                channel_sync.write_guide(guide_entries)
            except Exception:
                logger.exception("esportsarr: failed to write/refresh the guide")

        overall_status = "error" if any(isinstance(value, str) for value in results.values()) else "ok"
        response = {"status": overall_status, "assignment": results}
        if priority_warnings:
            response["priority_warnings"] = priority_warnings
        return response
    except Exception as exc:
        logger.exception("esportsarr: sync failed")
        return {"status": "error", "message": str(exc)}
    finally:
        _release_job_lock(lock_path)


# --- background scheduler ---------------------------------------------------

_scheduler_lock = threading.RLock()
_scheduler_stop = threading.Event()
_scheduler_thread: threading.Thread | None = None


def _is_web_server_process() -> bool:
    """True inside Dispatcharr's web workers; the scheduler should only run in the Celery worker."""
    if "uwsgi" in sys.modules:
        return True
    try:
        import uwsgi  # noqa: F401

        return True
    except ImportError:
        pass
    return any(server in (arg or "").lower() for arg in sys.argv[:2] for server in ("daphne", "gunicorn"))


def _scheduler_loop() -> None:
    own_module = __name__
    logger.info("esportsarr: self-scheduler started")
    while not _scheduler_stop.is_set():
        if own_module not in sys.modules:
            logger.info("esportsarr: exiting, module %s was unloaded", own_module)
            return
        try:
            from django.db import close_old_connections

            close_old_connections()
            _run_sync(_load_settings())
        except ImportError as exc:
            logger.info("esportsarr: exiting after plugin reload: %s", exc)
            return
        except Exception:
            logger.exception("esportsarr: scheduler tick failed")
        finally:
            try:
                from django.db import close_old_connections

                close_old_connections()
            except Exception:
                pass

        poll_interval = int(_load_settings().get("poll_interval_seconds", DEFAULT_SETTINGS["poll_interval_seconds"]))
        _scheduler_stop.wait(poll_interval)


def _start_scheduler() -> bool:
    global _scheduler_thread
    if _is_web_server_process():
        logger.info(
            "esportsarr: not starting scheduler in web-server process; "
            "it runs in the Celery worker process instead"
        )
        return False
    with _scheduler_lock:
        if _scheduler_thread and _scheduler_thread.is_alive():
            return False
        _scheduler_stop.clear()
        _scheduler_thread = threading.Thread(target=_scheduler_loop, name="EsportsarrThread", daemon=True)
        _scheduler_thread.start()
        return True


def _stop_scheduler() -> bool:
    global _scheduler_thread
    with _scheduler_lock:
        if not _scheduler_thread or not _scheduler_thread.is_alive():
            return False
        _scheduler_stop.set()
        if _scheduler_thread is not threading.current_thread():
            _scheduler_thread.join(timeout=5)
        return True


class Plugin:
    name = _PLUGIN_MANIFEST["name"]
    version = _PLUGIN_MANIFEST["version"]
    description = _PLUGIN_MANIFEST["description"]
    author = _PLUGIN_MANIFEST["author"]

    def __init__(self):
        try:
            _start_scheduler()
        except Exception:
            logger.exception("esportsarr: could not start scheduler")

    def run(self, action: str, params: dict, context: dict):
        settings = _merge_defaults(context.get("settings") or {})

        if action == "create_channels":
            try:
                return channel_sync.create_channels(settings)
            except Exception as exc:
                logger.exception("esportsarr: create_channels failed")
                return {"status": "error", "message": str(exc)}

        if action == "sync_now":
            return _run_sync(settings)

        return {"status": "error", "message": f"Unknown action {action!r}"}

    def stop(self, context: dict | None = None):
        try:
            _stop_scheduler()
            return {"status": "ok", "message": "Scheduler stopped."}
        except Exception as exc:
            logger.exception("esportsarr: failed to stop scheduler")
            return {"status": "error", "message": str(exc)}
