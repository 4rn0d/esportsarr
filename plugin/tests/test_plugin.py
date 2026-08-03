"""Tests for plugin.py's pure helpers: _classify_matches (the live/upcoming
bucketing that feeds allocator.assign_slots), _combined_priority (tiered
priority settings), and _unranked_live_leagues (priority-list validation).
Every test uses real-shaped match dicts (as they'd appear after
JSON-decoding schedule.json) rather than stripped-down stand-ins."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from esportsarr.channel_sync import GUIDE_LOOKBACK_HOURS
from esportsarr.plugin import (
    RESERVATION_GRACE_MINUTES,
    STALE_LIVE_GRACE_MINUTES,
    _classify_matches,
    _combined_priority,
    _fill_supplemental_content,
    _is_genuinely_live,
    _unranked_live_leagues,
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
    assert _is_genuinely_live(match, NOW, STALE_LIVE_GRACE) is True


def test_is_genuinely_live_is_true_for_unstarted_past_its_start_within_grace():
    # Regression test for a real bug (2026-07-30): a stream-verification
    # gate that checked state == "in_progress" directly (instead of this
    # shared helper) never ran for a Game Changers match stuck on
    # "unstarted" past its real start -- exactly the case this correction
    # exists for -- silently leaving it on the schedule's default declared
    # channel, identical to a different concurrent match on the same league
    # that verified correctly.
    match = _match(
        "Game Changers EMEA", "valorant", "2026-07-29T15:00:00+00:00", "unstarted", "Karmine Corp vs Gentle Mates", "valorant_emea"
    )
    now = datetime(2026, 7, 29, 15, 37, tzinfo=timezone.utc)  # 37 minutes past its own start
    assert _is_genuinely_live(match, now, STALE_LIVE_GRACE) is True


def test_is_genuinely_live_is_false_for_unstarted_before_its_start():
    match = _match("Game Changers EMEA", "valorant", "2026-07-29T15:00:00+00:00", "unstarted", "Karmine Corp vs Gentle Mates", "valorant_emea")
    now = datetime(2026, 7, 29, 14, 59, tzinfo=timezone.utc)
    assert _is_genuinely_live(match, now, STALE_LIVE_GRACE) is False


def test_is_genuinely_live_is_false_once_past_the_stale_grace_window():
    match = _match("Game Changers EMEA", "valorant", "2026-07-29T15:00:00+00:00", "unstarted", "Karmine Corp vs Gentle Mates", "valorant_emea")
    now = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc) + STALE_LIVE_GRACE + timedelta(minutes=1)
    assert _is_genuinely_live(match, now, STALE_LIVE_GRACE) is False


def test_is_genuinely_live_is_false_for_completed():
    match = _match("VCT EMEA", "valorant", "2026-07-29T15:00:00+00:00", "completed", "Team Liquid vs GIANTX", "valorant_emea")
    assert _is_genuinely_live(match, NOW, STALE_LIVE_GRACE) is False


def test_is_genuinely_live_is_false_when_start_is_missing():
    match = {"league": "Game Changers EMEA", "state": "unstarted"}
    assert _is_genuinely_live(match, NOW, STALE_LIVE_GRACE) is False


def test_in_progress_match_is_classified_live():
    now = datetime(2026, 7, 29, 15, 37, tzinfo=timezone.utc)
    giantx = _match("VCT EMEA", "valorant", "2026-07-29T15:00:00+00:00", "in_progress", "Team Liquid vs GIANTX", "valorant_emea")

    live, upcoming, far_upcoming, projectable = _classify([giantx], now)

    assert live["valorant"] == [giantx]
    assert "valorant" not in upcoming
    assert "valorant" not in far_upcoming
    assert projectable["valorant"] == [giantx]


def test_unstarted_match_past_its_start_time_is_treated_as_live_not_just_reserved():
    """Regression test: Riot marks some leagues (observed with Game Changers
    EMEA) 'unstarted' well after their real start while they're actually
    airing. Real match data from 2026-07-29: SK Nebula vs G2 Gozen and Gentle
    Mates vs FOKUS Sakura both started at 15:00 UTC but were still flagged
    'unstarted' in schedule.json fetched at 15:37 UTC, causing them to fall
    into the far-upcoming (preview-only, never displayed) bucket instead of
    actually showing on their channel."""
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
    """A match that never flips to 'completed' shouldn't squat a slot forever
    just because it's still 'unstarted' -- past STALE_LIVE_GRACE_MINUTES it's
    presumed stale data, and it's also too far in the past for the ordinary
    near/far reservation buckets (both share the same grace lower bound)."""
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
    playoffs = _match(
        "VCT EMEA", "valorant", "2026-07-29T17:30:00+00:00", "unstarted", "TBD vs TBD", "valorant_emea"
    )

    live, upcoming, far_upcoming, projectable = _classify([playoffs], now)

    assert "valorant" not in live
    assert "valorant" not in upcoming
    assert far_upcoming["valorant"] == [playoffs]
    assert projectable["valorant"] == [playoffs]


def test_completed_match_within_the_history_window_is_projectable_but_never_live_or_upcoming():
    # Regression test for a real bug (2026-07-29): completed matches were
    # never fed to the guide projection at all, so once a match ended, the
    # slot it occupied had zero historical record -- the guide couldn't
    # reconstruct what had actually aired there, only that nothing is live
    # there now.
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
    # Regression test for the real bug: "Game Changers Americas" was typed
    # into the priority setting instead of Riot's actual league name "Game
    # Changers NA". The typo itself never appears in schedule.json, but the
    # correct name does -- and since it's absent from `priority`, it's
    # exactly what should be flagged here.
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


def _replay_candidate(video_id: str, title: str = "GIANTX vs Team Liquid") -> dict:
    return {"id": video_id, "title": title, "duration_seconds": 9000}


def _live_plat_chat_schedule(now: datetime) -> dict:
    """A schedule dict (as returned by get_cached_plat_chat_schedule) that's
    genuinely airing right at `now`."""
    return {"video_id": "abc123", "topic": "Some topic", "episode": 274, "real_start": now.isoformat()}


def test_fill_supplemental_content_does_nothing_when_disabled():
    assignment = [None, None]
    settings = {"enable_supplemental_content": False}

    result = _fill_supplemental_content(
        "valorant",
        assignment,
        NOW,
        settings,
        get_plat_chat_schedule=lambda now: (_ for _ in ()).throw(AssertionError("should never be called")),
    )

    assert result == [None, None]


def test_fill_supplemental_content_leaves_a_real_match_untouched():
    real_match = {"league": "VCT Americas", "title": "VCT Americas"}
    assignment = [real_match, None]
    settings = {"enable_supplemental_content": True, "replay_channels_valorant": ""}

    result = _fill_supplemental_content(
        "valorant",
        assignment,
        NOW,
        settings,
        get_plat_chat_schedule=lambda now: None,
        get_replay_candidates=lambda game, urls, now: [],
    )

    assert result[0] is real_match


def test_fill_supplemental_content_prefers_plat_chat_for_an_empty_valorant_slot():
    assignment = [None]
    settings = {"enable_supplemental_content": True, "replay_channels_valorant": ""}

    result = _fill_supplemental_content(
        "valorant",
        assignment,
        NOW,
        settings,
        get_plat_chat_schedule=_live_plat_chat_schedule,
        get_replay_candidates=lambda game, urls, now: [_replay_candidate("should-not-be-picked")],
    )

    assert result[0]["league"] == "Plat Chat VALORANT"


def test_fill_supplemental_content_only_fills_one_slot_with_plat_chat_even_with_several_empty():
    assignment = [None, None]
    settings = {"enable_supplemental_content": True, "replay_channels_valorant": "https://example.com/videos"}

    result = _fill_supplemental_content(
        "valorant",
        assignment,
        NOW,
        settings,
        get_plat_chat_schedule=_live_plat_chat_schedule,
        get_replay_candidates=lambda game, urls, now: [_replay_candidate("replay-1")],
    )

    assert result[0]["league"] == "Plat Chat VALORANT"
    assert result[1]["league"] == "Replay"
    assert result[1]["stream_channel"] == "replay-1"


def test_fill_supplemental_content_falls_back_to_a_replay_when_plat_chat_is_not_live():
    assignment = [None]
    settings = {"enable_supplemental_content": True, "replay_channels_valorant": "https://example.com/videos"}

    result = _fill_supplemental_content(
        "valorant",
        assignment,
        NOW,
        settings,
        get_plat_chat_schedule=lambda now: None,
        get_replay_candidates=lambda game, urls, now: [_replay_candidate("replay-1")],
    )

    assert result[0]["league"] == "Replay"
    assert result[0]["game"] == "valorant"
    assert result[0]["stream_platform"] == "youtube_vod"
    assert result[0]["stream_channel"] == "replay-1"


def test_fill_supplemental_content_never_tries_plat_chat_for_lol():
    assignment = [None]
    settings = {"enable_supplemental_content": True, "replay_channels_lol": "https://example.com/videos"}

    result = _fill_supplemental_content(
        "lol",
        assignment,
        NOW,
        settings,
        get_plat_chat_schedule=lambda now: (_ for _ in ()).throw(AssertionError("should never be called for lol")),
        get_replay_candidates=lambda game, urls, now: [_replay_candidate("replay-1")],
    )

    assert result[0]["league"] == "Replay"


def test_fill_supplemental_content_reads_channels_from_the_right_game_setting():
    assignment = [None]
    settings = {
        "enable_supplemental_content": True,
        "replay_channels_lol": "https://example.com/lol-videos",
        "replay_channels_valorant": "https://example.com/valorant-videos",
    }
    seen: list[tuple[str, list[str]]] = []

    def get_replay_candidates(game: str, channel_urls: list[str], now: datetime) -> list[dict]:
        seen.append((game, channel_urls))
        return [_replay_candidate("replay-1")]

    _fill_supplemental_content(
        "lol", assignment, NOW, settings, get_plat_chat_schedule=lambda now: None, get_replay_candidates=get_replay_candidates
    )

    assert seen == [("lol", ["https://example.com/lol-videos"])]


def test_fill_supplemental_content_stays_stable_for_the_same_day_and_slot():
    assignment = [None]
    settings = {"enable_supplemental_content": True, "replay_channels_valorant": "https://example.com/videos"}
    candidates = [_replay_candidate("a"), _replay_candidate("b"), _replay_candidate("c"), _replay_candidate("d")]

    first = _fill_supplemental_content(
        "valorant",
        assignment,
        NOW,
        settings,
        get_plat_chat_schedule=lambda now: None,
        get_replay_candidates=lambda game, urls, now: candidates,
    )
    second = _fill_supplemental_content(
        "valorant",
        assignment,
        NOW,
        settings,
        get_plat_chat_schedule=lambda now: None,
        get_replay_candidates=lambda game, urls, now: candidates,
    )

    assert first[0]["stream_channel"] == second[0]["stream_channel"]


def test_fill_supplemental_content_returns_none_for_a_slot_when_no_replays_are_available():
    assignment = [None]
    settings = {"enable_supplemental_content": True, "replay_channels_valorant": "https://example.com/videos"}

    result = _fill_supplemental_content(
        "valorant",
        assignment,
        NOW,
        settings,
        get_plat_chat_schedule=lambda now: None,
        get_replay_candidates=lambda game, urls, now: []
    )

    assert result == [None]
