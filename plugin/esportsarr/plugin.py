"""Dispatcharr plugin: consolidates per-league esports channels (Twitch,
plus YouTube for leagues like LPL) into a fixed number of generic channels
per game, switching the active stream to whichever live match currently
holds priority (see allocator.py for the policy, channel_sync.py for the
Dispatcharr-side writes).

The live 60s tick does NOT run the allocation policy itself -- it applies
whatever `plan_builder.build_weekly_plan` decided, rebuilding that plan only
once a day (see `plan_refresh_interval_hours`). The tick's own job is
narrower: look up what the stored plan says is current right now, and
reconcile it against live reality (a match ending early/getting cancelled,
Twitch stream-title verification for Game Changers). See plan_builder.py's
docstring for why this split exists.

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

from . import channel_sync, plan_builder, stream_verification

logger = logging.getLogger(__name__)

PLUGIN_KEY = "esportsarr"

# Read from plugin.json rather than duplicated as literals, so release-please
# patching plugin.json's version can't drift from what this class reports.
_PLUGIN_MANIFEST = json.loads((Path(__file__).parent / "plugin.json").read_text(encoding="utf-8"))

DEFAULT_SETTINGS = {
    "schedule_url": "",
    "poll_interval_seconds": 60,
    "plan_refresh_interval_hours": 24,
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

REQUEST_TIMEOUT_SECONDS = 10
JOB_LOCK_TTL_SECONDS = 300  # a stuck tick shouldn't block the scheduler forever
PLUGIN_STATE_DIR = f"/app/data/plugins/{PLUGIN_KEY}/.state"
PLAN_FILE_PATH = f"{PLUGIN_STATE_DIR}/weekly-plan.json"


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


def _current_occupant(history: list[tuple[datetime, dict]], now: datetime) -> dict | None:
    """What the stored plan says a slot is showing at `now` -- the last
    entry whose `claimed_at <= now`, but only while that match's own real
    end hasn't passed yet. A gap `plan_builder` left unfilled (supplemental
    content disabled, or its candidate pool ran out) correctly falls through
    to `None` here instead of showing a long-finished match forever."""
    current = None
    for claimed_at, match in history:
        if claimed_at > now:
            break
        current = match
    if current is None:
        return None
    match_end = datetime.fromisoformat(current["start"]) + channel_sync.duration_for_match(current)
    return current if now < match_end else None


def _reconcile_with_reality(
    occupant: dict | None,
    matches_by_key: dict[tuple, dict],
    now: datetime,
    stale_live_grace: timedelta,
) -> dict | None:
    """The plan was built once, up to `plan_refresh_interval_hours` ago --
    this is where the live tick corrects it against what's actually true
    right now, without re-deciding *which* match should be playing (that's
    still entirely the stored plan's call). Plat Chat/replay entries have no
    schedule.json counterpart, so they pass through untouched. A real match
    that's vanished from the schedule (cancelled) or already completed
    clearly isn't happening -- the slot goes unstreamable rather than
    keep showing something stale. A genuinely live Game Changers match still
    gets the same Twitch stream-title verification it always did."""
    if occupant is None or plan_builder.is_supplemental(occupant):
        return occupant

    key = (occupant.get("match_id"), occupant.get("league"), occupant.get("start"))
    fresh = matches_by_key.get(key)
    if fresh is None or fresh.get("state") == "completed":
        return None

    if (
        plan_builder.is_genuinely_live(fresh, now, stale_live_grace)
        and fresh.get("league") in stream_verification.LIVE_CHANNEL_CANDIDATES
    ):
        return stream_verification.verify_stream_channel(fresh, stream_verification.fetch_twitch_stream_title)
    return fresh


def _run_sync(settings: dict, force_rebuild: bool = False) -> dict:
    lock_path = _job_lock("sync")
    if not lock_path:
        return {"status": "skipped", "message": "Another sync is already running."}

    try:
        now = datetime.now(timezone.utc)
        stale_live_grace = timedelta(minutes=plan_builder.STALE_LIVE_GRACE_MINUTES)

        plan = None if force_rebuild else plan_builder.load_plan(PLAN_FILE_PATH)
        max_age = timedelta(hours=int(settings["plan_refresh_interval_hours"]))
        matches = _fetch_schedule(settings)
        if plan_builder.plan_is_stale(plan, now, max_age):
            plan = plan_builder.build_weekly_plan(matches, now, settings)
            plan_builder.save_plan(plan, PLAN_FILE_PATH)

        matches_by_key = {
            (match.get("match_id"), match.get("league"), match.get("start")): match for match in matches
        }
        projection_end = now + timedelta(days=int(settings["schedule_projection_days"]))

        results: dict[str, list[str | None] | str] = {}
        guide_entries: list[dict] = []
        for game, per_slot_history in plan["games"].items():
            try:
                occupants = [_current_occupant(history, now) for history in per_slot_history]
                assignment = [
                    _reconcile_with_reality(occupant, matches_by_key, now, stale_live_grace) for occupant in occupants
                ]
                channel_sync.apply_assignment(settings, game, assignment)
                guide_entries.extend(channel_sync.build_guide_entries(game, per_slot_history, now, projection_end))
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
        if plan.get("priority_warnings"):
            response["priority_warnings"] = plan["priority_warnings"]
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
            # Manual trigger: always rebuild the plan fresh rather than
            # trusting whatever's on disk, so it's actually useful for
            # verifying behavior against a known live match right now.
            return _run_sync(settings, force_rebuild=True)

        return {"status": "error", "message": f"Unknown action {action!r}"}

    def stop(self, context: dict | None = None):
        try:
            _stop_scheduler()
            return {"status": "ok", "message": "Scheduler stopped."}
        except Exception as exc:
            logger.exception("esportsarr: failed to stop scheduler")
            return {"status": "error", "message": str(exc)}
