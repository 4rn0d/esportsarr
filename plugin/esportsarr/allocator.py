"""Pure slot-assignment logic. No Django dependency, so this is fully
unit-testable without a Dispatcharr environment (see tests/test_allocator.py).

Assignment is "sticky": a match already occupying a slot keeps it for as long
as it's still live, so a higher-priority match that starts later does not
preempt it. This matches the chosen overflow policy: when there are more live
matches than slots, the lowest-priority ones simply aren't shown until a slot
frees up on its own (the occupying match ending), rather than being bumped.

Not-yet-live matches (state == "unstarted") can still claim an *empty* slot
ahead of a lower-priority live match, holding it in reserve until the
anticipated match actually goes live. This is deliberately NOT preemption,
it only affects slots that aren't already occupied. There are two windows for
this, both pre-filtered by the caller (see plugin.py's
reservation_lookahead_minutes/reservation_priority_minutes settings), because
esports broadcasts typically go live on Twitch ~1h before the official match
time, not right at it:

- `upcoming_matches` ("near", within reservation_priority_minutes): competes
  on equal footing with live matches by priority rank, can win a contested
  slot away from a lower-priority live match's chance at it.
- `far_upcoming_matches` ("far", within the wider reservation_lookahead
  _minutes but outside the near window): can only ever take a slot that no
  live or near candidate wants. A distant international preview must never
  cost a live regional match its shot at a contested slot just because the
  international is scheduled sooner. Only once it's within the near window
  does it start competing for real.

Same-channel continuity: when a slot's occupant stops being live, and one of
this tick's still-unassigned live matches is on the very same stream channel
(the same league's broadcast simply moved on to its next match), that match
claims the *same* slot outright, ahead of the normal priority/slot-index
fill. Without this, two slots freeing up in the same tick could hand each
other's continuing broadcast to a different generic channel than before,
purely because of priority-rank/slot-index ordering, jarring even though
nothing about the actual stream changed. This only applies to live-to-live
continuations; a near/far reservation on the same channel isn't (yet)
given the same treatment.

`project_schedule` (below) drives `assign_slots` forward across a *known*
future schedule instead of reacting to the present, to build a week-ahead
guide instead of a one-tick snapshot. Unlike `assign_slots`, it does parse
match start times. It has to, to know which matches are "live" at any
given point along the projected timeline. It replays `assign_slots` at
every point where some match starts or ends (nothing can change in
between), carrying the previous call's result forward each time, the same
mechanism a real sync tick already uses, just walking a known future
instead of reacting to the present. No near/far reservation windows are
involved: those exist purely to handle real-time uncertainty about exactly
when a Twitch stream goes live relative to *now*. A deterministic future
schedule has no such uncertainty, every match's real start time is already
known up front.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

MatchDict = dict[str, Any]


def _match_key(match: MatchDict) -> tuple:
    """Identity used to tell "the same match" apart across ticks. league+start
    is enough. Riot doesn't reuse a start time for two different matches in
    the same league."""
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
    """Drops matches already occupying a slot, already live, or duplicated,
    whether within one bucket or across the near/far buckets (`seen_keys` is
    shared between both calls). A match must never compete against itself
    for a reservation; an accidental duplicate in the feed would otherwise
    burn two reservation slots on one anticipated match."""
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
    """Returns `(assignment, reserved_for, overflow)`.

    `assignment[i]` is the live match occupying slot i, or None. Both
    `assignment` and `reserved_for` have length `slots`.

    `reserved_for[i]` is only ever set when `assignment[i]` is None: the
    upcoming (not-yet-live) match that reserved this slot, exposed so the
    caller can still write a "coming up" guide entry for an empty slot
    instead of leaving it blank. None means the slot is genuinely idle,
    nothing live or anticipated.

    `overflow` is every remaining candidate (live or upcoming) that lost out
    on both a slot and a reservation, in priority order, e.g. a 3rd live
    match with only 2 slots, or a near/far candidate that couldn't unseat
    what's already live/reserved. This function has no concept of match end
    times (Riot never gives one), so it can't say *which* live slot a given
    overflow candidate will eventually land on. It only ranks them. The
    caller (which does know estimated end times) can use this to preview
    "what's next" once a live slot frees up.

    `live_matches` should already be filtered to one game and to
    state == "in_progress". This function has no concept of "game" at all,
    it just slots whatever list of matches it's given. `upcoming_matches`/
    `far_upcoming_matches` (state == "unstarted") are the near/far windows
    described in the module docstring. This function does no date parsing;
    window-filtering "upcoming" to something actually imminent, and to which
    bucket it belongs, is entirely the caller's job.
    """
    upcoming_matches = upcoming_matches or []
    far_upcoming_matches = far_upcoming_matches or []

    assignment: list[MatchDict | None] = list(previous_assignment or [])
    assignment += [None] * (slots - len(assignment))
    assignment = assignment[:slots]

    live_by_key = {_match_key(match): match for match in live_matches}

    # Captured before freeing anything, so a slot whose match just ended can
    # be handed straight back to whatever continues that same stream channel
    # (see module docstring's "Same-channel continuity" section).
    previous_channel_by_slot = {
        i: occupant.get("stream_channel") for i, occupant in enumerate(assignment) if occupant is not None
    }

    # Free any slot whose occupant is no longer live.
    freed_slots = [
        i for i, occupant in enumerate(assignment) if occupant is not None and _match_key(occupant) not in live_by_key
    ]
    for i in freed_slots:
        assignment[i] = None

    occupied_keys = {_match_key(m) for m in assignment if m is not None}

    # Live matches not already occupying a slot compete for the remaining ones.
    unassigned_live = [m for m in live_matches if _match_key(m) not in occupied_keys]

    # A freed slot's own continuing broadcast claims that exact slot
    # outright, ahead of the general priority/slot-index fill below.
    # Otherwise two slots freeing up in the same tick could swap which
    # generic channel shows which league purely due to ranking order, even
    # though neither channel's actual stream changed.
    for slot_index in freed_slots:
        channel = previous_channel_by_slot.get(slot_index)
        if channel is None:
            continue
        continuation = next((m for m in unassigned_live if m.get("stream_channel") == channel), None)
        if continuation is not None:
            assignment[slot_index] = continuation
            unassigned_live.remove(continuation)
            occupied_keys.add(_match_key(continuation))

    seen_upcoming_keys: set[tuple] = set()
    near_candidates = _dedupe_candidates(upcoming_matches, occupied_keys, live_by_key, seen_upcoming_keys)
    far_candidates = _dedupe_candidates(far_upcoming_matches, occupied_keys, live_by_key, seen_upcoming_keys)

    # Live + near candidates compete on equal footing by priority rank (live
    # listed first so a stable sort makes a live match win a same-rank tie
    # over a merely-scheduled one). Far candidates are appended strictly
    # after this whole ranked group, regardless of their own priority rank.
    # They can only ever fill a slot nothing in the primary group wants.
    primary = [(m, True) for m in unassigned_live] + [(m, False) for m in near_candidates]
    primary.sort(key=lambda pair: _priority_rank(pair[0], league_priority))
    secondary = [(m, False) for m in sorted(far_candidates, key=lambda m: _priority_rank(m, league_priority))]
    combined = primary + secondary

    # Fill empty slots, in slot order, from the top of that ranking. A live
    # candidate fills the slot; a reservation-only candidate leaves it None,
    # held for the anticipated match instead of being given to something
    # lower-priority that's live right now, but recorded in `reserved_for`
    # so the caller can still preview it in the guide.
    reserved_for: list[MatchDict | None] = [None] * slots
    empty_slot_indexes = [i for i, occupant in enumerate(assignment) if occupant is None]
    for slot_index, (match, is_live) in zip(empty_slot_indexes, combined):
        if is_live:
            assignment[slot_index] = match
        else:
            reserved_for[slot_index] = match

    overflow = [match for match, _is_live in combined[len(empty_slot_indexes):]]

    return assignment, reserved_for, overflow


def project_schedule(
    matches: list[MatchDict],
    slots: int,
    league_priority: list[str],
    duration: timedelta,
    initial_assignment: list[MatchDict | None] | None = None,
) -> list[list[tuple[datetime, MatchDict]]]:
    """Deterministically projects assign_slots's priority and same-channel-
    continuity rules forward across a known future schedule. Returns, per
    slot, the chronological list of `(claimed_at, match)` pairs it will show.
    Two matches whose intervals overlap and both want the same slot are
    resolved exactly like a real sync tick would (priority wins, the loser
    is simply omitted for that window entirely, never queued or interleaved)
    -- but "omitted" doesn't mean "gone forever": a match that lost a
    contested slot is still "live" for the rest of its own interval, and can
    still claim a *different* slot once one frees up, at whatever point that
    actually happens. `claimed_at` is that real point, which can be later
    than the match's own `start` -- see the caller-facing note below on why
    that matters.

    Each match's interval is `[start, start + duration)`. Works by replaying
    assign_slots() at every point where some match starts or ends, nothing
    can change between two such points, each time treating whatever
    matches are mid-interval as "live" and carrying the previous call's
    result forward as `previous_assignment`. `initial_assignment` seeds the
    very first replay, so the projection picks up exactly where a real
    tick's own `assign_slots()` call left off rather than starting cold,
    but that seed only covers the very first point in time. A currently
    in_progress match must also be included in `matches` itself (with its
    real `start`), or the first later event point evaluated won't know it's
    still live and will free its slot immediately, regardless of what
    `initial_assignment` said.

    A match reported as currently live whose computed interval has already
    elapsed by this plugin's own duration estimate (e.g. a best-of-5 running
    long) can get dropped from the projection earlier than it actually ends
    in reality, the same "3 hours is just an estimate, Riot gives no real
    end time" limitation this codebase already accepts everywhere else, and
    it self-corrects on the very next sync tick once matches update, since
    the guide is fully rebuilt every tick from the latest known state.

    Why `claimed_at` exists (confirmed as a real bug, 2026-07-30, against a
    real 3-way conflict on one Twitch channel): a match that loses a
    contested slot doesn't vanish -- it's still live and can win a
    *different* slot once one frees up, possibly well after its own `start`.
    If the caller displayed that match at its own `start` regardless, it
    would show as still occupying a slot the *previous* match hasn't
    actually finished yet by our own duration estimate -- a real, visible
    overlap in the guide. A match's `claimed_at` is never earlier than its
    own `start` (it can only be picked up once it's actually live), so using
    it as the displayed start instead is always safe and always at least as
    accurate.
    """
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

    # A match occupying a slot across several consecutive event points (other
    # slots changing around it doesn't affect it, per the sticky rule) must
    # appear once in the result, not fragmented into repeated entries -- kept
    # at the point it *first* claimed the slot, not every point it held it.
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
