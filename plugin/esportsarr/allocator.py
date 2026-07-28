"""Pure slot-assignment logic — no Django dependency, so this is fully
unit-testable without a Dispatcharr environment (see tests/test_allocator.py).

Assignment is "sticky": a match already occupying a slot keeps it for as long
as it's still live, so a higher-priority match that starts later does not
preempt it. This matches the chosen overflow policy: when there are more live
matches than slots, the lowest-priority ones simply aren't shown until a slot
frees up on its own (the occupying match ending), rather than being bumped.

`upcoming_matches` (not-yet-live, pre-filtered by the caller to a lookahead
window — see plugin.py's RESERVATION_GRACE_MINUTES/reservation_lookahead_minutes)
can still claim an *empty* slot ahead of a lower-priority live match, holding
it in reserve until the anticipated match actually goes live. This is
deliberately NOT preemption — it only affects slots that aren't already
occupied, never bumps an already-live match out of its slot.
"""

from __future__ import annotations

from typing import Any

MatchDict = dict[str, Any]


def _match_key(match: MatchDict) -> tuple:
    """Identity used to tell "the same match" apart across ticks. league+start
    is enough — Riot doesn't reuse a start time for two different matches in
    the same league."""
    return (match.get("league"), match.get("start"))


def _priority_rank(match: MatchDict, league_priority: list[str]) -> int:
    league = match.get("league")
    try:
        return league_priority.index(league)
    except ValueError:
        return len(league_priority)  # unranked leagues sort last


def assign_slots(
    live_matches: list[MatchDict],
    slots: int,
    league_priority: list[str],
    previous_assignment: list[MatchDict | None] | None = None,
    upcoming_matches: list[MatchDict] | None = None,
) -> tuple[list[MatchDict | None], list[MatchDict | None]]:
    """Returns `(assignment, reserved_for)`, both lists of length `slots`.

    `assignment[i]` is the live match occupying slot i, or None.

    `reserved_for[i]` is only ever set when `assignment[i]` is None: the
    upcoming (not-yet-live) match that reserved this slot ahead of a
    lower-priority live match — exposed so the caller can still write a
    "coming up" guide entry for an empty slot instead of leaving it blank.
    None means the slot is genuinely idle, nothing live or anticipated.

    `live_matches` should already be filtered to one game and to
    state == "in_progress" — this function has no concept of "game" at all,
    it just slots whatever list of matches it's given.

    `upcoming_matches` (state == "unstarted", also pre-filtered to one game)
    can win an *empty* slot ahead of a lower-priority live match, reserving
    it (left as None in `assignment`) until the anticipated match actually
    goes live — see module docstring. This function does no date parsing;
    window-filtering "upcoming" to something actually imminent is entirely
    the caller's job.
    """
    upcoming_matches = upcoming_matches or []

    assignment: list[MatchDict | None] = list(previous_assignment or [])
    assignment += [None] * (slots - len(assignment))
    assignment = assignment[:slots]

    live_by_key = {_match_key(match): match for match in live_matches}

    # Free any slot whose occupant is no longer live.
    for i, occupant in enumerate(assignment):
        if occupant is not None and _match_key(occupant) not in live_by_key:
            assignment[i] = None

    occupied_keys = {_match_key(m) for m in assignment if m is not None}

    # Live matches not already occupying a slot compete for the remaining ones.
    unassigned_live = [m for m in live_matches if _match_key(m) not in occupied_keys]

    # Dedupe upcoming matches and drop any that are already occupying a slot
    # or already live — a match must never compete against itself for a
    # reservation (an accidental duplicate in the feed would otherwise burn
    # two reservation slots on one anticipated match).
    reservation_candidates: list[MatchDict] = []
    seen_upcoming_keys: set[tuple] = set()
    for match in upcoming_matches:
        key = _match_key(match)
        if key in occupied_keys or key in live_by_key or key in seen_upcoming_keys:
            continue
        seen_upcoming_keys.add(key)
        reservation_candidates.append(match)

    # Live candidates listed first: a stable sort then makes a live match win
    # a same-rank tie over a merely-scheduled one, with no extra tie-break
    # logic to get wrong.
    combined = [(m, True) for m in unassigned_live] + [(m, False) for m in reservation_candidates]
    combined.sort(key=lambda pair: _priority_rank(pair[0], league_priority))

    # Fill empty slots, in slot order, from the top of that ranking. A live
    # candidate fills the slot; a reservation-only candidate leaves it None,
    # held for the anticipated match instead of being given to something
    # lower-priority that's live right now — but recorded in `reserved_for`
    # so the caller can still preview it in the guide.
    reserved_for: list[MatchDict | None] = [None] * slots
    empty_slot_indexes = [i for i, occupant in enumerate(assignment) if occupant is None]
    for slot_index, (match, is_live) in zip(empty_slot_indexes, combined):
        if is_live:
            assignment[slot_index] = match
        else:
            reserved_for[slot_index] = match

    return assignment, reserved_for
