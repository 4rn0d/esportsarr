from __future__ import annotations

import pytest

from esports_schedule.channel_map import TRACKED_LEAGUES, UnknownLeagueError, find_league, leagues_for_game
from esports_schedule.models import Game


def test_find_league_returns_exact_name_match():
    league = find_league("LCK")
    assert league.epg_channel_id == "twitch.lck"


def test_find_league_does_not_substring_match_similarly_named_leagues():
    # Regression guard: "LCK Challengers" must never resolve when looking up
    # "LCK" — a substring match here would silently point the wrong league's
    # matches at the LCK channel.
    with pytest.raises(UnknownLeagueError):
        find_league("LCK Challengers")


def test_leagues_for_game_splits_lol_and_valorant_correctly():
    lol_leagues = {league.display_name for league in leagues_for_game(Game.LOL)}
    valorant_leagues = {league.display_name for league in leagues_for_game(Game.VALORANT)}

    assert lol_leagues == {"LCS", "LEC", "LCK"}
    assert valorant_leagues == {"VCT Americas", "VCT EMEA", "VCT Pacific"}
    assert lol_leagues.isdisjoint(valorant_leagues)


def test_every_tracked_league_has_a_unique_epg_channel_id():
    channel_ids = [league.epg_channel_id for league in TRACKED_LEAGUES]
    assert len(channel_ids) == len(set(channel_ids))
