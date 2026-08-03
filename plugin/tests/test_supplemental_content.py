"""Tests for supplemental_content's pure logic. fetch_plat_chat_live_info and
fetch_replay_candidates themselves (real yt-dlp network calls) aren't tested
here, matching the pattern for stream_verification.fetch_twitch_stream_title
-- only the logic that consumes their output is."""

from __future__ import annotations

from datetime import datetime, timezone

from esportsarr.supplemental_content import (
    LIVE_CATEGORY,
    REPLAY_CATEGORY,
    _parse_episode,
    _thumbnail_url,
    build_replay_match,
    pick_replay,
)


def test_parse_episode_reads_the_trailing_ep_suffix():
    title = "The BEST teams in VCT right now are..? — Plat Chat VALORANT Ep. 274"
    topic, episode = _parse_episode(title)

    assert topic == "The BEST teams in VCT right now are..? — Plat Chat VALORANT"
    assert episode == 274


def test_parse_episode_accepts_the_word_episode_spelled_out():
    topic, episode = _parse_episode("Some topic - Plat Chat VALORANT Episode 42")

    assert episode == 42


def test_parse_episode_is_case_insensitive():
    topic, episode = _parse_episode("Some topic - ep. 7")

    assert episode == 7


def test_parse_episode_returns_none_when_theres_no_episode_suffix():
    topic, episode = _parse_episode("Just a regular video title")

    assert topic == "Just a regular video title"
    assert episode is None


def test_thumbnail_url_uses_the_video_id():
    assert _thumbnail_url("abc123") == "https://i.ytimg.com/vi/abc123/maxresdefault.jpg"


def test_pick_replay_is_deterministic_for_the_same_seed():
    candidates = [
        {"id": "a", "title": "Match A", "duration_seconds": 3000},
        {"id": "b", "title": "Match B", "duration_seconds": 4000},
        {"id": "c", "title": "Match C", "duration_seconds": 5000},
    ]

    first = pick_replay(candidates, seed="2026-07-30-valorant-0")
    second = pick_replay(candidates, seed="2026-07-30-valorant-0")

    assert first == second


def test_pick_replay_varies_with_a_different_seed():
    candidates = [
        {"id": "a", "title": "Match A", "duration_seconds": 3000},
        {"id": "b", "title": "Match B", "duration_seconds": 4000},
        {"id": "c", "title": "Match C", "duration_seconds": 5000},
        {"id": "d", "title": "Match D", "duration_seconds": 6000},
        {"id": "e", "title": "Match E", "duration_seconds": 7000},
    ]

    picks = {pick_replay(candidates, seed=f"2026-07-{day:02d}-valorant-0")["id"] for day in range(1, 11)}

    # Across 10 different days, a 5-item pool should not always land on the
    # exact same candidate -- proves the seed actually varies the pick.
    assert len(picks) > 1


def test_pick_replay_returns_none_for_an_empty_pool():
    assert pick_replay([], seed="2026-07-30-valorant-0") is None


def test_build_replay_match_uses_the_candidates_real_duration_and_replay_category():
    candidate = {"id": "xyz789", "title": "GIANTX vs Team Liquid - VCT EMEA Week 3", "duration_seconds": 9000}
    now = datetime(2026, 7, 30, 14, 0, tzinfo=timezone.utc)

    match = build_replay_match("valorant", "Replay", candidate, now)

    assert match["league"] == "Replay"
    assert match["game"] == "valorant"
    assert match["stream_platform"] == "youtube_vod"
    assert match["stream_channel"] == "xyz789"
    assert match["description"] == "GIANTX vs Team Liquid - VCT EMEA Week 3"
    assert match["duration_seconds"] == 9000
    assert match["category"] == REPLAY_CATEGORY
    assert match["icon"] == "https://i.ytimg.com/vi/xyz789/maxresdefault.jpg"
    assert match["start"] == now.isoformat()


def test_live_category_and_replay_category_are_distinct():
    assert LIVE_CATEGORY != REPLAY_CATEGORY
