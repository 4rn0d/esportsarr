"""Tests for stream_verification's pure logic. fetch_twitch_stream_title
itself (the real network call) isn't tested here -- every test injects a
fake `fetch_title` callable instead, matching the duration_fn injection
pattern used elsewhere in this codebase."""

from __future__ import annotations

from esportsarr.stream_verification import (
    LIVE_CHANNEL_CANDIDATES,
    _extract_participants,
    _title_matches_participants,
    verify_stream_channel,
)


def _match(league: str, stream_channel: str, description: str) -> dict:
    return {
        "league": league,
        "game": "valorant",
        "start": "2026-07-29T15:00:00+00:00",
        "state": "in_progress",
        "title": league,
        "stream_platform": "twitch",
        "stream_channel": stream_channel,
        "description": description,
    }


def test_extract_participants_reads_the_leading_vs_segment():
    assert _extract_participants("SK Nebula vs G2 Gozen · Week 2 · Bo3") == ("SK Nebula", "G2 Gozen")


def test_extract_participants_returns_none_when_theres_no_vs_segment():
    assert _extract_participants("Week 2 · Bo3") is None
    assert _extract_participants("") is None


def test_title_matches_participants_is_case_insensitive():
    assert _title_matches_participants("SK NEBULA vs G2 Gozen | Game Changers", ("SK Nebula", "G2 Gozen"))


def test_title_matches_participants_requires_both_teams_present():
    assert not _title_matches_participants("SK Nebula vs Habos Babos", ("SK Nebula", "G2 Gozen"))


def test_verify_stream_channel_ignores_a_league_with_no_candidates():
    match = _match("VCT Americas", "valorant_americas", "Sentinels vs 100T · Week 3 · Bo3")

    result = verify_stream_channel(match, fetch_title=lambda channel: "should never be called")

    assert result == match


def test_verify_stream_channel_ignores_a_match_with_no_participants_in_its_description():
    match = _match("Game Changers NA", "valorant_americas", "Game Changers NA")

    result = verify_stream_channel(match, fetch_title=lambda channel: "should never be called")

    assert result == match


def test_verify_stream_channel_keeps_the_declared_channel_when_its_title_matches():
    match = _match("Game Changers NA", "valorant_americas", "Shopify Rebellion Gold vs SwimTrek Blue · Swiss · Bo3")

    def fetch_title(channel: str) -> str | None:
        return "Shopify Rebellion Gold vs SwimTrek Blue - Game Changers NA" if channel == "valorant_americas" else None

    result = verify_stream_channel(match, fetch_title)

    assert result["stream_platform"] == "twitch"
    assert result["stream_channel"] == "valorant_americas"


def test_verify_stream_channel_corrects_to_the_secondary_channel_when_thats_where_the_match_actually_is():
    # Regression test for the real concern: the declared channel is showing
    # a DIFFERENT concurrent Game Changers match, while this one actually
    # moved to a secondary channel Riot's schedule has no way to reflect.
    match = _match("Game Changers NA", "valorant_americas", "Shopify Rebellion Gold vs SwimTrek Blue · Swiss · Bo3")

    def fetch_title(channel: str) -> str | None:
        return {
            "valorant_americas": "Ora Temper vs SaD GC - Game Changers NA",
            "Raidiant_glow": "Shopify Rebellion Gold vs SwimTrek Blue - Game Changers NA",
        }.get(channel)

    result = verify_stream_channel(match, fetch_title)

    assert result["stream_platform"] == "twitch"
    assert result["stream_channel"] == "Raidiant_glow"


def test_verify_stream_channel_checks_candidates_in_order_stopping_at_the_first_match():
    calls: list[str] = []
    match = _match("Game Changers EMEA", "valorant_emea", "SK Nebula vs G2 Gozen · Week 2 · Bo3")

    def fetch_title(channel: str) -> str | None:
        calls.append(channel)
        return "SK Nebula vs G2 Gozen - Game Changers EMEA"

    verify_stream_channel(match, fetch_title)

    assert calls == ["valorant_emea"]  # never checked "remakeval", stopped at the first match


def test_verify_stream_channel_marks_unstreamable_when_no_candidate_matches():
    match = _match("Game Changers EMEA", "valorant_emea", "SK Nebula vs G2 Gozen · Week 2 · Bo3")

    def fetch_title(channel: str) -> str | None:
        return "Gentle Mates vs FOKUS Sakura - Game Changers EMEA"  # a different match entirely

    result = verify_stream_channel(match, fetch_title)

    assert result["stream_platform"] is None
    assert result["stream_channel"] is None


def test_verify_stream_channel_marks_unstreamable_when_every_candidate_is_offline():
    match = _match("Game Changers NA", "valorant_americas", "Shopify Rebellion Gold vs SwimTrek Blue · Swiss · Bo3")

    result = verify_stream_channel(match, fetch_title=lambda channel: None)

    assert result["stream_platform"] is None
    assert result["stream_channel"] is None


def test_live_channel_candidates_lists_the_declared_channel_first():
    # The schedule's own declared channel must be checked first so the
    # common case (nothing actually split) costs exactly one lookup.
    assert LIVE_CHANNEL_CANDIDATES["Game Changers NA"][0] == "valorant_americas"
    assert LIVE_CHANNEL_CANDIDATES["Game Changers EMEA"][0] == "valorant_emea"
