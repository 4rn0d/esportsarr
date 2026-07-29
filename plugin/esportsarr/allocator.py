"""Pure slot-assignment logic, no Django dependency (see tests/test_allocator.py)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

MatchDict = dict[str, Any]


def _match_key(match: MatchDict) -> tuple:
    return (match.get("league"), match.get("start"))


def _priority_rank(match: MatchDict, league_priority: list[str]) -> int:
    league = match.get("league")
    try:
        return league_priority.index(league)
    except ValueError:
        return len(league_priority)  # unranked leagues sort last


def _dedupe_candidates(
    matches: list[MatchDict],
    occupied_keys: set[tuple],
    live_by_key: dict[tuple, MatchDict],
    seen_keys: set[tuple],
) -> list[MatchDict]:
    result = []
    for match in matches:
        key = _match_key(match)
        if key in occupied_keys or key in live_by_key or key in seen_keys:
            continue
        seen_keys.add(key)
        result.append(match)
    return result


def assign_slots(
    live_matches: list[MatchDict],
    slots: int,
    league_priority: list[str],
    previous_assignment: list[MatchDict | None] | None = None,
    upcoming_matches: list[MatchDict] | None = None,
    far_upcoming_matches: list[MatchDict] | None = None,
) -> tuple[list[MatchDict | None], list[MatchDict | None], list[MatchDict]]:
    """Returns `(assignment, reserved_for, overflow)`. Sticky: a live match
    keeps its slot until it ends, never preempted by a higher-priority
    arrival. `upcoming_matches`/`far_upcoming_matches` are unstarted matches
    that can claim an empty slot ahead of time (near competes for contested
    slots, far only takes an uncontested one) -- see tests for exact policy."""
    upcoming_matches = upcoming_matches or []
    far_upcoming_matches = far_upcoming_matches or []

    assignment: list[MatchDict | None] = list(previous_assignment or [])
    assignment += [None] * (slots - len(assignment))
    assignment = assignment[:slots]

    live_by_key = {_match_key(match): match for match in live_matches}

    previous_channel_by_slot = {
        i: occupant.get("stream_channel") for i, occupant in enumerate(assignment) if occupant is not None
    }

    freed_slots = [
        i for i, occupant in enumerate(assignment) if occupant is not None and _match_key(occupant) not in live_by_key
    ]
    for i in freed_slots:
        assignment[i] = None

    occupied_keys = {_match_key(m) for m in assignment if m is not None}
    unassigned_live = [m for m in live_matches if _match_key(m) not in occupied_keys]

    seen_upcoming_keys: set[tuple] = set()
    near_candidates = _dedupe_candidates(upcoming_matches, occupied_keys, live_by_key, seen_upcoming_keys)
    far_candidates = _dedupe_candidates(far_upcoming_matches, occupied_keys, live_by_key, seen_upcoming_keys)

    # Who wins an empty slot is decided by priority alone. Same-channel
    # continuity (below) only decides which specific slot number a winner
    # lands on -- it must never let a lower-priority continuation take a
    # freed slot ahead of a higher-priority candidate that's also waiting
    # for one, live or reserved.
    primary = [(m, True) for m in unassigned_live] + [(m, False) for m in near_candidates]
    primary.sort(key=lambda pair: _priority_rank(pair[0], league_priority))
    secondary = [(m, False) for m in sorted(far_candidates, key=lambda m: _priority_rank(m, league_priority))]
    combined = primary + secondary

    empty_slot_indexes = [i for i, occupant in enumerate(assignment) if occupant is None]
    winners = combined[:len(empty_slot_indexes)]
    overflow = [match for match, _is_live in combined[len(empty_slot_indexes):]]

    remaining_slots = list(empty_slot_indexes)
    remaining_winners = list(winners)
    for slot_index in empty_slot_indexes:
        channel = previous_channel_by_slot.get(slot_index)
        if channel is None:
            continue
        match_pair = next((pair for pair in remaining_winners if pair[1] and pair[0].get("stream_channel") == channel), None)
        if match_pair is not None:
            assignment[slot_index] = match_pair[0]
            remaining_slots.remove(slot_index)
            remaining_winners.remove(match_pair)

    reserved_for: list[MatchDict | None] = [None] * slots
    for slot_index, (match, is_live) in zip(remaining_slots, remaining_winners):
        if is_live:
            assignment[slot_index] = match
        else:
            reserved_for[slot_index] = match

    return assignment, reserved_for, overflow


def project_schedule(
    matches: list[MatchDict],
    slots: int,
    league_priority: list[str],
    duration: timedelta,
    initial_assignment: list[MatchDict | None] | None = None,
) -> list[list[tuple[datetime, MatchDict]]]:
    """Replays assign_slots across a known future schedule instead of just
    "now". Returns, per slot, `(claimed_at, match)` pairs -- claimed_at is
    when the slot actually started showing that match, which can be later
    than the match's own start if it had to wait out a contested slot."""
    intervals = [
        (datetime.fromisoformat(m["start"]), datetime.fromisoformat(m["start"]) + duration, m) for m in matches
    ]
    event_points = sorted({start for start, _end, _m in intervals} | {end for _start, end, _m in intervals})

    running_assignment: list[MatchDict | None] = list(initial_assignment or [None] * slots)
    per_slot_history: list[list[tuple[datetime, MatchDict | None]]] = [[] for _ in range(slots)]

    for point in event_points:
        live_now = [m for start, end, m in intervals if start <= point < end]
        running_assignment, _reserved_for, _overflow = assign_slots(
            live_matches=live_now,
            slots=slots,
            league_priority=league_priority,
            previous_assignment=running_assignment,
        )
        for slot_index, match in enumerate(running_assignment):
            per_slot_history[slot_index].append((point, match))

    # Keep each match once, at the point it first claimed the slot.
    projected: list[list[tuple[datetime, MatchDict]]] = []
    for history in per_slot_history:
        chronological: list[tuple[datetime, MatchDict]] = []
        last_key: object = object()
        for point, match in history:
            key = _match_key(match) if match is not None else None
            if key != last_key:
                if match is not None:
                    chronological.append((point, match))
                last_key = key
        projected.append(chronological)
    return projected
