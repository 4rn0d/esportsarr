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
from typing import Any

import requests

from . import channel_sync
from .allocator import assign_slots, project_schedule

logger = logging.getLogger(__name__)

PLUGIN_KEY = "esportsarr"

# Read from plugin.json rather than duplicated as literals, so release-please
# patching plugin.json's version can't drift from what this class reports.
_PLUGIN_MANIFEST = json.loads((Path(__file__).parent / "plugin.json").read_text(encoding="utf-8"))

DEFAULT_SETTINGS = {
    "schedule_url": "",
    "poll_interval_seconds": 60,
    "slots_per_game": 2,
    "channel_group_name": "Esports Multiplex",
    "league_priority_lol": "Worlds,MSI,First Stand,LCS,LEC,LCK,LPL",
    "league_priority_valorant": (
        "Champions,VALORANT Masters,Game Changers Championship,"
        "VCT Americas,VCT EMEA,VCT Pacific,"
        "Game Changers NA,Game Changers EMEA,Game Changers Pacific"
    ),
    # Twitch broadcasts typically go live ~1h before the official match time.
    # lookahead: how far ahead a slot can be reserved at all. priority: how
    # close to start it actually competes for a contested slot (see allocator.py).
    "reservation_lookahead_minutes": 60,
    "reservation_priority_minutes": 30,
    "schedule_projection_days": 7,
    # StreamProfile names are settings, not hardcoded, since they're external
    # identifiers this plugin doesn't own. YouTube's default is an unverified
    # guess (no equivalent to Twitcharr exists for it yet) -- see plugin/README.md.
    "twitch_stream_profile_name": "Twitcharr (ad-free, low-latency)",
    "youtube_stream_profile_name": "Twitcharr (ad-free, low-latency)",
}

GAME_PRIORITY_SETTING_KEYS = {
    "lol": "league_priority_lol",
    "valorant": "league_priority_valorant",
}

REQUEST_TIMEOUT_SECONDS = 10
JOB_LOCK_TTL_SECONDS = 300  # a stuck tick shouldn't block the scheduler forever
PLUGIN_STATE_DIR = f"/app/data/plugins/{PLUGIN_KEY}/.state"

# Caps how far a delayed "unstarted" match's start can slip into the past
# and still count as a reservation candidate.
RESERVATION_GRACE_MINUTES = 30

_last_assignment: dict[str, list[dict | None]] = {}  # in-memory, reset on process restart


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


def _run_sync(settings: dict) -> dict:
    lock_path = _job_lock("sync")
    if not lock_path:
        return {"status": "skipped", "message": "Another sync is already running."}

    try:
        matches = _fetch_schedule(settings)
        now = datetime.now(timezone.utc)
        wide_lookahead = timedelta(minutes=int(settings["reservation_lookahead_minutes"]))
        narrow_lookahead = timedelta(minutes=int(settings["reservation_priority_minutes"]))
        grace = timedelta(minutes=RESERVATION_GRACE_MINUTES)
        projection_end = now + timedelta(days=int(settings["schedule_projection_days"]))

        live_by_game: dict[str, list[dict]] = {}
        upcoming_by_game: dict[str, list[dict]] = {}  # "near": competes for contested slots
        far_upcoming_by_game: dict[str, list[dict]] = {}  # "far": preview-only, never displaces
        projectable_by_game: dict[str, list[dict]] = {}  # broader set fed to the week-ahead projection
        for match in matches:
            if not match.get("stream_platform") or not match.get("stream_channel"):
                continue

            state = match.get("state")
            if state == "in_progress":
                live_by_game.setdefault(match["game"], []).append(match)
                projectable_by_game.setdefault(match["game"], []).append(match)
            elif state == "unstarted":
                start = datetime.fromisoformat(match["start"])
                if now - grace <= start <= now + narrow_lookahead:
                    upcoming_by_game.setdefault(match["game"], []).append(match)
                elif start <= now + wide_lookahead:
                    far_upcoming_by_game.setdefault(match["game"], []).append(match)
                if now - grace <= start < projection_end:
                    projectable_by_game.setdefault(match["game"], []).append(match)

        results: dict[str, list[str | None] | str] = {}
        guide_entries: list[dict] = []
        for game, priority_key in GAME_PRIORITY_SETTING_KEYS.items():
            try:
                priority = _parse_priority(settings[priority_key])
                slots = int(settings["slots_per_game"])
                previous = _last_assignment.get(game)

                assignment, _reserved_for, _overflow = assign_slots(
                    live_matches=live_by_game.get(game, []),
                    slots=slots,
                    league_priority=priority,
                    previous_assignment=previous,
                    upcoming_matches=upcoming_by_game.get(game, []),
                    far_upcoming_matches=far_upcoming_by_game.get(game, []),
                )
                channel_sync.apply_assignment(settings, game, assignment)
                _last_assignment[game] = assignment

                projected_by_slot = project_schedule(
                    matches=projectable_by_game.get(game, []),
                    slots=slots,
                    league_priority=priority,
                    duration=channel_sync.PROGRAMME_DURATION,
                    initial_assignment=assignment,
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
        return {"status": overall_status, "assignment": results}
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
