"""Dispatcharr plugin: consolidates per-league esports Twitch channels into a
fixed number of generic channels per game, switching the active stream to
whichever live match currently holds priority (see allocator.py for the
policy, channel_sync.py for the Dispatcharr-side writes).

Dispatcharr's plugin framework has no native periodic-task hook (confirmed
against https://github.com/Dispatcharr/Dispatcharr/blob/main/Plugins.md,
2026-07-27) — actions only run in response to a UI button click. The
background scheduler thread pattern here (self-rolled thread, DB-backed
settings loading, file-based job lock, web-process detection) mirrors the
Twitcharr plugin (github.com/eliasbruno124-dev/Twitcharr), which solves the
same problem.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from . import channel_sync
from .allocator import assign_slots

logger = logging.getLogger(__name__)

PLUGIN_KEY = "esportsarr"

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
    "reservation_lookahead_minutes": 45,
}

GAME_PRIORITY_SETTING_KEYS = {
    "lol": "league_priority_lol",
    "valorant": "league_priority_valorant",
}

REQUEST_TIMEOUT_SECONDS = 10
JOB_LOCK_TTL_SECONDS = 300  # a stuck tick shouldn't block the scheduler forever
PLUGIN_STATE_DIR = f"/app/data/plugins/{PLUGIN_KEY}/.state"

# Safety net, not a user setting: an "unstarted" match whose start time has
# slipped into the past (delayed broadcast) would otherwise satisfy
# `start <= now + lookahead` forever and reserve a slot indefinitely.
RESERVATION_GRACE_MINUTES = 30

# In-memory only: which match currently occupies each slot, per game. Reset
# on process restart — worst case is one tick where a match that was live
# before the restart gets treated as new (no functional difference, since a
# newly-seen match just gets ranked and assigned like any other).
_last_assignment: dict[str, list[dict | None]] = {}


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
        lookahead = timedelta(minutes=int(settings["reservation_lookahead_minutes"]))
        grace = timedelta(minutes=RESERVATION_GRACE_MINUTES)

        live_by_game: dict[str, list[dict]] = {}
        upcoming_by_game: dict[str, list[dict]] = {}
        for match in matches:
            state = match.get("state")
            if state == "in_progress":
                live_by_game.setdefault(match["game"], []).append(match)
            elif state == "unstarted":
                start = datetime.fromisoformat(match["start"])
                if now - grace <= start <= now + lookahead:
                    upcoming_by_game.setdefault(match["game"], []).append(match)

        results: dict[str, list[str | None]] = {}
        for game, priority_key in GAME_PRIORITY_SETTING_KEYS.items():
            priority = _parse_priority(settings[priority_key])
            slots = int(settings["slots_per_game"])
            previous = _last_assignment.get(game)

            assignment = assign_slots(
                live_matches=live_by_game.get(game, []),
                slots=slots,
                league_priority=priority,
                previous_assignment=previous,
                upcoming_matches=upcoming_by_game.get(game, []),
            )
            channel_sync.apply_assignment(settings, game, assignment)
            _last_assignment[game] = assignment
            results[game] = [match["title"] if match else None for match in assignment]

        return {"status": "ok", "assignment": results}
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
    """True inside Dispatcharr's uWSGI/Daphne/Gunicorn web workers — the
    scheduler should only run in the Celery worker process, same as Twitcharr."""
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
    name = "Esportsarr"
    version = "0.1.0"
    description = (
        "Consolidates per-league esports Twitch channels into a fixed number of "
        "generic channels per game, switching the active stream to whichever "
        "live match currently holds priority."
    )
    author = "4rn0d"

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
