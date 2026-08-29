"""Builds and persists ONE authoritative week-ahead schedule ("the plan"),
so the live 60s sync tick applies a stored decision instead of re-running
the full allocation policy from scratch every tick.

Before this module existed, `plugin.py`'s live tick called
`allocator.assign_slots` for "right now" and separately called
`allocator.project_schedule` to preview the week ahead for the guide --
two independent simulations of the same policy that could disagree (the
root cause behind several bugs: a guide showing a stale league while the
live stream was actually correct, supplemental-content picks only ever
decided the instant a tick happened to reach an empty slot rather than
something inspectable in advance). Now `build_weekly_plan` is the ONE place
that runs `assign_slots`/`project_schedule`, once a day; the live tick just
looks up what the stored plan says is current and reconciles it against
live reality (state changes, Twitch stream-title verification), never
re-decides allocation itself.

No Django dependency -- fully offline/testable, same as allocator.py and
supplemental_content.py.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Callable

from . import channel_sync, stream_verification, supplemental_content
from .allocator import assign_slots, project_schedule

logger = logging.getLogger(__name__)

MatchDict = dict[str, Any]
SlotHistory = list[tuple[datetime, MatchDict]]
WeeklyPlan = dict[str, Any]

# Caps how far a delayed "unstarted" match's start can slip into the past
# and still count as a reservation candidate.
RESERVATION_GRACE_MINUTES = 30

# Riot's live-state flag isn't reliable for every league tier (Game Changers
# events have been observed staying "unstarted" well past their real start
# while actually airing). An "unstarted" match already past its start is
# treated as live until this long after start, rather than trusting the flag.
STALE_LIVE_GRACE_MINUTES = 720

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
REPLAY_CHANNELS_SETTING_BY_GAME = {
    "lol": "replay_channels_lol",
    "valorant": "replay_channels_valorant",
}

PLAN_FILE_PATH = "/app/data/plugins/esportsarr/.state/weekly-plan.json"


def _parse_priority(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _combined_priority(settings: dict, tier_keys: list[str]) -> list[str]:
    combined: list[str] = []
    for key in tier_keys:
        combined.extend(_parse_priority(settings[key]))
    return combined


def _unranked_live_leagues(matches: list[MatchDict], game: str, priority: list[str]) -> list[str]:
    """Leagues seen live for `game` in this fetch that aren't in any priority
    tier, so they're silently sorting last -- either a forgotten entry or a
    typo elsewhere in the list (e.g. 'Game Changers Americas' instead of the
    real 'Game Changers NA')."""
    seen = {match["league"] for match in matches if match.get("game") == game and match.get("league")}
    return sorted(seen - set(priority))


def is_genuinely_live(match: MatchDict, now: datetime, stale_live_grace: timedelta) -> bool:
    """True if `state` says so, or if it's stuck `"unstarted"` past its own
    real start within `stale_live_grace` -- Riot's `state` flag isn't
    reliable for every league tier (Game Changers especially). Shared by
    `_classify_matches` (plan-build time) and `plugin.py`'s live-tick
    reconciliation so both agree."""
    state = match.get("state")
    if state == "in_progress":
        return True
    if state != "unstarted" or not match.get("start"):
        return False
    start = datetime.fromisoformat(match["start"])
    return start <= now <= start + stale_live_grace


def is_supplemental(match: MatchDict) -> bool:
    """True for a Plat Chat or replay entry `plan_builder` synthesized
    itself -- these have no counterpart in the fetched schedule, so the live
    tick must never try to reconcile them against it."""
    return match.get("is_replay") or match.get("league") == supplemental_content.PLAT_CHAT_LEAGUE


def _classify_matches(
    matches: list[MatchDict],
    now: datetime,
    wide_lookahead: timedelta,
    narrow_lookahead: timedelta,
    grace: timedelta,
    stale_live_grace: timedelta,
    projection_end: datetime,
    history_window: timedelta,
) -> tuple[dict[str, list[MatchDict]], dict[str, list[MatchDict]], dict[str, list[MatchDict]], dict[str, list[MatchDict]]]:
    """Buckets matches into live/near-upcoming/far-upcoming/projectable per game."""
    live_by_game: dict[str, list[MatchDict]] = {}
    upcoming_by_game: dict[str, list[MatchDict]] = {}  # "near": competes for contested slots
    far_upcoming_by_game: dict[str, list[MatchDict]] = {}  # "far": preview-only, never displaces
    projectable_by_game: dict[str, list[MatchDict]] = {}  # broader set fed to the week-ahead projection
    for match in matches:
        if not match.get("stream_platform") or not match.get("stream_channel"):
            continue

        state = match.get("state")
        start = datetime.fromisoformat(match["start"]) if match.get("start") else None

        if is_genuinely_live(match, now, stale_live_grace):
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
            if now - history_window <= start < projection_end:
                projectable_by_game.setdefault(match["game"], []).append(match)

    return live_by_game, upcoming_by_game, far_upcoming_by_game, projectable_by_game


def _find_gaps(history: SlotHistory, now: datetime, projection_end: datetime) -> list[tuple[datetime, datetime]]:
    """Stretches of `[now, projection_end)` not already covered by a real
    match in `history`, in chronological order."""
    gaps: list[tuple[datetime, datetime]] = []
    cursor = now
    for claimed_at, match in history:
        if claimed_at > cursor:
            gaps.append((cursor, claimed_at))
        match_end = datetime.fromisoformat(match["start"]) + channel_sync.duration_for_match(match)
        cursor = max(cursor, match_end)
    if cursor < projection_end:
        gaps.append((cursor, projection_end))
    return gaps


def _fill_game_projection_gaps(
    game: str,
    projected_by_slot: list[SlotHistory],
    now: datetime,
    projection_end: datetime,
    settings: dict,
    supplemental_data: dict | None = None,
    get_plat_chat_schedule: Callable[[datetime], dict | None] = supplemental_content.get_cached_plat_chat_schedule,
    get_replay_candidates: Callable[[str, list[str], datetime], list[dict]] = supplemental_content.get_cached_replay_candidates,
) -> list[SlotHistory]:
    """Resolves every idle stretch across the whole projection window to a
    concrete Plat Chat/replay pick up front, instead of leaving it to be
    decided live the instant a tick happens to reach an empty slot. Plat Chat
    is placed at most once across the whole plan (it's a single show, not
    one instance per gap); replay picks stay deterministic per calendar day
    via `pick_replay`'s seed, with a same-day cross-slot `used_ids` set so
    two slots never show the identical VOD at the same time (the same
    protection the old per-tick `_fill_supplemental_content` had, generalized
    across the whole week instead of just "now")."""
    supplemental_enabled = supplemental_data is not None or settings.get("enable_supplemental_content")
    if not supplemental_enabled:
        return projected_by_slot

    if supplemental_data is not None:
        schedule = supplemental_data.get("plat_chat_schedule") if game == "valorant" else None
        candidates = supplemental_data.get("replay_candidates", {}).get(game, [])
    else:
        schedule = get_plat_chat_schedule(now) if game == "valorant" else None
        channel_setting = REPLAY_CHANNELS_SETTING_BY_GAME.get(game)
        channel_urls = _parse_priority(settings.get(channel_setting, "")) if channel_setting else []
        candidates = get_replay_candidates(game, channel_urls, now)

    plat_chat_window: tuple[datetime, datetime] | None = None
    if schedule is not None:
        real_start = datetime.fromisoformat(schedule["real_start"])
        real_end = real_start + timedelta(seconds=supplemental_content.PLAT_CHAT_DURATION_SECONDS)
        plat_chat_window = (real_start, real_end)

    plat_chat_placed = False
    used_replay_ids_by_date: dict[str, set[str]] = {}

    filled_by_slot: list[SlotHistory] = []
    for slot_index, history in enumerate(projected_by_slot):
        fillers: list[tuple[datetime, MatchDict]] = []
        for gap_start, gap_end in _find_gaps(history, now, projection_end):
            cursor = gap_start
            iteration = 0
            while cursor < gap_end:
                if not plat_chat_placed and plat_chat_window and plat_chat_window[0] < gap_end and plat_chat_window[1] > cursor:
                    real_start, real_end = plat_chat_window
                    match = supplemental_content.plat_chat_match_if_live(schedule, real_start)
                    fillers.append((max(cursor, real_start), match))
                    plat_chat_placed = True
                    cursor = real_end
                    continue

                day_key = cursor.date().isoformat()
                used_ids = used_replay_ids_by_date.setdefault(day_key, set())
                available = [c for c in candidates if c["id"] not in used_ids]
                candidate = supplemental_content.pick_replay(available, seed=f"{day_key}-{game}-{slot_index}-{iteration}")
                if candidate is None:
                    break
                used_ids.add(candidate["id"])
                fillers.append((cursor, supplemental_content.build_replay_match(game, "Replay", candidate, cursor)))
                cursor += timedelta(seconds=candidate["duration_seconds"])
                iteration += 1

        filled_by_slot.append(sorted(history + fillers, key=lambda pair: pair[0]))
    return filled_by_slot


def build_weekly_plan(
    matches: list[MatchDict], now: datetime, settings: dict, supplemental_data: dict | None = None
) -> WeeklyPlan:
    """The ONE place `assign_slots`/`project_schedule` get called from. Runs
    once a day (see plugin.py's staleness check), not every 60s tick."""
    wide_lookahead = timedelta(minutes=int(settings["reservation_lookahead_minutes"]))
    narrow_lookahead = timedelta(minutes=int(settings["reservation_priority_minutes"]))
    grace = timedelta(minutes=RESERVATION_GRACE_MINUTES)
    stale_live_grace = timedelta(minutes=STALE_LIVE_GRACE_MINUTES)
    projection_end = now + timedelta(days=int(settings["schedule_projection_days"]))
    history_window = timedelta(hours=channel_sync.GUIDE_LOOKBACK_HOURS)

    matches = [
        stream_verification.verify_stream_channel(match, stream_verification.fetch_twitch_stream_title)
        if is_genuinely_live(match, now, stale_live_grace) and match.get("league") in stream_verification.LIVE_CHANNEL_CANDIDATES
        else match
        for match in matches
    ]

    live_by_game, upcoming_by_game, far_upcoming_by_game, projectable_by_game = _classify_matches(
        matches, now, wide_lookahead, narrow_lookahead, grace, stale_live_grace, projection_end, history_window
    )

    games: dict[str, list[SlotHistory]] = {}
    priority_warnings: dict[str, list[str]] = {}

    for game, tier_keys in GAME_PRIORITY_TIER_KEYS.items():
        priority = _combined_priority(settings, tier_keys)
        unranked_live = _unranked_live_leagues(matches, game, priority)
        if unranked_live:
            priority_warnings[game] = unranked_live
        slots = int(settings["slots_per_game"])

        assignment, _reserved_for, _overflow, channel_by_slot = assign_slots(
            live_matches=live_by_game.get(game, []),
            slots=slots,
            league_priority=priority,
            previous_assignment=None,
            upcoming_matches=upcoming_by_game.get(game, []),
            far_upcoming_matches=far_upcoming_by_game.get(game, []),
            last_channel_by_slot=None,
        )

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

        games[game] = _fill_game_projection_gaps(game, projected_by_slot, now, projection_end, settings, supplemental_data)

    return {"built_at": now.isoformat(), "priority_warnings": priority_warnings, "games": games}


def plan_is_stale(plan: WeeklyPlan | None, now: datetime, max_age: timedelta) -> bool:
    if plan is None:
        return True
    return now - datetime.fromisoformat(plan["built_at"]) >= max_age


def serialize_plan(plan: WeeklyPlan) -> dict:
    return {
        "built_at": plan["built_at"],
        "priority_warnings": plan["priority_warnings"],
        "games": {
            game: [[[claimed_at.isoformat(), match] for claimed_at, match in history] for history in per_slot]
            for game, per_slot in plan["games"].items()
        },
    }


def deserialize_plan(data: dict) -> WeeklyPlan:
    return {
        "built_at": data["built_at"],
        "priority_warnings": data.get("priority_warnings", {}),
        "games": {
            game: [[(datetime.fromisoformat(claimed_at), match) for claimed_at, match in history] for history in per_slot]
            for game, per_slot in data["games"].items()
        },
    }


def save_plan(plan: WeeklyPlan, path: str = PLAN_FILE_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(serialize_plan(plan), f)


def load_plan(path: str = PLAN_FILE_PATH) -> WeeklyPlan | None:
    try:
        with open(path, encoding="utf-8") as f:
            return deserialize_plan(json.load(f))
    except (OSError, ValueError, KeyError):
        return None
