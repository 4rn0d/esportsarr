"""Tests for supplemental_content's pure logic. fetch_plat_chat_schedule and
fetch_replay_candidates themselves (real yt-dlp network calls) aren't tested
here, matching the pattern for stream_verification.fetch_twitch_stream_title
-- only the logic that consumes their output is. The caching wrappers
(get_cached_plat_chat_schedule/get_cached_replay_candidates) accept an
injectable `fetch` the same way, so they're tested with a fake fetch and a
temp cache file instead of real yt-dlp/production paths."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import esportsarr.supplemental_content as supplemental_content
from esportsarr.supplemental_content import (
    KNOWN_REPLAY_LEAGUES,
    MIN_REPLAY_DURATION_SECONDS,
    NEGATIVE_CACHE_TTL,
    PLAT_CHAT_DURATION_SECONDS,
    SUPPLEMENTAL_CATEGORIES,
    _extract_replay_league,
    _is_cache_entry_fresh,
    _parse_episode,
    _thumbnail_url,
    build_replay_match,
    fetch_replay_candidates,
    get_cached_plat_chat_schedule,
    get_cached_replay_candidates,
    pick_replay,
    plat_chat_match_if_live,
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


def test_extract_replay_league_finds_a_known_league_as_a_substring():
    title = "FLY v C9 - PLAYOFFS 2025 LTA North Split 2 - W11D2 - Game 05"
    assert _extract_replay_league(title, KNOWN_REPLAY_LEAGUES["lol"]) == "LTA North"


def test_extract_replay_league_is_case_insensitive():
    title = "g2 v mkoi | 2025 lec spring playoffs | grand final"
    assert _extract_replay_league(title, KNOWN_REPLAY_LEAGUES["lol"]) == "LEC"


def test_extract_replay_league_returns_none_when_nothing_recognized():
    assert _extract_replay_league("Some totally unrelated video title", KNOWN_REPLAY_LEAGUES["lol"]) is None


def test_extract_replay_league_prefers_the_more_specific_name_listed_first():
    # "LTA North" is listed before a hypothetical bare "LTA" would be --
    # regression guard against a future edit reordering the list and
    # silently matching a shorter/less specific name first.
    title = "Team A vs Team B - LTA North Split 1"
    assert _extract_replay_league(title, KNOWN_REPLAY_LEAGUES["lol"]) == "LTA North"


def test_build_replay_match_shortens_the_title_to_the_recognized_league_moving_full_details_to_description():
    # Arnaud, 2026-07-30: "the title of lol1 should only be LTA North and
    # the rest be the description."
    candidate = {"id": "abc123", "title": "FLY v C9 - PLAYOFFS 2025 LTA North Split 2 - W11D2 - Game 05", "duration_seconds": 9000}
    now = datetime(2026, 7, 30, 14, 0, tzinfo=timezone.utc)

    match = build_replay_match("lol", "Replay", candidate, now)

    assert match["title"] == "LTA North"
    assert match["description"] == "FLY v C9 - PLAYOFFS 2025 LTA North Split 2 - W11D2 - Game 05"


def test_build_replay_match_falls_back_to_the_full_title_when_no_known_league_is_recognized():
    candidate = {"id": "xyz789", "title": "Some totally unrelated video title", "duration_seconds": 9000}
    now = datetime(2026, 7, 30, 14, 0, tzinfo=timezone.utc)

    match = build_replay_match("lol", "Replay", candidate, now)

    assert match["title"] == "Some totally unrelated video title"
    assert match["description"] == ""


def test_build_replay_match_carries_through_the_rest_of_the_fields():
    # "This is a rerun" is the standard <previously-shown/> tag (is_replay),
    # never a category string -- the literal word "Replay" never appears in
    # the displayed title or description.
    candidate = {"id": "xyz789", "title": "GIANTX vs Team Liquid - VCT EMEA Week 3", "duration_seconds": 9000}
    now = datetime(2026, 7, 30, 14, 0, tzinfo=timezone.utc)

    match = build_replay_match("valorant", "Replay", candidate, now)

    assert match["league"] == "Replay"
    assert match["game"] == "valorant"
    assert match["stream_platform"] == "youtube_vod"
    assert match["stream_channel"] == "xyz789"
    assert match["title"] == "VCT EMEA"
    assert match["description"] == "GIANTX vs Team Liquid - VCT EMEA Week 3"
    assert match["duration_seconds"] == 9000
    assert match["categories"] == SUPPLEMENTAL_CATEGORIES
    assert match["is_replay"] is True
    assert match["icon"] == "https://i.ytimg.com/vi/xyz789/maxresdefault.jpg"
    assert match["start"] == now.isoformat()


def test_supplemental_categories_excludes_sports():
    # Neither Plat Chat nor a replay is itself a live sports match, so
    # neither gets the "Sports" category real matches default to
    # (channel_sync.DEFAULT_CATEGORIES).
    assert "Sports" not in SUPPLEMENTAL_CATEGORIES


def _schedule(now: datetime, video_id: str = "abc123", episode: int | None = 274) -> dict:
    return {"video_id": video_id, "topic": "Some topic", "episode": episode, "real_start": now.isoformat()}


def test_plat_chat_match_if_live_returns_none_for_no_schedule():
    now = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)
    assert plat_chat_match_if_live(None, now) is None


def test_plat_chat_match_if_live_returns_none_before_the_real_start():
    start = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)
    now = start - timedelta(minutes=1)
    assert plat_chat_match_if_live(_schedule(start), now) is None


def test_plat_chat_match_if_live_returns_a_match_within_the_live_window():
    start = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)
    now = start + timedelta(hours=1)  # well within the 3.5h duration estimate

    match = plat_chat_match_if_live(_schedule(start), now)

    assert match is not None
    assert match["league"] == "Plat Chat VALORANT"
    assert match["start"] == now.isoformat()
    assert match["episode_num"] == 274
    assert match["description"] == "Some topic · Episode 274"


def test_plat_chat_match_if_live_returns_none_past_the_duration_estimate():
    # Cached schedules can be reused for up to CACHE_TTL, so "is it still
    # airing" must be derived from real_start + duration, not trusted from
    # whenever the schedule was originally fetched -- otherwise a cached
    # "was live" schedule would incorrectly look live forever.
    start = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)
    now = start + timedelta(seconds=PLAT_CHAT_DURATION_SECONDS) + timedelta(minutes=1)

    assert plat_chat_match_if_live(_schedule(start), now) is None


def test_plat_chat_match_if_live_omits_episode_num_when_no_episode_was_parsed():
    start = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)
    now = start + timedelta(minutes=1)

    match = plat_chat_match_if_live(_schedule(start, episode=None), now)

    assert match["episode_num"] is None
    assert match["description"] == "Some topic"


def test_is_cache_entry_fresh_is_false_for_a_missing_entry():
    now = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)
    assert _is_cache_entry_fresh(None, now, timedelta(hours=24)) is False


def test_is_cache_entry_fresh_is_true_within_the_ttl():
    now = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)
    entry = {"fetched_at": (now - timedelta(hours=1)).isoformat()}
    assert _is_cache_entry_fresh(entry, now, timedelta(hours=24)) is True


def test_is_cache_entry_fresh_is_false_once_past_the_ttl():
    now = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)
    entry = {"fetched_at": (now - timedelta(hours=25)).isoformat()}
    assert _is_cache_entry_fresh(entry, now, timedelta(hours=24)) is False


def test_get_cached_replay_candidates_fetches_and_persists_on_a_cold_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(supplemental_content, "CACHE_FILE_PATH", str(tmp_path / "cache.json"))
    now = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)
    calls: list[str] = []

    def fake_fetch(channel_url: str) -> list[dict]:
        calls.append(channel_url)
        return [{"id": "v1", "title": "T", "duration_seconds": 100}]

    result = get_cached_replay_candidates("lol", ["https://example.com/a"], now, fetch=fake_fetch)

    assert result == [{"id": "v1", "title": "T", "duration_seconds": 100}]
    assert calls == ["https://example.com/a"]


def test_get_cached_replay_candidates_reuses_a_fresh_cache_without_refetching(tmp_path, monkeypatch):
    monkeypatch.setattr(supplemental_content, "CACHE_FILE_PATH", str(tmp_path / "cache.json"))
    now = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)
    calls: list[str] = []

    def fake_fetch(channel_url: str) -> list[dict]:
        calls.append(channel_url)
        return [{"id": "v1", "title": "T", "duration_seconds": 100}]

    get_cached_replay_candidates("lol", ["https://example.com/a"], now, fetch=fake_fetch)
    result = get_cached_replay_candidates("lol", ["https://example.com/a"], now + timedelta(hours=1), fetch=fake_fetch)

    assert result == [{"id": "v1", "title": "T", "duration_seconds": 100}]
    assert calls == ["https://example.com/a"]  # only the first call actually fetched


def test_get_cached_replay_candidates_refetches_once_the_cache_is_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(supplemental_content, "CACHE_FILE_PATH", str(tmp_path / "cache.json"))
    now = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)
    calls: list[str] = []

    def fake_fetch(channel_url: str) -> list[dict]:
        calls.append(channel_url)
        return [{"id": "v1", "title": "T", "duration_seconds": 100}]

    get_cached_replay_candidates("lol", ["https://example.com/a"], now, fetch=fake_fetch)
    get_cached_replay_candidates("lol", ["https://example.com/a"], now + timedelta(hours=25), fetch=fake_fetch)

    assert calls == ["https://example.com/a", "https://example.com/a"]


def test_get_cached_replay_candidates_caches_separately_per_game(tmp_path, monkeypatch):
    monkeypatch.setattr(supplemental_content, "CACHE_FILE_PATH", str(tmp_path / "cache.json"))
    now = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)

    get_cached_replay_candidates("lol", ["https://example.com/lol"], now, fetch=lambda url: [{"id": "lol-vod", "title": "T", "duration_seconds": 100}])
    valorant_result = get_cached_replay_candidates(
        "valorant", ["https://example.com/valorant"], now, fetch=lambda url: [{"id": "valorant-vod", "title": "T", "duration_seconds": 100}]
    )

    assert valorant_result == [{"id": "valorant-vod", "title": "T", "duration_seconds": 100}]


def test_get_cached_plat_chat_schedule_fetches_and_persists_on_a_cold_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(supplemental_content, "CACHE_FILE_PATH", str(tmp_path / "cache.json"))
    now = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)
    schedule = _schedule(now)

    result = get_cached_plat_chat_schedule(now, fetch=lambda: schedule)

    assert result == schedule


def test_get_cached_plat_chat_schedule_reuses_a_fresh_cache_without_refetching(tmp_path, monkeypatch):
    monkeypatch.setattr(supplemental_content, "CACHE_FILE_PATH", str(tmp_path / "cache.json"))
    now = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)
    calls = []

    def fake_fetch():
        calls.append(1)
        return _schedule(now)

    get_cached_plat_chat_schedule(now, fetch=fake_fetch)
    get_cached_plat_chat_schedule(now + timedelta(hours=1), fetch=fake_fetch)

    assert len(calls) == 1


def test_get_cached_plat_chat_schedule_refetches_a_none_result_after_the_short_negative_ttl(tmp_path, monkeypatch):
    # A cold check that happens to run before an episode is announced must
    # not stay blind to it for the full 24h CACHE_TTL -- confirmed as a real
    # bug, 2026-07-30 (Plat Chat never showing up because the first check
    # found nothing and that null result was trusted all day).
    monkeypatch.setattr(supplemental_content, "CACHE_FILE_PATH", str(tmp_path / "cache.json"))
    now = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)
    calls = []

    def fake_fetch_none():
        calls.append(1)
        return None

    get_cached_plat_chat_schedule(now, fetch=fake_fetch_none)
    get_cached_plat_chat_schedule(now + NEGATIVE_CACHE_TTL + timedelta(minutes=1), fetch=fake_fetch_none)

    assert len(calls) == 2


def test_get_cached_plat_chat_schedule_keeps_trusting_a_none_result_within_the_negative_ttl(tmp_path, monkeypatch):
    monkeypatch.setattr(supplemental_content, "CACHE_FILE_PATH", str(tmp_path / "cache.json"))
    now = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)
    calls = []

    def fake_fetch_none():
        calls.append(1)
        return None

    get_cached_plat_chat_schedule(now, fetch=fake_fetch_none)
    result = get_cached_plat_chat_schedule(now + NEGATIVE_CACHE_TTL - timedelta(minutes=1), fetch=fake_fetch_none)

    assert result is None
    assert len(calls) == 1


def test_get_cached_plat_chat_schedule_still_trusts_a_real_result_for_the_full_ttl(tmp_path, monkeypatch):
    # A genuine find has known, fixed timing -- it should NOT get demoted to
    # the short negative TTL just because a later call happens to reuse it.
    monkeypatch.setattr(supplemental_content, "CACHE_FILE_PATH", str(tmp_path / "cache.json"))
    now = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)
    schedule = _schedule(now)
    calls = []

    def fake_fetch():
        calls.append(1)
        return schedule

    get_cached_plat_chat_schedule(now, fetch=fake_fetch)
    result = get_cached_plat_chat_schedule(now + NEGATIVE_CACHE_TTL + timedelta(minutes=1), fetch=fake_fetch)

    assert result == schedule
    assert len(calls) == 1


def test_get_cached_replay_candidates_refetches_an_empty_result_after_the_short_negative_ttl(tmp_path, monkeypatch):
    monkeypatch.setattr(supplemental_content, "CACHE_FILE_PATH", str(tmp_path / "cache.json"))
    now = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)
    calls: list[str] = []

    def fake_fetch_empty(channel_url: str) -> list[dict]:
        calls.append(channel_url)
        return []

    get_cached_replay_candidates("lol", ["https://example.com/a"], now, fetch=fake_fetch_empty)
    get_cached_replay_candidates(
        "lol", ["https://example.com/a"], now + NEGATIVE_CACHE_TTL + timedelta(minutes=1), fetch=fake_fetch_empty
    )

    assert calls == ["https://example.com/a", "https://example.com/a"]


def test_get_cached_replay_candidates_keeps_trusting_a_real_result_past_the_negative_ttl(tmp_path, monkeypatch):
    monkeypatch.setattr(supplemental_content, "CACHE_FILE_PATH", str(tmp_path / "cache.json"))
    now = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)
    calls: list[str] = []

    def fake_fetch(channel_url: str) -> list[dict]:
        calls.append(channel_url)
        return [{"id": "v1", "title": "T", "duration_seconds": 100}]

    get_cached_replay_candidates("lol", ["https://example.com/a"], now, fetch=fake_fetch)
    get_cached_replay_candidates(
        "lol", ["https://example.com/a"], now + NEGATIVE_CACHE_TTL + timedelta(minutes=1), fetch=fake_fetch
    )

    assert calls == ["https://example.com/a"]  # only the cold-cache call actually fetched


class _FakeYoutubeDL:
    def __init__(self, entries):
        self._entries = entries

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def extract_info(self, url, download=False):
        return {"entries": self._entries}


def test_fetch_replay_candidates_excludes_entries_with_no_duration(monkeypatch):
    entries = [
        {"id": "live1", "title": "Still live", "duration": None},
        {"id": "full1", "title": "Full match", "duration": MIN_REPLAY_DURATION_SECONDS + 60},
    ]
    monkeypatch.setattr(supplemental_content.yt_dlp, "YoutubeDL", lambda opts: _FakeYoutubeDL(entries))

    candidates = fetch_replay_candidates("https://example.com/channel")

    assert [c["id"] for c in candidates] == ["full1"]


def test_fetch_replay_candidates_excludes_short_clips_below_the_minimum_duration(monkeypatch):
    # Confirmed as a real bug, 2026-07-30: clips as short as ~12 minutes were
    # being shown as full replay blocks in the guide.
    entries = [
        {"id": "clip1", "title": "Short clip", "duration": 12 * 60},
        {"id": "clip2", "title": "Highlight reel", "duration": 20 * 60},
        {"id": "full1", "title": "Full VOD", "duration": MIN_REPLAY_DURATION_SECONDS + 1},
    ]
    monkeypatch.setattr(supplemental_content.yt_dlp, "YoutubeDL", lambda opts: _FakeYoutubeDL(entries))

    candidates = fetch_replay_candidates("https://example.com/channel")

    assert [c["id"] for c in candidates] == ["full1"]


def test_fetch_replay_candidates_includes_an_entry_exactly_at_the_minimum_duration(monkeypatch):
    entries = [{"id": "exact1", "title": "Exactly at threshold", "duration": MIN_REPLAY_DURATION_SECONDS}]
    monkeypatch.setattr(supplemental_content.yt_dlp, "YoutubeDL", lambda opts: _FakeYoutubeDL(entries))

    candidates = fetch_replay_candidates("https://example.com/channel")

    assert [c["id"] for c in candidates] == ["exact1"]
