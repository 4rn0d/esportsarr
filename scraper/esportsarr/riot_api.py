"""Thin client for Riot Games' esports schedule API (undocumented, shared
public key -- see README for what to check if it starts 401/403ing)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

from .models import RIOT_STATE_TO_MATCH_STATE, League, MatchEvent, StreamPlatform

logger = logging.getLogger(__name__)

RIOT_ESPORTS_PUBLIC_API_KEY = "0TvQnueqKa5mxJntVWt0w4LpLfEkrV1Ta8rQBb9Z"  # public, shared by Riot's own web client

REQUEST_TIMEOUT_SECONDS = 15
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


def _has_real_content(event: dict) -> bool:
    teams = (event.get("match") or {}).get("teams") or []
    return len(teams) == 2 or bool(event.get("blockName"))


def _match_participants(event: dict) -> str | None:
    teams = (event.get("match") or {}).get("teams") or []
    team_names = [team.get("name", "TBD") for team in teams]
    return f"{team_names[0]} vs {team_names[1]}" if len(team_names) == 2 else None


def _best_of(event: dict) -> int | None:
    strategy = (event.get("match") or {}).get("strategy") or {}
    return strategy.get("count") if strategy.get("type") == "bestOf" else None


def _match_description(event: dict, best_of: int | None) -> str:
    # Title is always just the league name, so this is where the actual
    # match info lives: participants first, then stage, then format.
    parts = [part for part in (_match_participants(event), event.get("blockName")) if part]
    if best_of:
        parts.append(f"Bo{best_of}")
    return " · ".join(parts)


STREAM_PLATFORM_PREFIXES: dict[str, StreamPlatform] = {
    "twitch.": StreamPlatform.TWITCH,
    "youtube.": StreamPlatform.YOUTUBE,
}


def _stream_identity_for_league(league: League) -> tuple[StreamPlatform | None, str | None]:
    # Riot's stream info per-event is unreliable (empty even for live
    # matches), so this derives it from epg_channel_id's prefix instead.
    for prefix, platform in STREAM_PLATFORM_PREFIXES.items():
        if league.epg_channel_id.startswith(prefix):
            return platform, league.epg_channel_id[len(prefix):]
    return None, None


def _normalize_event(event: dict, league: League) -> MatchEvent:
    start = datetime.fromisoformat(event["startTime"].replace("Z", "+00:00")).astimezone(timezone.utc)
    has_real_content = _has_real_content(event)
    best_of = _best_of(event)
    stream_platform, stream_channel = _stream_identity_for_league(league)
    if not has_real_content:
        # No team names and no stage name -- just the bare league name, not
        # worth blocking a slot from a real match over.
        stream_platform, stream_channel = None, None
    return MatchEvent(
        league=league,
        start=start,
        state=RIOT_STATE_TO_MATCH_STATE[event["state"]],
        title=league.display_name,
        stream_platform=stream_platform,
        stream_channel=stream_channel,
        description=_match_description(event, best_of),
        has_real_content=has_real_content,
        best_of=best_of,
        match_id=(event.get("match") or {}).get("id"),
    )


def fetch_matches_for_leagues(leagues: list[League], api_key: str = RIOT_ESPORTS_PUBLIC_API_KEY) -> list[MatchEvent]:
    """Fetches and normalizes matches for every given league, grouped by host
    so getLeagues is only called once per host. A league not found via
    getLeagues is logged and skipped rather than raised."""
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
                    "League %r not found via getLeagues on %s. Riot may have renamed "
                    "or retired it, or TRACKED_LEAGUES has a typo. Skipping it.",
                    league.display_name,
                    host.base_url,
                )
                continue
            events = get_schedule(host, league_id, api_key)
            matches.extend(_normalize_event(event, league) for event in events)

    return matches
