"""Tests for plugin._classify_matches, the live/upcoming bucketing that feeds
allocator.assign_slots. Every test uses real-shaped match dicts (as they'd
appear after JSON-decoding schedule.json) rather than stripped-down stand-ins."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from esportsarr.plugin import RESERVATION_GRACE_MINUTES, STALE_LIVE_GRACE_MINUTES, _classify_matches

GRACE = timedelta(minutes=RESERVATION_GRACE_MINUTES)
STALE_LIVE_GRACE = timedelta(minutes=STALE_LIVE_GRACE_MINUTES)
WIDE_LOOKAHEAD = timedelta(minutes=180)
NARROW_LOOKAHEAD = timedelta(minutes=120)


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
    return _classify_matches(matches, now, WIDE_LOOKAHEAD, NARROW_LOOKAHEAD, GRACE, STALE_LIVE_GRACE, now + timedelta(days=7))


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
        "Game Changers EMEA", "valorant", "2026-07-29T10:00:00+00:00", "unstarted", "Stale Match A vs Stale Match B", "valorant_emea"
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
