"""Thin client for Riot Games' esports schedule API.

Both lolesports.com and valorantesports.com are React SPAs that call this same
persisted-query gateway client-side. Endpoints, response shape, and the public
`x-api-key` below were verified live against the real API on 2026-07-27 (see
project README for the exact commands used) — Riot doesn't publish this as a
supported integration, so if requests start failing with 401/403, this key is
the first thing to check against the community docs at
https://github.com/vickz84259/lolesports-api-docs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

from .models import RIOT_STATE_TO_MATCH_STATE, League, MatchEvent

logger = logging.getLogger(__name__)

# Widely-shared public read-only key that lolesports.com/valorantesports.com's
# own web client embeds and sends on every request. Not a secret.
RIOT_ESPORTS_PUBLIC_API_KEY = "0TvQnueqKa5mxJntVWt0w4LpLfEkrV1Ta8rQBb9Z"

REQUEST_TIMEOUT_SECONDS = 15

# Fill in your own repo URL here so Riot (or anyone reading logs) can see who's
# calling — no documented requirement for this endpoint, but good citizenship
# given the rate limits are undocumented too.
SCRAPER_USER_AGENT = "esportsarr/0.1 (+https://github.com/4rn0d/esportsarr)"


@dataclass(frozen=True)
class RiotHost:
    base_url: str
    extra_params: dict[str, str] = field(default_factory=dict)


LOL_HOST = RiotHost(base_url="https://esports-api.lolesports.com/persisted/gw")
VALORANT_HOST = RiotHost(
    base_url="https://esports-api.service.valorantesports.com/persisted/val",
    extra_params={"sport": "val"},
)

HOST_FOR_GAME = {
    "lol": LOL_HOST,
    "valorant": VALORANT_HOST,
}


def _get(host: RiotHost, api_key: str, endpoint: str, params: dict[str, str]) -> dict:
    response = requests.get(
        f"{host.base_url}/{endpoint}",
        headers={"x-api-key": api_key, "User-Agent": SCRAPER_USER_AGENT},
        params={**host.extra_params, **params},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def get_leagues(host: RiotHost, api_key: str = RIOT_ESPORTS_PUBLIC_API_KEY) -> list[dict]:
    """Raw league dicts (id, name, slug, region, ...) for a given host."""
    payload = _get(host, api_key, "getLeagues", {"hl": "en-US"})
    return payload["data"]["leagues"]


def get_schedule(host: RiotHost, league_id: str, api_key: str = RIOT_ESPORTS_PUBLIC_API_KEY) -> list[dict]:
    """Raw schedule event dicts for one league id."""
    payload = _get(host, api_key, "getSchedule", {"hl": "en-US", "leagueId": league_id})
    return payload["data"]["schedule"]["events"]


def _match_title(event: dict, league: League) -> str:
    teams = (event.get("match") or {}).get("teams") or []
    team_names = [team.get("name", "TBD") for team in teams]
    if len(team_names) == 2:
        return f"{team_names[0]} vs {team_names[1]}"
    block_name = event.get("blockName")
    return f"{league.display_name}: {block_name}" if block_name else league.display_name


def _twitch_channel(event: dict) -> str | None:
    for stream in event.get("streams") or []:
        if stream.get("provider") == "twitch" and stream.get("parameter"):
            return stream["parameter"]
    return None


def _normalize_event(event: dict, league: League) -> MatchEvent:
    start = datetime.fromisoformat(event["startTime"].replace("Z", "+00:00")).astimezone(timezone.utc)
    return MatchEvent(
        league=league,
        start=start,
        state=RIOT_STATE_TO_MATCH_STATE[event["state"]],
        title=_match_title(event, league),
        twitch_channel=_twitch_channel(event),
    )


def fetch_matches_for_leagues(leagues: list[League], api_key: str = RIOT_ESPORTS_PUBLIC_API_KEY) -> list[MatchEvent]:
    """Fetch and normalize matches for every given league, grouped by host so
    `getLeagues` is only called once per host regardless of how many leagues
    from that game we're tracking.

    A league not found via getLeagues (Riot renamed/retired it, or
    TRACKED_LEAGUES has a typo) is logged and skipped rather than raised —
    one bad entry shouldn't take down every other league in the same game.
    Use `python -m esportsarr.list_leagues --game <game>` to find the exact
    current name when adding or fixing a league.
    """
    matches: list[MatchEvent] = []

    leagues_by_game: dict[str, list[League]] = {}
    for league in leagues:
        leagues_by_game.setdefault(league.game.value, []).append(league)

    for game, game_leagues in leagues_by_game.items():
        host = HOST_FOR_GAME[game]
        remote_leagues = get_leagues(host, api_key)
        id_by_name = {entry["name"]: entry["id"] for entry in remote_leagues}

        for league in game_leagues:
            league_id = id_by_name.get(league.display_name)
            if league_id is None:
                logger.warning(
                    "League %r not found via getLeagues on %s — Riot may have renamed "
                    "or retired it, or TRACKED_LEAGUES has a typo. Skipping it.",
                    league.display_name,
                    host.base_url,
                )
                continue
            events = get_schedule(host, league_id, api_key)
            matches.extend(_normalize_event(event, league) for event in events)

    return matches
