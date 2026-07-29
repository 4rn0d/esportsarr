"""CLI helper: prints every league Riot's getLeagues returns for a game, with
its exact `name` string and `id`. TRACKED_LEAGUES entries in channel_map.py
must match one of these names exactly (not a substring). This exists so
adding or fixing a league doesn't require an ad-hoc API query first.

Run via `python -m esportsarr.list_leagues --game valorant`.
"""

from __future__ import annotations

import argparse

from .models import Game
from .riot_api import HOST_FOR_GAME, get_leagues


def run(game: Game) -> None:
    host = HOST_FOR_GAME[game.value]
    leagues = get_leagues(host)
    for league in sorted(leagues, key=lambda entry: entry["name"]):
        print(f"{league['name']!r} (id={league['id']})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", choices=[game.value for game in Game], required=True)
    args = parser.parse_args()
    run(Game(args.game))


if __name__ == "__main__":
    main()
