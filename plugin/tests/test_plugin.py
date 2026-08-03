"""Tests for plugin.py's live-tick pure helpers. The allocation policy
itself (classification, priority, gap-filling) now lives entirely in
plan_builder.py (see test_plan_builder.py) -- plugin.py's own job is just
looking up what the stored plan says is current (_current_occupant) and
reconciling that against live reality (_reconcile_with_reality), never
re-deciding allocation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from esportsarr import stream_verification
from esportsarr.plan_builder import STALE_LIVE_GRACE_MINUTES
from esportsarr.plugin import _current_occupant, _reconcile_with_reality

STALE_LIVE_GRACE = timedelta(minutes=STALE_LIVE_GRACE_MINUTES)
NOW = datetime(2026, 7, 29, 15, 37, tzinfo=timezone.utc)


def _match(
    league: str, start: str, state: str = "in_progress", best_of: int = 3, game: str = "valorant", match_id: str | None = None
) -> dict:
    return {
        "league": league,
        "game": game,
        "start": start,
        "state": state,
        "title": league,
        "best_of": best_of,
        "stream_platform": "twitch",
        "stream_channel": league.lower().replace(" ", "_"),
        "is_replay": False,
        "match_id": match_id,
    }


def _key(match: dict) -> tuple:
    return (match.get("match_id"), match.get("league"), match.get("start"))


def test_current_occupant_is_none_for_an_empty_history():
    assert _current_occupant([], NOW) is None


def test_current_occupant_is_none_before_the_first_entry_claims_the_slot():
    future = NOW + timedelta(hours=1)
    match = _match("VCT Americas", future.isoformat())
    assert _current_occupant([(future, match)], NOW) is None


def test_current_occupant_returns_the_most_recently_claimed_match_still_within_its_duration():
    claimed_at = NOW - timedelta(minutes=30)
    match = _match("VCT Americas", claimed_at.isoformat(), best_of=3)  # bo3 -> 3h duration
    assert _current_occupant([(claimed_at, match)], NOW) == match


def test_current_occupant_is_none_once_the_matchs_own_real_end_has_passed():
    # A gap plan_builder left unfilled (supplemental content disabled, or its
    # candidate pool ran out) must fall through to None, not keep showing a
    # long-finished match forever.
    claimed_at = NOW - timedelta(hours=5)
    match = _match("VCT Americas", claimed_at.isoformat(), best_of=1)  # bo1 -> 1h duration, long over by now

    assert _current_occupant([(claimed_at, match)], NOW) is None


def test_current_occupant_picks_the_latest_of_several_claimed_entries():
    earlier = NOW - timedelta(hours=2)
    later = NOW - timedelta(minutes=10)
    earlier_match = _match("VCT Americas", earlier.isoformat(), best_of=1)
    later_match = _match("VCT EMEA", later.isoformat(), best_of=3)

    result = _current_occupant([(earlier, earlier_match), (later, later_match)], NOW)

    assert result == later_match


def test_reconcile_with_reality_passes_through_none():
    assert _reconcile_with_reality(None, {}, NOW, STALE_LIVE_GRACE) is None


def test_reconcile_with_reality_passes_through_supplemental_content_untouched():
    replay = {"league": "Replay", "is_replay": True, "title": "LTA North"}
    assert _reconcile_with_reality(replay, {}, NOW, STALE_LIVE_GRACE) is replay

    plat_chat = {"league": "Plat Chat VALORANT", "is_replay": False, "title": "Plat Chat VALORANT"}
    assert _reconcile_with_reality(plat_chat, {}, NOW, STALE_LIVE_GRACE) is plat_chat


def test_reconcile_with_reality_marks_unstreamable_when_the_match_vanished_from_the_schedule():
    occupant = _match("VCT Americas", NOW.isoformat())
    # matches_by_key is empty -- the match that was in the plan is gone from
    # the freshly fetched schedule entirely (cancelled/removed).
    assert _reconcile_with_reality(occupant, {}, NOW, STALE_LIVE_GRACE) is None


def test_reconcile_with_reality_marks_unstreamable_when_the_fresh_state_is_completed():
    occupant = _match("VCT Americas", NOW.isoformat(), state="in_progress")
    fresh = _match("VCT Americas", NOW.isoformat(), state="completed")
    matches_by_key = {_key(fresh): fresh}

    assert _reconcile_with_reality(occupant, matches_by_key, NOW, STALE_LIVE_GRACE) is None


def test_reconcile_with_reality_returns_the_fresh_copy_for_an_ordinary_live_match():
    # VCT Americas isn't in stream_verification.LIVE_CHANNEL_CANDIDATES, so
    # no Twitch check should run -- just the fresh schedule data.
    occupant = _match("VCT Americas", NOW.isoformat(), state="unstarted")
    fresh = _match("VCT Americas", NOW.isoformat(), state="in_progress")
    matches_by_key = {_key(fresh): fresh}

    assert _reconcile_with_reality(occupant, matches_by_key, NOW, STALE_LIVE_GRACE) == fresh


def test_reconcile_with_reality_distinguishes_concurrent_same_league_same_start_matches_by_match_id():
    # Confirmed as a real bug, 2026-08-03: Game Changers EMEA regularly
    # schedules multiple concurrent matches with the identical start time --
    # (league, start) alone can't tell them apart, match_id can.
    occupant = _match("Game Changers EMEA", NOW.isoformat(), state="in_progress", match_id="match-b")
    other_fresh = _match("Game Changers EMEA", NOW.isoformat(), state="in_progress", match_id="match-a")
    this_fresh = _match("Game Changers EMEA", NOW.isoformat(), state="completed", match_id="match-b")
    matches_by_key = {_key(other_fresh): other_fresh, _key(this_fresh): this_fresh}

    # occupant's own match_id ("match-b") maps to the *completed* fresh copy,
    # not the other concurrent match still in progress under the same
    # league/start -- proves the lookup used match_id, not just (league, start).
    assert _reconcile_with_reality(occupant, matches_by_key, NOW, STALE_LIVE_GRACE) is None


def test_reconcile_with_reality_runs_stream_verification_for_a_genuinely_live_game_changers_match(monkeypatch):
    # _reconcile_with_reality hardcodes stream_verification.fetch_twitch_stream_title
    # (not an injectable param), so the real network call is monkeypatched
    # here rather than left to hit Twitch's GQL endpoint during a unit test.
    occupant = _match("Game Changers EMEA", NOW.isoformat(), state="in_progress")
    fresh = _match("Game Changers EMEA", NOW.isoformat(), state="in_progress")
    fresh["description"] = "Karmine Corp vs Gentle Mates"
    matches_by_key = {_key(fresh): fresh}

    monkeypatch.setattr(stream_verification, "fetch_twitch_stream_title", lambda channel: "Karmine Corp vs Gentle Mates")

    result = _reconcile_with_reality(occupant, matches_by_key, NOW, STALE_LIVE_GRACE)

    # The first candidate channel for Game Changers EMEA is "valorant_emea" --
    # proves verify_stream_channel actually ran and matched against it,
    # rather than _reconcile_with_reality just returning `fresh` untouched.
    assert result["stream_channel"] == "valorant_emea"


def test_reconcile_with_reality_marks_unstreamable_when_no_candidate_channel_title_matches(monkeypatch):
    occupant = _match("Game Changers EMEA", NOW.isoformat(), state="in_progress")
    fresh = _match("Game Changers EMEA", NOW.isoformat(), state="in_progress")
    fresh["description"] = "Karmine Corp vs Gentle Mates"
    matches_by_key = {_key(fresh): fresh}

    monkeypatch.setattr(stream_verification, "fetch_twitch_stream_title", lambda channel: "Some Unrelated Match")

    result = _reconcile_with_reality(occupant, matches_by_key, NOW, STALE_LIVE_GRACE)

    assert result["stream_platform"] is None
    assert result["stream_channel"] is None
