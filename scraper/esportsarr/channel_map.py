"""Which leagues we track and which existing Dispatcharr/Twitcharr EPG channel
each one feeds. Matching is by exact Riot API league `name` (not substring —
e.g. "LCK" vs "LCK Challengers" must not collide).
"""

from __future__ import annotations

from .models import Game, League

TRACKED_LEAGUES: tuple[League, ...] = (
    League(display_name="Worlds", game=Game.LOL, epg_channel_id="twitch.riotgames"),
    League(display_name="MSI", game=Game.LOL, epg_channel_id="twitch.riotgames"),
    League(display_name="First Stand", game=Game.LOL, epg_channel_id="twitch.riotgames"),

    League(display_name="LCS", game=Game.LOL, epg_channel_id="twitch.lcs"),
    League(display_name="LEC", game=Game.LOL, epg_channel_id="twitch.lec"),
    League(display_name="LCK", game=Game.LOL, epg_channel_id="twitch.lck"),
    League(display_name="LPL", game=Game.LOL, epg_channel_id="youtube.LPL_English"),


    League(display_name="Champions", game=Game.VALORANT, epg_channel_id="twitch.VALORANT"),
    League(display_name="VALORANT Masters", game=Game.VALORANT, epg_channel_id="twitch.VALORANT"),
    League(display_name="Game Changers Championship", game=Game.VALORANT, epg_channel_id="twitch.VALORANT"),

    League(display_name="VCT Americas", game=Game.VALORANT, epg_channel_id="twitch.valorant_americas"),
    League(display_name="Game Changers NA", game=Game.VALORANT, epg_channel_id="twitch.valorant_americas"),
    League(display_name="Last Chance Qualifier Americas", game=Game.VALORANT, epg_channel_id="twitch.valorant_americas"),

    League(display_name="VCT EMEA", game=Game.VALORANT, epg_channel_id="twitch.valorant_emea"),
    League(display_name="Game Changers EMEA", game=Game.VALORANT, epg_channel_id="twitch.valorant_emea"),
    League(display_name="Last Chance Qualifier EMEA", game=Game.VALORANT, epg_channel_id="twitch.valorant_emea"),

    League(display_name="VCT Pacific", game=Game.VALORANT, epg_channel_id="twitch.valorant_pacific"),
    League(display_name="Game Changers Pacific", game=Game.VALORANT, epg_channel_id="twitch.valorant_pacific"),
    League(display_name="Last Chance Qualifier Pacific", game=Game.VALORANT, epg_channel_id="twitch.valorant_pacific"),
)


class UnknownLeagueError(ValueError):
    """Raised when a league we expect to track can't be found on the Riot API."""


def leagues_for_game(game: Game) -> tuple[League, ...]:
    return tuple(league for league in TRACKED_LEAGUES if league.game == game)


def find_league(display_name: str) -> League:
    for league in TRACKED_LEAGUES:
        if league.display_name == display_name:
            return league
    raise UnknownLeagueError(
        f"{display_name!r} is not in TRACKED_LEAGUES — add it to channel_map.py "
        "with its Dispatcharr EPG channel id before scraping it."
    )
