"""Tests for plan_builder.py's pure logic: is_genuinely_live, _classify_matches
(the live/upcoming bucketing that feeds allocator.assign_slots),
_combined_priority (tiered priority settings), _unranked_live_leagues
(priority-list validation), the gap-filling that resolves supplemental
content across the whole week-ahead plan instead of just "now", and the
plan's JSON persistence. Every test uses real-shaped match dicts (as they'd
appear after JSON-decoding schedule.json) rather than stripped-down
stand-ins."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from esportsarr.channel_sync import GUIDE_LOOKBACK_HOURS
from esportsarr.plan_builder import (
    RESERVATION_GRACE_MINUTES,
    STALE_LIVE_GRACE_MINUTES,
    _classify_matches,
    _combined_priority,
    _fill_game_projection_gaps,
    _find_gaps,
    _unranked_live_leagues,
    deserialize_plan,
    is_genuinely_live,
    is_supplemental,
    plan_is_stale,
    save_plan,
    load_plan,
    serialize_plan,
)

GRACE = timedelta(minutes=RESERVATION_GRACE_MINUTES)
STALE_LIVE_GRACE = timedelta(minutes=STALE_LIVE_GRACE_MINUTES)
WIDE_LOOKAHEAD = timedelta(minutes=180)
NARROW_LOOKAHEAD = timedelta(minutes=120)
HISTORY_WINDOW = timedelta(hours=GUIDE_LOOKBACK_HOURS)
NOW = datetime(2026, 7, 29, 15, 37, tzinfo=timezone.utc)


def _match(league: str, game: str, start: str, state: str, title: str, stream_channel: str) -> dict:
    return {
        "league": league,
        "game": game,
        "start": start,
        "state": state,
        "title": title,
        "stream_platform": "twitch",
        "stream_channel": stream_channel,
        "description": f"{league}: Week 1",
    }


def _classify(matches: list[dict], now: datetime):
    return _classify_matches(
        matches, now, WIDE_LOOKAHEAD, NARROW_LOOKAHEAD, GRACE, STALE_LIVE_GRACE, now + timedelta(days=7), HISTORY_WINDOW
    )


def test_is_genuinely_live_is_true_for_state_in_progress():
    match = _match("VCT EMEA", "valorant", "2026-07-29T15:00:00+00:00", "in_progress", "Team Liquid vs GIANTX", "valorant_emea")
    assert is_genuinely_live(match, NOW, STALE_LIVE_GRACE) is True


def test_is_genuinely_live_is_true_for_unstarted_past_its_start_within_grace():
    match = _match(
        "Game Changers EMEA", "valorant", "2026-07-29T15:00:00+00:00", "unstarted", "Karmine Corp vs Gentle Mates", "valorant_emea"
    )
    now = datetime(2026, 7, 29, 15, 37, tzinfo=timezone.utc)  # 37 minutes past its own start
    assert is_genuinely_live(match, now, STALE_LIVE_GRACE) is True


def test_is_genuinely_live_is_false_for_unstarted_before_its_start():
    match = _match("Game Changers EMEA", "valorant", "2026-07-29T15:00:00+00:00", "unstarted", "Karmine Corp vs Gentle Mates", "valorant_emea")
    now = datetime(2026, 7, 29, 14, 59, tzinfo=timezone.utc)
    assert is_genuinely_live(match, now, STALE_LIVE_GRACE) is False


def test_is_genuinely_live_is_false_once_past_the_stale_grace_window():
    match = _match("Game Changers EMEA", "valorant", "2026-07-29T15:00:00+00:00", "unstarted", "Karmine Corp vs Gentle Mates", "valorant_emea")
    now = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc) + STALE_LIVE_GRACE + timedelta(minutes=1)
    assert is_genuinely_live(match, now, STALE_LIVE_GRACE) is False


def test_is_genuinely_live_is_false_for_completed():
    match = _match("VCT EMEA", "valorant", "2026-07-29T15:00:00+00:00", "completed", "Team Liquid vs GIANTX", "valorant_emea")
    assert is_genuinely_live(match, NOW, STALE_LIVE_GRACE) is False


def test_is_genuinely_live_is_false_when_start_is_missing():
    match = {"league": "Game Changers EMEA", "state": "unstarted"}
    assert is_genuinely_live(match, NOW, STALE_LIVE_GRACE) is False


def test_in_progress_match_is_classified_live():
    now = datetime(2026, 7, 29, 15, 37, tzinfo=timezone.utc)
    giantx = _match("VCT EMEA", "valorant", "2026-07-29T15:00:00+00:00", "in_progress", "Team Liquid vs GIANTX", "valorant_emea")

    live, upcoming, far_upcoming, projectable = _classify([giantx], now)

    assert live["valorant"] == [giantx]
    assert "valorant" not in upcoming
    assert "valorant" not in far_upcoming
    assert projectable["valorant"] == [giantx]


def test_unstarted_match_past_its_start_time_is_treated_as_live_not_just_reserved():
    now = datetime(2026, 7, 29, 15, 37, tzinfo=timezone.utc)
    sk_nebula = _match(
        "Game Changers EMEA", "valorant", "2026-07-29T15:00:00+00:00", "unstarted", "SK Nebula vs G2 Gozen", "valorant_emea"
    )
    gentle_mates = _match(
        "Game Changers EMEA", "valorant", "2026-07-29T15:00:00+00:00", "unstarted", "Gentle Mates vs FOKUS Sakura", "valorant_emea"
    )

    live, upcoming, far_upcoming, projectable = _classify([sk_nebula, gentle_mates], now)

    assert live["valorant"] == [sk_nebula, gentle_mates]
    assert "valorant" not in upcoming
    assert "valorant" not in far_upcoming
    assert projectable["valorant"] == [sk_nebula, gentle_mates]


def test_unstarted_match_stuck_past_the_stale_live_grace_window_is_dropped_entirely():
    now = datetime(2026, 7, 29, 15, 37, tzinfo=timezone.utc)
    stale = _match(
        "Game Changers EMEA", "valorant", "2026-07-28T20:00:00+00:00", "unstarted", "Stale Match A vs Stale Match B", "valorant_emea"
    )

    live, upcoming, far_upcoming, projectable = _classify([stale], now)

    assert "valorant" not in live
    assert "valorant" not in upcoming
    assert "valorant" not in far_upcoming
    assert "valorant" not in projectable


def test_unstarted_match_within_narrow_lookahead_is_a_near_reservation_candidate():
    now = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
    lcq = _match(
        "Last Chance Qualifier Americas",
        "valorant",
        "2026-07-29T16:30:00+00:00",
        "unstarted",
        "Shopify Rebellion Black vs 2GAME",
        "VALORANT_NorthAmerica",
    )

    live, upcoming, far_upcoming, projectable = _classify([lcq], now)

    assert "valorant" not in live
    assert upcoming["valorant"] == [lcq]
    assert "valorant" not in far_upcoming
    assert projectable["valorant"] == [lcq]


def test_unstarted_match_beyond_narrow_but_within_wide_lookahead_is_a_far_candidate():
    now = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
    playoffs = _match("VCT EMEA", "valorant", "2026-07-29T17:30:00+00:00", "unstarted", "TBD vs TBD", "valorant_emea")

    live, upcoming, far_upcoming, projectable = _classify([playoffs], now)

    assert "valorant" not in live
    assert "valorant" not in upcoming
    assert far_upcoming["valorant"] == [playoffs]
    assert projectable["valorant"] == [playoffs]


def test_completed_match_within_the_history_window_is_projectable_but_never_live_or_upcoming():
    now = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
    rich_gang = _match("NLC", "lol", "2026-07-29T09:00:00+00:00", "completed", "Rich Gang vs Lund Esports Organization", "nlc")

    live, upcoming, far_upcoming, projectable = _classify([rich_gang], now)

    assert "lol" not in live
    assert "lol" not in upcoming
    assert "lol" not in far_upcoming
    assert projectable["lol"] == [rich_gang]


def test_completed_match_older_than_the_history_window_is_dropped_entirely():
    now = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
    stale = _match("NLC", "lol", "2026-07-28T20:00:00+00:00", "completed", "Old Match A vs Old Match B", "nlc")

    live, upcoming, far_upcoming, projectable = _classify([stale], now)

    assert "lol" not in live
    assert "lol" not in upcoming
    assert "lol" not in far_upcoming
    assert "lol" not in projectable


def test_match_missing_stream_channel_is_excluded_entirely():
    now = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
    placeholder = {
        "league": "Game Changers EMEA",
        "game": "valorant",
        "start": "2026-07-29T14:27:57.779000+00:00",
        "state": "in_progress",
        "title": "Game Changers EMEA",
        "stream_platform": None,
        "stream_channel": None,
        "description": "Game Changers EMEA",
    }

    live, upcoming, far_upcoming, projectable = _classify([placeholder], now)

    assert live == {}
    assert upcoming == {}
    assert far_upcoming == {}
    assert projectable == {}


def test_matches_across_multiple_games_are_bucketed_separately():
    now = datetime(2026, 7, 29, 15, 37, tzinfo=timezone.utc)
    lol_match = _match("LCK", "lol", "2026-07-29T15:00:00+00:00", "in_progress", "T1 vs kt Rolster", "lck")
    valorant_match = _match("VCT EMEA", "valorant", "2026-07-29T15:00:00+00:00", "in_progress", "Team Liquid vs GIANTX", "valorant_emea")

    live, _upcoming, _far_upcoming, _projectable = _classify([lol_match, valorant_match], now)

    assert live["lol"] == [lol_match]
    assert live["valorant"] == [valorant_match]


def test_combined_priority_concatenates_tiers_in_order():
    settings = {
        "league_priority_valorant_international": "Champions,VALORANT Masters",
        "league_priority_valorant_regional": "VCT Americas,VCT EMEA,VCT Pacific",
        "league_priority_valorant_qualifiers": "Last Chance Qualifier Americas",
    }
    tier_keys = [
        "league_priority_valorant_international",
        "league_priority_valorant_regional",
        "league_priority_valorant_qualifiers",
    ]

    assert _combined_priority(settings, tier_keys) == [
        "Champions",
        "VALORANT Masters",
        "VCT Americas",
        "VCT EMEA",
        "VCT Pacific",
        "Last Chance Qualifier Americas",
    ]


def test_combined_priority_skips_a_blank_tier():
    settings = {
        "league_priority_lol_international": "Worlds,MSI,First Stand",
        "league_priority_lol_regional": "LCS,LEC,LCK,LPL",
        "league_priority_lol_qualifiers": "",
    }
    tier_keys = ["league_priority_lol_international", "league_priority_lol_regional", "league_priority_lol_qualifiers"]

    assert _combined_priority(settings, tier_keys) == ["Worlds", "MSI", "First Stand", "LCS", "LEC", "LCK", "LPL"]


def test_unranked_live_leagues_is_empty_when_every_live_league_is_ranked():
    matches = [
        _match("VCT EMEA", "valorant", "2026-07-29T15:00:00+00:00", "in_progress", "Team Liquid vs GIANTX", "valorant_emea"),
    ]
    priority = ["VCT EMEA", "VCT Americas"]

    assert _unranked_live_leagues(matches, "valorant", priority) == []


def test_unranked_live_leagues_flags_a_typo_in_the_priority_list():
    matches = [
        _match(
            "Game Changers NA", "valorant", "2026-07-27T21:00:00+00:00", "in_progress", "Shopify Rebellion Gold vs SwimTrek Blue", "valorant_americas"
        ),
    ]
    priority = ["VCT Americas", "Game Changers Americas"]

    assert _unranked_live_leagues(matches, "valorant", priority) == ["Game Changers NA"]


def test_unranked_live_leagues_ignores_matches_from_a_different_game():
    matches = [
        _match("LPL", "lol", "2026-07-29T09:00:00+00:00", "in_progress", "TOP ESPORTS vs LGD GAMING", "LPL_English"),
    ]

    assert _unranked_live_leagues(matches, "valorant", []) == []


def test_unranked_live_leagues_deduplicates_and_sorts_multiple_unranked_leagues():
    matches = [
        _match("Game Changers EMEA", "valorant", "2026-07-29T15:00:00+00:00", "in_progress", "SK Nebula vs G2 Gozen", "valorant_emea"),
        _match(
            "Game Changers EMEA", "valorant", "2026-07-29T15:00:00+00:00", "in_progress", "Gentle Mates vs FOKUS Sakura", "valorant_emea"
        ),
        _match("Last Chance Qualifier EMEA", "valorant", "2026-07-08T15:00:00+00:00", "completed", "Cilekler vs MISA", "valorant_emea"),
    ]

    assert _unranked_live_leagues(matches, "valorant", []) == ["Game Changers EMEA", "Last Chance Qualifier EMEA"]


def test_is_supplemental_is_true_for_a_replay():
    assert is_supplemental({"is_replay": True, "league": "Replay"}) is True


def test_is_supplemental_is_true_for_plat_chat():
    assert is_supplemental({"is_replay": False, "league": "Plat Chat VALORANT"}) is True


def test_is_supplemental_is_false_for_a_real_match():
    assert is_supplemental({"is_replay": False, "league": "VCT Americas"}) is False


def _real_match_entry(claimed_at: datetime, league: str = "VCT Americas", best_of: int = 3) -> tuple[datetime, dict]:
    return (
        claimed_at,
        {
            "league": league,
            "game": "valorant",
            "start": claimed_at.isoformat(),
            "title": league,
            "best_of": best_of,
            "is_replay": False,
        },
    )


def test_find_gaps_returns_the_whole_window_for_an_empty_history():
    now = NOW
    projection_end = now + timedelta(days=1)

    assert _find_gaps([], now, projection_end) == [(now, projection_end)]


def test_find_gaps_finds_the_stretch_before_and_after_a_single_match():
    now = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)
    projection_end = now + timedelta(hours=6)
    match_start = now + timedelta(hours=1)
    history = [_real_match_entry(match_start, best_of=1)]  # 1h duration -> ends at now+2h

    gaps = _find_gaps(history, now, projection_end)

    assert gaps == [(now, match_start), (match_start + timedelta(hours=1), projection_end)]


def test_find_gaps_returns_nothing_when_matches_fully_cover_the_window():
    now = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)
    match_start = now
    projection_end = match_start + timedelta(hours=1)
    history = [_real_match_entry(match_start, best_of=1)]

    assert _find_gaps(history, now, projection_end) == []


def _replay_candidate(video_id: str, duration_seconds: int = 5400, title: str = "GIANTX vs Team Liquid") -> dict:
    return {"id": video_id, "title": title, "duration_seconds": duration_seconds}


def _live_plat_chat_schedule(real_start: datetime) -> dict:
    return {"video_id": "abc123", "topic": "Some topic", "episode": 274, "real_start": real_start.isoformat()}


def test_fill_game_projection_gaps_does_nothing_when_disabled():
    now = NOW
    projected_by_slot = [[]]
    settings = {"enable_supplemental_content": False}

    result = _fill_game_projection_gaps(
        "valorant",
        projected_by_slot,
        now,
        now + timedelta(days=1),
        settings,
        get_plat_chat_schedule=lambda now: (_ for _ in ()).throw(AssertionError("should never be called")),
    )

    assert result == [[]]


def test_fill_game_projection_gaps_leaves_real_matches_untouched():
    now = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)
    projection_end = now + timedelta(hours=1)
    entry = _real_match_entry(now, best_of=1)
    settings = {"enable_supplemental_content": True, "replay_channels_valorant": ""}

    result = _fill_game_projection_gaps(
        "valorant",
        [[entry]],
        now,
        projection_end,
        settings,
        get_plat_chat_schedule=lambda now: None,
        get_replay_candidates=lambda game, urls, now: [],
    )

    assert result == [[entry]]


def test_fill_game_projection_gaps_places_plat_chat_at_its_real_scheduled_window():
    now = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)
    projection_end = now + timedelta(days=2)
    real_start = now + timedelta(hours=5)
    schedule = _live_plat_chat_schedule(real_start)
    settings = {"enable_supplemental_content": True, "replay_channels_valorant": ""}

    result = _fill_game_projection_gaps(
        "valorant",
        [[]],
        now,
        projection_end,
        settings,
        get_plat_chat_schedule=lambda now: schedule,
        get_replay_candidates=lambda game, urls, now: [],
    )

    claimed_at, match = result[0][0]
    assert claimed_at == real_start
    assert match["league"] == "Plat Chat VALORANT"


def test_fill_game_projection_gaps_places_plat_chat_at_most_once_across_all_slots():
    now = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)
    projection_end = now + timedelta(hours=8)
    real_start = now + timedelta(hours=1)
    schedule = _live_plat_chat_schedule(real_start)
    settings = {"enable_supplemental_content": True, "replay_channels_valorant": ""}

    result = _fill_game_projection_gaps(
        "valorant",
        [[], [], []],
        now,
        projection_end,
        settings,
        get_plat_chat_schedule=lambda now: schedule,
        get_replay_candidates=lambda game, urls, now: [],
    )

    plat_chat_slots = [
        slot_index for slot_index, history in enumerate(result) for _claimed_at, match in history if match["league"] == "Plat Chat VALORANT"
    ]
    assert len(plat_chat_slots) == 1


def test_fill_game_projection_gaps_never_tries_plat_chat_for_lol():
    now = NOW
    projection_end = now + timedelta(hours=2)
    settings = {"enable_supplemental_content": True, "replay_channels_lol": "https://example.com/videos"}

    result = _fill_game_projection_gaps(
        "lol",
        [[]],
        now,
        projection_end,
        settings,
        get_plat_chat_schedule=lambda now: (_ for _ in ()).throw(AssertionError("should never be called for lol")),
        get_replay_candidates=lambda game, urls, now: [_replay_candidate("replay-1")],
    )

    assert result[0][0][1]["league"] == "Replay"


def test_fill_game_projection_gaps_chains_replays_back_to_back_to_cover_a_long_gap():
    now = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)
    projection_end = now + timedelta(hours=3)  # long enough for two 90-minute replays
    settings = {"enable_supplemental_content": True, "replay_channels_valorant": "https://example.com/videos"}
    candidates = [_replay_candidate("a", duration_seconds=5400), _replay_candidate("b", duration_seconds=5400)]

    result = _fill_game_projection_gaps(
        "valorant",
        [[]],
        now,
        projection_end,
        settings,
        get_plat_chat_schedule=lambda now: None,
        get_replay_candidates=lambda game, urls, now: candidates,
    )

    history = result[0]
    assert len(history) == 2
    assert history[0][0] == now
    assert history[1][0] == now + timedelta(seconds=5400)


def test_fill_game_projection_gaps_never_shows_the_same_replay_on_two_slots_the_same_day():
    now = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)
    projection_end = now + timedelta(hours=1)
    settings = {"enable_supplemental_content": True, "replay_channels_valorant": "https://example.com/videos"}
    candidates = [_replay_candidate("only-one")]

    result = _fill_game_projection_gaps(
        "valorant",
        [[], []],
        now,
        projection_end,
        settings,
        get_plat_chat_schedule=lambda now: None,
        get_replay_candidates=lambda game, urls, now: candidates,
    )

    filled_slots = [history for history in result if history]
    # The candidate pool only has one video -- it can cover at most one slot
    # for this exact window, the other must be left empty rather than
    # duplicating it (confirmed as a real bug, 2026-07-30: two Valorant
    # slots both showing the identical replay).
    assert len(filled_slots) == 1


def test_fill_game_projection_gaps_leaves_a_gap_unfilled_once_candidates_run_out():
    now = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)
    projection_end = now + timedelta(hours=3)
    settings = {"enable_supplemental_content": True, "replay_channels_valorant": "https://example.com/videos"}
    candidates = [_replay_candidate("only-one", duration_seconds=3600)]

    result = _fill_game_projection_gaps(
        "valorant",
        [[]],
        now,
        projection_end,
        settings,
        get_plat_chat_schedule=lambda now: None,
        get_replay_candidates=lambda game, urls, now: candidates,
    )

    assert len(result[0]) == 1  # only the one candidate placed, no infinite loop / crash


def test_plan_is_stale_is_true_for_a_missing_plan():
    assert plan_is_stale(None, NOW, timedelta(hours=24)) is True


def test_plan_is_stale_is_false_within_max_age():
    plan = {"built_at": (NOW - timedelta(hours=1)).isoformat()}
    assert plan_is_stale(plan, NOW, timedelta(hours=24)) is False


def test_plan_is_stale_is_true_once_past_max_age():
    plan = {"built_at": (NOW - timedelta(hours=25)).isoformat()}
    assert plan_is_stale(plan, NOW, timedelta(hours=24)) is True


def test_serialize_deserialize_plan_round_trips():
    plan = {
        "built_at": NOW.isoformat(),
        "priority_warnings": {"valorant": ["Game Changers EMEA"]},
        "games": {
            "valorant": [[(NOW, {"league": "VCT Americas", "title": "VCT Americas"})], []],
        },
    }

    round_tripped = deserialize_plan(serialize_plan(plan))

    assert round_tripped == plan


def test_save_and_load_plan_round_trips_through_disk(tmp_path):
    path = str(tmp_path / "weekly-plan.json")
    plan = {
        "built_at": NOW.isoformat(),
        "priority_warnings": {},
        "games": {"lol": [[(NOW, {"league": "LCK", "title": "LCK"})]]},
    }

    save_plan(plan, path)
    loaded = load_plan(path)

    assert loaded == plan


def test_load_plan_returns_none_when_the_file_does_not_exist(tmp_path):
    assert load_plan(str(tmp_path / "does-not-exist.json")) is None
