"""Tests for allocator.assign_slots, the core stream-switching policy.
Every test uses real-shaped match dicts (as they'd appear after JSON-decoding
schedule.json) rather than stripped-down stand-ins, so a field-name typo here
would actually be caught."""

from __future__ import annotations

from datetime import datetime, timedelta

from esportsarr.allocator import assign_slots, project_schedule


def _at(match: dict) -> datetime:
    """The expected `claimed_at` for a match that wins its slot with no
    contention, i.e. exactly its own real start."""
    return datetime.fromisoformat(match["start"])

VALORANT_PRIORITY = ["VCT Americas", "VCT EMEA", "VCT Pacific"]

# Internationals ranked first, matching the real default in plugin.py/plugin.json.
VALORANT_PRIORITY_WITH_INTL = ["Champions", "VCT Americas", "VCT EMEA", "VCT Pacific"]


def _match(league: str, start: str, title: str, stream_channel: str) -> dict:
    return {
        "league": league,
        "game": "valorant",
        "start": start,
        "state": "in_progress",
        "title": title,
        "stream_channel": stream_channel,
    }


def _upcoming(league: str, start: str, title: str, stream_channel: str) -> dict:
    return {**_match(league, start, title, stream_channel), "state": "unstarted"}


def test_fills_empty_slots_by_priority_when_nothing_was_previously_assigned():
    americas = _match("VCT Americas", "2026-07-27T18:00:00+00:00", "Sentinels vs 100T", "valorant_americas")
    pacific = _match("VCT Pacific", "2026-07-27T18:00:00+00:00", "Paper Rex vs DRX", "valorant_pacific")

    assignment, reserved_for, _overflow = assign_slots(
        live_matches=[pacific, americas],  # deliberately out of priority order
        slots=2,
        league_priority=VALORANT_PRIORITY,
        previous_assignment=None,
    )

    assert assignment == [americas, pacific]
    assert reserved_for == [None, None]


