"""Tests for esportsarr.riot_api, the normalization logic (Riot JSON ->
MatchEvent) is the part most likely to break silently if Riot changes field
names, so these assert on exact output shape rather than just "it didn't crash".
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import responses

from esportsarr.models import Game, League, MatchState, StreamPlatform
from esportsarr.riot_api import (
    LOL_HOST,
    VALORANT_HOST,
    _match_description,
    _stream_identity_for_league,
    fetch_matches_for_leagues,
)

LCS = League(display_name="LCS", game=Game.LOL, epg_channel_id="twitch.lcs")
VCT_AMERICAS = League(display_name="VCT Americas", game=Game.VALORANT, epg_channel_id="twitch.valorant_americas")


def _leagues_payload(entries: list[dict]) -> dict:
    return {"data": {"leagues": entries}}


def _schedule_payload(events: list[dict]) -> dict:
    return {"data": {"schedule": {"events": events}}}


def test_stream_identity_for_league_strips_the_twitch_prefix():
    league = League(display_name="LCS", game=Game.LOL, epg_channel_id="twitch.lcs")
    assert _stream_identity_for_league(league) == (StreamPlatform.TWITCH, "lcs")


def test_stream_identity_for_league_strips_the_youtube_prefix():
    league = League(display_name="LPL", game=Game.LOL, epg_channel_id="youtube.LPL_English")
    assert _stream_identity_for_league(league) == (StreamPlatform.YOUTUBE, "LPL_English")


def test_stream_identity_for_league_returns_none_none_for_an_unrecognized_source():
    league = League(display_name="Made Up League", game=Game.LOL, epg_channel_id="dailymotion.whatever")
    assert _stream_identity_for_league(league) == (None, None)


def test_match_description_combines_league_and_block_name():
    assert _match_description({"blockName": "Playoffs"}, LCS) == "LCS: Playoffs"


def test_match_description_falls_back_to_league_name_when_block_name_is_absent():
    assert _match_description({}, LCS) == "LCS"


@responses.activate
def test_fetch_matches_normalizes_two_team_match():
    responses.add(
        responses.GET,
        f"{LOL_HOST.base_url}/getLeagues",
        json=_leagues_payload([{"id": "111", "name": "LCS", "slug": "lcs"}]),
        status=200,
    )
    responses.add(
        responses.GET,
        f"{LOL_HOST.base_url}/getSchedule",
        json=_schedule_payload(
            [
                {
                    "startTime": "2026-07-27T20:00:00Z",
                    "state": "inProgress",
                    "blockName": "Playoffs",
                    "match": {"teams": [{"name": "Sentinels"}, {"name": "Cloud9"}]},
                }
            ]
        ),
        status=200,
    )

    [match] = fetch_matches_for_leagues([LCS], api_key="test-key")

    assert match.league is LCS
    assert match.state == MatchState.IN_PROGRESS
    assert match.title == "Sentinels vs Cloud9"
    # Derived from LCS.epg_channel_id ("twitch.lcs"), not from Riot's own
    # (unreliable, see _stream_identity_for_league) per-event stream data.
    assert match.stream_platform == StreamPlatform.TWITCH
    assert match.stream_channel == "lcs"
    assert match.start.year == 2026 and match.start.hour == 20
    # Title uses "vs" since it's a real 2-team match, but the description
    # still surfaces the competition/stage context separately either way.
    assert match.description == "LCS: Playoffs"


@responses.activate
def test_fetch_matches_titles_tbd_placeholder_teams_normally():
    responses.add(
        responses.GET,
        f"{LOL_HOST.base_url}/getLeagues",
        json=_leagues_payload([{"id": "111", "name": "LCS", "slug": "lcs"}]),
        status=200,
    )
    responses.add(
        responses.GET,
        f"{LOL_HOST.base_url}/getSchedule",
        json=_schedule_payload(
            [
                {
                    "startTime": "2026-08-01T00:00:00Z",
                    "state": "unstarted",
                    "blockName": "Swiss",
                    "match": {"teams": [{"name": "TBD"}, {"name": "TBD"}]},
                }
            ]
        ),
        status=200,
    )

    [match] = fetch_matches_for_leagues([LCS], api_key="test-key")

    # Title-building keys off whether exactly 2 team entries are present, not
    # on the team names' content. "TBD" placeholders still produce a normal
    # "X vs Y" title. The blockName fallback (tested below) only kicks in when
    # `match.teams` itself is missing or not exactly 2 entries.
    assert match.title == "TBD vs TBD"


@responses.activate
def test_fetch_matches_uses_block_name_when_match_key_is_absent():
    responses.add(
        responses.GET,
        f"{LOL_HOST.base_url}/getLeagues",
        json=_leagues_payload([{"id": "111", "name": "LCS", "slug": "lcs"}]),
        status=200,
    )
    responses.add(
        responses.GET,
        f"{LOL_HOST.base_url}/getSchedule",
        json=_schedule_payload(
            [
                {
                    "startTime": "2026-08-01T00:00:00Z",
                    "state": "unstarted",
                    "blockName": "Pre-Show",
                }
            ]
        ),
        status=200,
    )

    [match] = fetch_matches_for_leagues([LCS], api_key="test-key")

    assert match.title == "LCS: Pre-Show"


@responses.activate
def test_fetch_matches_skips_unknown_league_but_keeps_others_in_the_same_game():
    # Regression guard for a real incident: a typo'd/renamed league used to
    # raise and abort every other league sharing its game host. A single bad
    # TRACKED_LEAGUES entry shouldn't break leagues that still work fine.
    renamed_league = League(display_name="LCK Challengers", game=Game.LOL, epg_channel_id="twitch.lck_challengers")

    responses.add(
        responses.GET,
        f"{LOL_HOST.base_url}/getLeagues",
        json=_leagues_payload([{"id": "111", "name": "LCS", "slug": "lcs"}]),
        status=200,
    )
    responses.add(
        responses.GET,
        f"{LOL_HOST.base_url}/getSchedule",
        json=_schedule_payload(
            [
                {
                    "startTime": "2026-07-27T20:00:00Z",
                    "state": "inProgress",
                    "blockName": "Playoffs",
                    "match": {"teams": [{"name": "Sentinels"}, {"name": "Cloud9"}]},
                }
            ]
        ),
        status=200,
    )

    matches = fetch_matches_for_leagues([LCS, renamed_league], api_key="test-key")

    # Only one getSchedule call: the unknown league is skipped before ever
    # requesting its schedule, not just filtered out afterward.
    assert len(responses.calls) == 2
    assert len(matches) == 1
    assert matches[0].league is LCS


@responses.activate
def test_valorant_requests_include_sport_param():
    responses.add(
        responses.GET,
        f"{VALORANT_HOST.base_url}/getLeagues",
        json=_leagues_payload([{"id": "222", "name": "VCT Americas", "slug": "vct_americas"}]),
        status=200,
    )
    responses.add(
        responses.GET,
        f"{VALORANT_HOST.base_url}/getSchedule",
        json=_schedule_payload([]),
        status=200,
    )

    fetch_matches_for_leagues([VCT_AMERICAS], api_key="test-key")

    assert len(responses.calls) == 2
    for call in responses.calls:
        query = parse_qs(urlparse(call.request.url).query)
        assert query["sport"] == ["val"]
