"""Shared data types for the esports schedule scraper."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Game(str, Enum):
    LOL = "lol"
    VALORANT = "valorant"


class StreamPlatform(str, Enum):
    TWITCH = "twitch"
    YOUTUBE = "youtube"


class MatchState(str, Enum):
    UNSTARTED = "unstarted"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


# The Riot esports API uses these exact strings for event state. Kept as a
# single mapping so both `riot_api.py` and tests agree on the translation.
RIOT_STATE_TO_MATCH_STATE = {
    "unstarted": MatchState.UNSTARTED,
    "inProgress": MatchState.IN_PROGRESS,
    "completed": MatchState.COMPLETED,
}


@dataclass(frozen=True)
class League:
    """One league we track, as it appears on the Riot esports API `name` field."""

    display_name: str
    game: Game
    epg_channel_id: str  # Dispatcharr EPG channel id, e.g. "twitch.lcs"


@dataclass(frozen=True)
class MatchEvent:
    """A single scheduled/live/completed match, normalized across LoL and Valorant."""

    league: League
    start: datetime
    state: MatchState
    title: str  # always the league's display_name, e.g. "LCS"
    stream_platform: StreamPlatform | None  # None if the league has no known streamable source
    stream_channel: str | None
    description: str  # e.g. "Sentinels vs Cloud9 · Playoffs · Bo3"
    has_real_content: bool  # False if Riot gave neither team names nor a stage name
    best_of: int | None  # e.g. 3 for a Bo3; None if Riot didn't report a format
