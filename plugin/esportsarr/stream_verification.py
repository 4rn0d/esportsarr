"""Cross-checks a live match's declared Twitch channel against reality via
Twitch's public, unauthenticated GQL endpoint -- no API credentials needed,
the same keyless approach the Twitcharr plugin uses. Game Changers
broadcasts sometimes split concurrent games onto a secondary channel that
Riot's schedule has no way to reflect; this catches that mismatch."""

from __future__ import annotations

import logging
from typing import Any, Callable

import requests

logger = logging.getLogger(__name__)

TWITCH_GQL_URL = "https://gql.twitch.tv/gql"
TWITCH_PUBLIC_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
REQUEST_TIMEOUT_SECONDS = 10

CHANNEL_QUERY = """
query EsportsarrStreamCheck($login: String!) {
  user(login: $login) {
    stream {
      title
    }
  }
}
"""

# Leagues whose broadcast is known to sometimes split concurrent games onto a
# secondary Twitch channel Riot's schedule has no way to reflect -- checked
# in order, so the schedule's own declared channel (listed first) still wins
# when it's actually correct.
LIVE_CHANNEL_CANDIDATES: dict[str, tuple[str, ...]] = {
    "Game Changers NA": ("valorant_americas", "Raidiant_glow", "RaidiantGG", "Raidiant_beam"),
    "Game Changers EMEA": ("valorant_emea", "remakeval"),
}


def fetch_twitch_stream_title(channel: str) -> str | None:
    """None if the channel is offline or the request fails -- a failed check
    is treated the same as "not live there", never crashes the sync tick."""
    try:
        response = requests.post(
            TWITCH_GQL_URL,
            json={"query": CHANNEL_QUERY, "variables": {"login": channel}},
            headers={"Client-ID": TWITCH_PUBLIC_CLIENT_ID, "Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        stream = response.json()["data"]["user"]["stream"]
        return stream["title"] if stream else None
    except Exception:
        logger.exception("esportsarr: failed to check Twitch stream title for %r", channel)
        return None


def _extract_participants(description: str) -> tuple[str, str] | None:
    first_part = description.split(" · ")[0]
    if " vs " not in first_part:
        return None
    team_a, team_b = first_part.split(" vs ", 1)
    return team_a.strip(), team_b.strip()


def _title_matches_participants(title: str, participants: tuple[str, str]) -> bool:
    title_lower = title.lower()
    return all(team.lower() in title_lower for team in participants)


def verify_stream_channel(match: dict[str, Any], fetch_title: Callable[[str], str | None]) -> dict[str, Any]:
    """Checks each candidate channel's live title against the match's own
    participants (from its description), in order, and returns a copy of
    `match` pointed at the first one that actually matches. If none do, the
    match is marked unstreamable for this tick rather than risk showing the
    wrong game."""
    candidates = LIVE_CHANNEL_CANDIDATES.get(match.get("league"))
    if not candidates:
        return match

    participants = _extract_participants(match.get("description", ""))
    if participants is None:
        return match

    for channel in candidates:
        title = fetch_title(channel)
        if title and _title_matches_participants(title, participants):
            return {**match, "stream_platform": "twitch", "stream_channel": channel}

    return {**match, "stream_platform": None, "stream_channel": None}
