"""Pure slot-assignment logic — no Django dependency, so this is fully
unit-testable without a Dispatcharr environment (see tests/test_allocator.py).

Assignment is "sticky": a match already occupying a slot keeps it for as long
as it's still live, so a higher-priority match that starts later does not
preempt it. This matches the chosen overflow policy: when there are more live
matches than slots, the lowest-priority ones simply aren't shown until a slot
frees up on its own (the occupying match ending), rather than being bumped.
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
) -> list[MatchDict | None]:
    """Returns a list of length `slots`; each entry is the match dict assigned
    to that slot, or None if no live match currently holds it.

    `live_matches` should already be filtered to one game and to
    state == "in_progress" — this function has no concept of "game" at all,
    it just slots whatever list of matches it's given.
    """
    assignment: list[MatchDict | None] = list(previous_assignment or [])
    assignment += [None] * (slots - len(assignment))
    assignment = assignment[:slots]

    live_by_key = {_match_key(match): match for match in live_matches}

    # Free any slot whose occupant is no longer live.
    for i, occupant in enumerate(assignment):
        if occupant is not None and _match_key(occupant) not in live_by_key:
            assignment[i] = None

    # Rank live matches not already occupying a slot, highest priority first.
    occupied_keys = {_match_key(m) for m in assignment if m is not None}
    unassigned = [m for m in live_matches if _match_key(m) not in occupied_keys]
    unassigned.sort(key=lambda m: _priority_rank(m, league_priority))

    # Fill empty slots, in slot order, with the highest-priority unassigned matches.
    empty_slot_indexes = [i for i, occupant in enumerate(assignment) if occupant is None]
    for slot_index, match in zip(empty_slot_indexes, unassigned):
        assignment[slot_index] = match

    return assignment
