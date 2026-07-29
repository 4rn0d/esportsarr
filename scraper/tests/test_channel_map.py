from __future__ import annotations

import pytest

from esportsarr.channel_map import TRACKED_LEAGUES, UnknownLeagueError, find_league, leagues_for_game
from esportsarr.models import Game


def test_find_league_returns_exact_name_match():
    league = find_league("LCK")
    assert league.epg_channel_id == "twitch.lck"


def test_find_league_does_not_substring_match_similarly_named_leagues():
    # Regression guard: "LCK Challengers" must never resolve when looking up
    # "LCK". A substring match here would silently point the wrong league's
    # matches at the LCK channel.
    with pytest.raises(UnknownLeagueError):
        find_league("LCK Challengers")


def test_leagues_for_game_splits_lol_and_valorant_correctly():
    lol_leagues = {league.display_name for league in leagues_for_game(Game.LOL)}
    valorant_leagues = {league.display_name for league in leagues_for_game(Game.VALORANT)}

    # Subset checks, not exact equality: TRACKED_LEAGUES is expected to grow
    # as leagues get added, and this test shouldn't need an edit every time
    # that happens. It only needs to catch a league ending up in the wrong
    # game's bucket.
    assert {"LCS", "LEC", "LCK"}.issubset(lol_leagues)
    assert {"VCT Americas", "VCT EMEA", "VCT Pacific"}.issubset(valorant_leagues)
    assert lol_leagues.isdisjoint(valorant_leagues)


def test_every_tracked_league_has_a_unique_display_name():
    # find_league does an exact first-match lookup by display_name. A
    # duplicate would silently shadow one of the two leagues. Multiple
    # leagues CAN share the same epg_channel_id on purpose (e.g. Game
    # Changers NA airing on the same Twitch channel as VCT Americas).
    display_names = [league.display_name for league in TRACKED_LEAGUES]
    assert len(display_names) == len(set(display_names))