def test_sticky_keeps_existing_live_match_even_when_a_higher_priority_match_starts():
    emea = _match("VCT EMEA", "2026-07-27T14:00:00+00:00", "Fnatic vs Team Liquid", "valorant_emea")
    americas = _match("VCT Americas", "2026-07-27T18:00:00+00:00", "Sentinels vs 100T", "valorant_americas")

    # EMEA already holds the only slot; Americas (higher priority) starts later
    # while EMEA is still live. Policy says Americas waits, it does not preempt.
    assignment, _, _overflow = assign_slots(
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

    assignment, _, overflow = assign_slots(
        live_matches=[pacific, emea, americas],
        slots=2,
        league_priority=VALORANT_PRIORITY,
        previous_assignment=None,
    )

    assert assignment == [americas, emea]
    assert pacific not in assignment
    # "Dropped" means it doesn't occupy a slot. It's still surfaced via
    # overflow so the caller can preview it once a slot frees up.
    assert overflow == [pacific]


def test_match_ending_frees_its_slot_for_the_next_highest_priority_live_match():
    emea = _match("VCT EMEA", "2026-07-27T14:00:00+00:00", "Fnatic vs Team Liquid", "valorant_emea")
    pacific = _match("VCT Pacific", "2026-07-27T18:00:00+00:00", "Paper Rex vs DRX", "valorant_pacific")

    # EMEA held the slot last tick but is no longer in the live list (it ended).
    assignment, _, _overflow = assign_slots(
        live_matches=[pacific],
        slots=1,
        league_priority=VALORANT_PRIORITY,
        previous_assignment=[emea],
    )

    assert assignment == [pacific]


def test_same_stream_channel_continuation_keeps_its_original_slot_even_when_another_slot_also_frees_up():
    # Slot 0 held EMEA, slot 1 held Americas. Both matches end in the same
    # tick and both leagues immediately start a new match on the exact same
    # Twitch channel as before (e.g. game 2 of a same-day series). Without
    # same-channel continuity, Americas (higher priority) would grab slot 0
    # (lowest empty index) and EMEA would land on slot 1, swapping which
    # generic channel shows which league even though neither's actual stream
    # changed. Continuity must keep each on its own original slot.
    old_emea = _match("VCT EMEA", "2026-07-27T14:00:00+00:00", "Fnatic vs Team Liquid", "valorant_emea")
    old_americas = _match("VCT Americas", "2026-07-27T14:00:00+00:00", "Sentinels vs 100T", "valorant_americas")
    new_emea = _match("VCT EMEA", "2026-07-27T16:00:00+00:00", "Team Heretics vs KOI", "valorant_emea")
    new_americas = _match("VCT Americas", "2026-07-27T16:00:00+00:00", "LOUD vs NRG", "valorant_americas")

    assignment, _, _overflow = assign_slots(
        live_matches=[new_emea, new_americas],
        slots=2,
        league_priority=VALORANT_PRIORITY,
        previous_assignment=[old_emea, old_americas],
    )

    assert assignment == [new_emea, new_americas]


def test_same_stream_channel_continuation_does_not_block_a_genuinely_new_channel_from_the_other_freed_slot():
    # Slot 0's EMEA match ends with no continuation on that channel; slot 1's
    # Americas match ends and is immediately followed by a new Americas match
    # on the same channel. The Americas continuation must still claim slot 1
    # specifically, leaving slot 0 to whatever wins it via normal priority
    # ranking, here, a brand new Pacific match with nothing competing for it.
    old_emea = _match("VCT EMEA", "2026-07-27T14:00:00+00:00", "Fnatic vs Team Liquid", "valorant_emea")
    old_americas = _match("VCT Americas", "2026-07-27T14:00:00+00:00", "Sentinels vs 100T", "valorant_americas")
    new_americas = _match("VCT Americas", "2026-07-27T16:00:00+00:00", "LOUD vs NRG", "valorant_americas")
    new_pacific = _match("VCT Pacific", "2026-07-27T16:00:00+00:00", "Paper Rex vs DRX", "valorant_pacific")

    assignment, _, _overflow = assign_slots(
        live_matches=[new_pacific, new_americas],
        slots=2,
        league_priority=VALORANT_PRIORITY,
        previous_assignment=[old_emea, old_americas],
    )

    assert assignment == [new_pacific, new_americas]


def test_same_channel_continuity_never_costs_a_higher_priority_candidate_its_slot():
    # Regression test for a real bug (2026-07-30): slot 0 held VCT EMEA, slot
    # 1 held Game Changers EMEA, both on the same Twitch channel. Both end in
    # the same tick; a new VCT EMEA match continues on that channel (higher
    # priority), a new Game Changers EMEA match also continues on it (lowest
    # priority), and a Last Chance Qualifier Americas match (middle priority,
    # different channel entirely) is also waiting. With only 2 slots, GC EMEA
    # must lose the priority contest entirely and never get a slot back, even
    # though it's "continuing its own channel" -- continuity only decides
    # which slot a priority-contest winner lands on, never who wins.
    priority = ["VCT EMEA", "Last Chance Qualifier Americas", "Game Changers EMEA"]
    old_emea = _match("VCT EMEA", "2026-07-27T14:00:00+00:00", "GIANTX vs Team Liquid", "valorant_emea")
    old_gc_emea = _match("Game Changers EMEA", "2026-07-27T14:00:00+00:00", "SK Nebula vs G2 Gozen", "valorant_emea")
    new_emea = _match("VCT EMEA", "2026-07-27T17:00:00+00:00", "Gentle Mates vs Team Vitality", "valorant_emea")
    new_gc_emea = _match(
        "Game Changers EMEA", "2026-07-27T17:00:00+00:00", "ALTERNATE aTTaX Ruby vs Barca eSports", "valorant_emea"
    )
    lcq = _upcoming(
        "Last Chance Qualifier Americas", "2026-07-27T18:00:00+00:00", "Shopify Rebellion Black vs 2GAME", "VALORANT_NorthAmerica"
    )

    assignment, reserved_for, overflow = assign_slots(
        live_matches=[new_emea, new_gc_emea],
        slots=2,
        league_priority=priority,
        previous_assignment=[old_emea, old_gc_emea],
        upcoming_matches=[lcq],
    )

    assert assignment == [new_emea, None]
    assert reserved_for == [None, lcq]
    assert overflow == [new_gc_emea]


def test_unranked_league_is_lowest_priority_but_still_fills_a_free_slot():
    unranked = _match("VCT Masters", "2026-07-27T18:00:00+00:00", "Team A vs Team B", "valorant_masters")

    assignment, _, _overflow = assign_slots(
        live_matches=[unranked],
        slots=1,
        league_priority=VALORANT_PRIORITY,
        previous_assignment=None,
    )

    assert assignment == [unranked]


def test_empty_live_matches_produces_all_none_slots():
    assignment, reserved_for, _overflow = assign_slots(
        live_matches=[],
        slots=2,
        league_priority=VALORANT_PRIORITY,
        previous_assignment=None,
    )

    assert assignment == [None, None]
    assert reserved_for == [None, None]


def test_previous_assignment_longer_than_slots_is_truncated_not_errored():
    americas = _match("VCT Americas", "2026-07-27T18:00:00+00:00", "Sentinels vs 100T", "valorant_americas")

    assignment, _, _overflow = assign_slots(
        live_matches=[americas],
        slots=1,
        league_priority=VALORANT_PRIORITY,
        previous_assignment=[americas, None, None],
    )

    assert assignment == [americas]


def test_upcoming_higher_priority_match_reserves_a_slot_instead_of_a_lower_priority_live_one():
    americas = _match("VCT Americas", "2026-07-27T18:00:00+00:00", "Sentinels vs 100T", "valorant_americas")
    champions = _upcoming("Champions", "2026-07-27T19:00:00+00:00", "Grand Final", "valorant")

    # Two empty slots, one live regional match, one imminent international
    # not live yet. The international's reservation wins the higher-ranked
    # slot; the regional gets the other one rather than both being empty.
    assignment, reserved_for, _overflow = assign_slots(
        live_matches=[americas],
        slots=2,
        league_priority=VALORANT_PRIORITY_WITH_INTL,
        previous_assignment=None,
        upcoming_matches=[champions],
    )

    assert assignment == [None, americas]
    # The reserved slot exposes exactly which match justified holding it open
    # That's what lets the caller write a "coming up" guide entry for it.
    assert reserved_for == [champions, None]


def test_reservation_never_preempts_an_already_live_match():
    americas = _match("VCT Americas", "2026-07-27T18:00:00+00:00", "Sentinels vs 100T", "valorant_americas")
    champions = _upcoming("Champions", "2026-07-27T19:00:00+00:00", "Grand Final", "valorant")

    # Americas already holds the only slot and is still live. Champions is
    # imminent but there are zero empty slots. The existing live match must
    # never be bumped out to make room for a reservation.
    assignment, reserved_for, _overflow = assign_slots(
        live_matches=[americas],
        slots=1,
        league_priority=VALORANT_PRIORITY_WITH_INTL,
        previous_assignment=[americas],
        upcoming_matches=[champions],
    )

    assert assignment == [americas]
    assert reserved_for == [None]


def test_duplicate_upcoming_entries_for_the_same_match_only_reserve_one_slot():
    americas = _match("VCT Americas", "2026-07-27T18:00:00+00:00", "Sentinels vs 100T", "valorant_americas")
    champions = _upcoming("Champions", "2026-07-27T19:00:00+00:00", "Grand Final", "valorant")

    # Regression guard: a duplicate in the upcoming list (feed glitch, double
    # count) must not burn two reservation slots on the same anticipated
    # match. That would leave the live regional match with nowhere to go.
    assignment, reserved_for, _overflow = assign_slots(
        live_matches=[americas],
        slots=2,
        league_priority=VALORANT_PRIORITY_WITH_INTL,
        previous_assignment=None,
        upcoming_matches=[champions, dict(champions)],
    )

    assert assignment == [None, americas]
    assert reserved_for == [champions, None]


def test_upcoming_matches_defaults_when_not_provided():
    americas = _match("VCT Americas", "2026-07-27T18:00:00+00:00", "Sentinels vs 100T", "valorant_americas")

    assignment, reserved_for, _overflow = assign_slots(
        live_matches=[americas],
        slots=1,
        league_priority=VALORANT_PRIORITY,
        previous_assignment=None,
    )

    assert assignment == [americas]
    assert reserved_for == [None]


def test_idle_slot_with_nothing_live_or_upcoming_has_no_reservation_preview():
    # Nothing live, nothing anticipated. reserved_for must stay None rather
    # than pointing at something stale, so the caller knows to write an
    # honest "no match scheduled" placeholder instead of a preview.
    assignment, reserved_for, _overflow = assign_slots(
        live_matches=[],
        slots=2,
        league_priority=VALORANT_PRIORITY_WITH_INTL,
        previous_assignment=None,
        upcoming_matches=[],
    )

    assert assignment == [None, None]
    assert reserved_for == [None, None]


def test_near_upcoming_match_displaces_a_live_match_for_the_only_contested_slot():
    # Baseline for the contrast below: within the "near"/priority window, an
    # international competes on equal footing by rank and can win the only
    # slot away from a live regional match. Pacific gets nothing at all.
    pacific = _match("VCT Pacific", "2026-07-27T18:00:00+00:00", "Paper Rex vs DRX", "valorant_pacific")
    champions = _upcoming("Champions", "2026-07-27T19:00:00+00:00", "Grand Final", "valorant")

    assignment, reserved_for, _overflow = assign_slots(
        live_matches=[pacific],
        slots=1,
        league_priority=VALORANT_PRIORITY_WITH_INTL,
        previous_assignment=None,
        upcoming_matches=[champions],
    )

    assert assignment == [None]
    assert reserved_for == [champions]


def test_far_upcoming_match_does_not_displace_a_live_match_for_a_contested_slot():
    # Same setup as above, but Champions is in the "far" bucket (outside the
    # priority window, still inside the wider lookahead). It must NOT be
    # able to take Pacific's only slot just because it outranks Pacific.
    # Only "near" reservations can do that; "far" ones can't touch a
    # contested slot at all.
    pacific = _match("VCT Pacific", "2026-07-27T18:00:00+00:00", "Paper Rex vs DRX", "valorant_pacific")
    champions = _upcoming("Champions", "2026-07-27T19:00:00+00:00", "Grand Final", "valorant")

    assignment, reserved_for, _overflow = assign_slots(
        live_matches=[pacific],
        slots=1,
        league_priority=VALORANT_PRIORITY_WITH_INTL,
        previous_assignment=None,
        far_upcoming_matches=[champions],
    )

    assert assignment == [pacific]
    assert reserved_for == [None]


def test_far_upcoming_match_still_previews_a_slot_nothing_live_wants():
    # Two slots, one live regional match takes one of them. The other is
    # genuinely uncontested (no live match wants it), so a "far" reservation
    # can still preview it even outside the priority window.
    pacific = _match("VCT Pacific", "2026-07-27T18:00:00+00:00", "Paper Rex vs DRX", "valorant_pacific")
    champions = _upcoming("Champions", "2026-07-27T19:00:00+00:00", "Grand Final", "valorant")

    assignment, reserved_for, _overflow = assign_slots(
        live_matches=[pacific],
        slots=2,
        league_priority=VALORANT_PRIORITY_WITH_INTL,
        previous_assignment=None,
        far_upcoming_matches=[champions],
    )

    assert assignment == [pacific, None]
    assert reserved_for == [None, champions]


def test_overflow_is_empty_when_every_candidate_gets_a_slot_or_a_reservation():
    americas = _match("VCT Americas", "2026-07-27T18:00:00+00:00", "Sentinels vs 100T", "valorant_americas")
    champions = _upcoming("Champions", "2026-07-27T19:00:00+00:00", "Grand Final", "valorant")

    assignment, reserved_for, overflow = assign_slots(
        live_matches=[americas],
        slots=2,
        league_priority=VALORANT_PRIORITY_WITH_INTL,
        previous_assignment=None,
        upcoming_matches=[champions],
    )

    assert assignment == [None, americas]
    assert reserved_for == [champions, None]
    assert overflow == []


def test_overflow_orders_multiple_leftover_candidates_by_priority():
    americas = _match("VCT Americas", "2026-07-27T18:00:00+00:00", "Sentinels vs 100T", "valorant_americas")
    emea = _match("VCT EMEA", "2026-07-27T18:00:00+00:00", "Fnatic vs Team Liquid", "valorant_emea")
    pacific = _match("VCT Pacific", "2026-07-27T18:00:00+00:00", "Paper Rex vs DRX", "valorant_pacific")

    # Only one slot for three live matches. Americas wins it, and the two
    # that lose out must come back in priority order (EMEA before Pacific),
    # not feed order, so the caller can preview the *best* leftover first
    # once the slot frees up.
    assignment, _, overflow = assign_slots(
        live_matches=[pacific, emea, americas],
        slots=1,
        league_priority=VALORANT_PRIORITY,
        previous_assignment=None,
    )

    assert assignment == [americas]
    assert overflow == [emea, pacific]


def test_duplicate_match_across_near_and_far_buckets_only_reserves_once():
    # Regression guard mirroring the within-bucket dedup test: the caller's
    # near/far windows are meant to be mutually exclusive, but if the same
    # match somehow ends up in both, it must still only ever claim one slot.
    americas = _match("VCT Americas", "2026-07-27T18:00:00+00:00", "Sentinels vs 100T", "valorant_americas")
    champions = _upcoming("Champions", "2026-07-27T19:00:00+00:00", "Grand Final", "valorant")

    assignment, reserved_for, _overflow = assign_slots(
        live_matches=[americas],
        slots=2,
        league_priority=VALORANT_PRIORITY_WITH_INTL,
        previous_assignment=None,
        upcoming_matches=[champions],
        far_upcoming_matches=[dict(champions)],
    )

    assert assignment == [None, americas]
    assert reserved_for == [champions, None]


THREE_HOURS = timedelta(hours=3)


def test_project_schedule_returns_a_single_future_match_covering_its_own_slot():
    americas = _upcoming("VCT Americas", "2026-07-27T18:00:00+00:00", "Sentinels vs 100T", "valorant_americas")

    projected = project_schedule(matches=[americas], slots=1, league_priority=VALORANT_PRIORITY, duration=THREE_HOURS)

    assert projected == [[(_at(americas), americas)]]


def test_project_schedule_orders_back_to_back_matches_on_one_slot_chronologically():
    # Second match starts exactly when the first's 3h estimated block ends.
    # No overlap, so both should appear on the single slot in order.
    first = _upcoming("VCT Americas", "2026-07-27T14:00:00+00:00", "Sentinels vs 100T", "valorant_americas")
    second = _upcoming("VCT Americas", "2026-07-27T17:00:00+00:00", "LOUD vs NRG", "valorant_americas")

    projected = project_schedule(
        matches=[second, first],  # deliberately out of chronological order
        slots=1,
        league_priority=VALORANT_PRIORITY,
        duration=THREE_HOURS,
    )

    assert projected == [[(_at(first), first), (_at(second), second)]]


def test_project_schedule_drops_the_losing_match_entirely_when_two_leagues_start_at_the_same_time_with_only_one_slot():
    # Same start (and, with a uniform duration, same end) means Pacific never
    # gets a window where Americas isn't occupying the slot. It must be
    # fully absent from the projection, not queued for afterward.
    americas = _upcoming("VCT Americas", "2026-07-27T18:00:00+00:00", "Sentinels vs 100T", "valorant_americas")
    pacific = _upcoming("VCT Pacific", "2026-07-27T18:00:00+00:00", "Paper Rex vs DRX", "valorant_pacific")

    projected = project_schedule(
        matches=[americas, pacific], slots=1, league_priority=VALORANT_PRIORITY, duration=THREE_HOURS
    )

    assert projected == [[(_at(americas), americas)]]


def test_project_schedule_keeps_a_seeded_live_match_until_it_ends_even_if_a_higher_priority_match_starts_meanwhile():
    live_emea = _match("VCT EMEA", "2026-07-27T14:00:00+00:00", "Fnatic vs Team Liquid", "valorant_emea")
    # Champions starts an hour into EMEA's remaining block and runs well past
    # EMEA's own estimated end. Sticky assignment must still hold EMEA until
    # its own interval ends before Champions gets the slot.
    champions = _upcoming("Champions", "2026-07-27T15:00:00+00:00", "Grand Final", "valorant")

    # live_emea must be in `matches` too (mirroring how the real caller always
    # includes a currently in_progress match). project_schedule needs its own
    # interval to know when it actually ends; initial_assignment alone only
    # seeds the very first replay, it doesn't keep a match alive forever.
    projected = project_schedule(
        matches=[live_emea, champions],
        slots=1,
        league_priority=VALORANT_PRIORITY_WITH_INTL,
        duration=THREE_HOURS,
        initial_assignment=[live_emea],
    )

    # Champions is only actually claimed once EMEA's own interval ends
    # (17:00), an hour after Champions' own real start (15:00) -- it had to
    # wait, since EMEA was sticky-occupying the only slot until then.
    assert projected == [[(_at(live_emea), live_emea), (datetime.fromisoformat("2026-07-27T17:00:00+00:00"), champions)]]


def test_project_schedule_defaults_initial_assignment_to_empty_when_not_provided():
    americas = _upcoming("VCT Americas", "2026-07-27T18:00:00+00:00", "Sentinels vs 100T", "valorant_americas")

    projected = project_schedule(matches=[americas], slots=1, league_priority=VALORANT_PRIORITY, duration=THREE_HOURS)

    assert projected == [[(_at(americas), americas)]]


def test_project_schedule_claims_a_contended_match_only_once_its_slot_actually_frees_up():
    # Regression test for a real bug (2026-07-30), reconstructed from the
    # exact match data that triggered it: 3 matches share one Twitch channel
    # (valorant_emea) at both 15:00 and 18:00 -- VCT EMEA and Game Changers
    # EMEA air on the same regional channel -- so with only 2 slots, one
    # loses the contest each time but stays live until its own 3h estimate
    # ends. A 4th match (Shopify, a different channel entirely) starts at
    # 19:00, while both slots are still showing 18:00-started EMEA matches
    # until 21:00. It must not be displayed as claiming a slot at its own
    # 19:00 start -- that slot is still genuinely showing something else
    # until 21:00. The bug: displaying it at 19:00 anyway made it look like
    # it was overlapping whatever was still on-air.
    giantx_vs_liquid = _match("VCT EMEA", "2026-07-27T15:00:00+00:00", "GIANTX vs Team Liquid", "valorant_emea")
    gentle_mates_vs_vitality = _match("VCT EMEA", "2026-07-27T18:00:00+00:00", "Gentle Mates vs Team Vitality", "valorant_emea")
    sk_nebula_vs_g2_gozen = _match("Game Changers EMEA", "2026-07-27T15:00:00+00:00", "SK Nebula vs G2 Gozen", "valorant_emea")
    fokus_vs_gentle_mates = _match(
        "Game Changers EMEA", "2026-07-27T15:00:00+00:00", "FOKUS Sakura vs Gentle Mates", "valorant_emea"
    )
    habos_babos_vs_giantx = _match("Game Changers EMEA", "2026-07-27T18:00:00+00:00", "Habos Babos vs GIANTX", "valorant_emea")
    alternate_vs_barca = _match(
        "Game Changers EMEA", "2026-07-27T18:00:00+00:00", "ALTERNATE aTTaX Ruby vs Barca eSports", "valorant_emea"
    )
    # Unranked league (not in `priority` below), matching the real case --
    # sorts last on priority, but that's irrelevant once it's the only
    # candidate left for the only slot still open.
    shopify_vs_2game = _match(
        "Last Chance Qualifier Americas", "2026-07-27T19:00:00+00:00", "Shopify Rebellion Black vs 2GAME", "VALORANT_NorthAmerica"
    )

    priority = ["VCT EMEA", "Game Changers EMEA"]
    projected = project_schedule(
        matches=[
            giantx_vs_liquid,
            gentle_mates_vs_vitality,
            sk_nebula_vs_g2_gozen,
            fokus_vs_gentle_mates,
            habos_babos_vs_giantx,
            alternate_vs_barca,
            shopify_vs_2game,
        ],
        slots=2,
        league_priority=priority,
        duration=THREE_HOURS,
    )

    all_claims = [claim for slot in projected for claim in slot]
    shopify_claims = [claimed_at for claimed_at, match in all_claims if match is shopify_vs_2game]
    # Claimed exactly once, only once a slot genuinely frees up at 21:00 --
    # never at its own 19:00 start, while both slots are still showing
    # 18:00-started matches that don't end until 21:00.
    assert shopify_claims == [datetime.fromisoformat("2026-07-27T21:00:00+00:00")]

    # And no slot's entries ever overlap in time -- the actual guarantee
    # this bug broke. A slot's Nth entry must claim no earlier than the
    # (N-1)th entry's own real end.
    for slot in projected:
        for (_prev_claimed_at, prev_match), (claimed_at, _next_match) in zip(slot, slot[1:]):
            prev_end = datetime.fromisoformat(prev_match["start"]) + THREE_HOURS
            assert claimed_at >= prev_end


def test_project_schedule_on_no_matches_returns_empty_lists_per_slot():
    assert project_schedule(matches=[], slots=2, league_priority=VALORANT_PRIORITY, duration=THREE_HOURS) == [[], []]
