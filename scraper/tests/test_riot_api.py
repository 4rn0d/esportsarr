"""Tests for esports_schedule.riot_api — the normalization logic (Riot JSON ->
MatchEvent) is the part most likely to break silently if Riot changes field
names, so these assert on exact output shape rather than just "it didn't crash".
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
import responses

from esports_schedule.channel_map import UnknownLeagueError
from esports_schedule.models import Game, League, MatchState
from esports_schedule.riot_api import (
    LOL_HOST,
    VALORANT_HOST,
    fetch_matches_for_leagues,
)

LCS = League(display_name="LCS", game=Game.LOL, epg_channel_id="twitch.lcs")
VCT_AMERICAS = League(display_name="VCT Americas", game=Game.VALORANT, epg_channel_id="twitch.valorant_americas")


def _leagues_payload(entries: list[dict]) -> dict:
    return {"data": {"leagues": entries}}


def _schedule_payload(events: list[dict]) -> dict:
    return {"data": {"schedule": {"events": events}}}


@responses.activate
def test_fetch_matches_normalizes_two_team_match_with_twitch_stream():
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
                    "streams": [
                        {"provider": "twitch", "parameter": "lcs", "locale": "en-US"},
                        {"provider": "youtube", "parameter": "abc123", "locale": "en-US"},
                    ],
                }
            ]
        ),
        status=200,
    )

    [match] = fetch_matches_for_leagues([LCS], api_key="test-key")

    assert match.league is LCS
    assert match.state == MatchState.IN_PROGRESS
    assert match.title == "Sentinels vs Cloud9"
    assert match.twitch_channel == "lcs"
    assert match.start.year == 2026 and match.start.hour == 20


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
                    "streams": [],
                }
            ]
        ),
        status=200,
    )

    [match] = fetch_matches_for_leagues([LCS], api_key="test-key")

    # Title-building keys off whether exactly 2 team entries are present, not
    # on the team names' content — "TBD" placeholders still produce a normal
    # "X vs Y" title. The blockName fallback (tested below) only kicks in when
    # `match.teams` itself is missing or not exactly 2 entries.
    assert match.title == "TBD vs TBD"
    assert match.twitch_channel is None


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
                    "streams": [],
                }
            ]
        ),
        status=200,
    )

    [match] = fetch_matches_for_leagues([LCS], api_key="test-key")

    assert match.title == "LCS: Pre-Show"


@responses.activate
def test_fetch_matches_raises_when_league_not_found():
    responses.add(
        responses.GET,
        f"{LOL_HOST.base_url}/getLeagues",
        json=_leagues_payload([{"id": "999", "name": "LCK Challengers", "slug": "lck_challengers_league"}]),
        status=200,
    )

    with pytest.raises(UnknownLeagueError):
        fetch_matches_for_leagues([LCS], api_key="test-key")


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
