"""Tests for allocator.assign_slots — the core stream-switching policy.
Every test uses real-shaped match dicts (as they'd appear after JSON-decoding
schedule.json) rather than stripped-down stand-ins, so a field-name typo here
would actually be caught."""

from __future__ import annotations

from esports_scheduler.allocator import assign_slots

VALORANT_PRIORITY = ["VCT Americas", "VCT EMEA", "VCT Pacific"]


def _match(league: str, start: str, title: str, twitch_channel: str) -> dict:
    return {
        "league": league,
        "game": "valorant",
        "start": start,
        "state": "in_progress",
        "title": title,
        "twitch_channel": twitch_channel,
    }


def test_fills_empty_slots_by_priority_when_nothing_was_previously_assigned():
    americas = _match("VCT Americas", "2026-07-27T18:00:00+00:00", "Sentinels vs 100T", "valorant_americas")
    pacific = _match("VCT Pacific", "2026-07-27T18:00:00+00:00", "Paper Rex vs DRX", "valorant_pacific")

    assignment = assign_slots(
        live_matches=[pacific, americas],  # deliberately out of priority order
        slots=2,
        league_priority=VALORANT_PRIORITY,
        previous_assignment=None,
    )

    assert assignment == [americas, pacific]


def test_sticky_keeps_existing_live_match_even_when_a_higher_priority_match_starts():
    emea = _match("VCT EMEA", "2026-07-27T14:00:00+00:00", "Fnatic vs Team Liquid", "valorant_emea")
    americas = _match("VCT Americas", "2026-07-27T18:00:00+00:00", "Sentinels vs 100T", "valorant_americas")

    # EMEA already holds the only slot; Americas (higher priority) starts later
    # while EMEA is still live — policy says Americas waits, it does not preempt.
    assignment = assign_slots(
        live_matches=[emea, americas],
        slots=1,
        league_priority=VALORANT_PRIORITY,
        previous_assignment=[emea],
    )

    assert assignment == [emea]


def test_overflow_matches_beyond_available_slots_are_dropped_not_queued():
    americas = _match("VCT Americas", "2026-07-27T18:00:00+00:00", "Sentinels vs 100T", "valorant_americas")
    emea = _match("VCT EMEA", "2026-07-27T18:00:00+00:00", "Fnatic vs Team Liquid", "valorant_emea")
    pacific = _match("VCT Pacific", "2026-07-27T18:00:00+00:00", "Paper Rex vs DRX", "valorant_pacific")

    assignment = assign_slots(
        live_matches=[pacific, emea, americas],
        slots=2,
        league_priority=VALORANT_PRIORITY,
        previous_assignment=None,
    )

    assert assignment == [americas, emea]
    assert pacific not in assignment


def test_match_ending_frees_its_slot_for_the_next_highest_priority_live_match():
    emea = _match("VCT EMEA", "2026-07-27T14:00:00+00:00", "Fnatic vs Team Liquid", "valorant_emea")
    pacific = _match("VCT Pacific", "2026-07-27T18:00:00+00:00", "Paper Rex vs DRX", "valorant_pacific")

    # EMEA held the slot last tick but is no longer in the live list (it ended).
    assignment = assign_slots(
        live_matches=[pacific],
        slots=1,
        league_priority=VALORANT_PRIORITY,
        previous_assignment=[emea],
    )

    assert assignment == [pacific]


def test_unranked_league_is_lowest_priority_but_still_fills_a_free_slot():
    unranked = _match("VCT Masters", "2026-07-27T18:00:00+00:00", "Team A vs Team B", "valorant_masters")

    assignment = assign_slots(
        live_matches=[unranked],
        slots=1,
        league_priority=VALORANT_PRIORITY,
        previous_assignment=None,
    )

    assert assignment == [unranked]


def test_empty_live_matches_produces_all_none_slots():
    assignment = assign_slots(
        live_matches=[],
        slots=2,
        league_priority=VALORANT_PRIORITY,
        previous_assignment=None,
    )

    assert assignment == [None, None]


def test_previous_assignment_longer_than_slots_is_truncated_not_errored():
    americas = _match("VCT Americas", "2026-07-27T18:00:00+00:00", "Sentinels vs 100T", "valorant_americas")

    assignment = assign_slots(
        live_matches=[americas],
        slots=1,
        league_priority=VALORANT_PRIORITY,
        previous_assignment=[americas, None, None],
    )

    assert assignment == [americas]
