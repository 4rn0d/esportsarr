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
    epg_channel_id: str


@dataclass(frozen=True)
class MatchEvent:
    """A single scheduled/live/completed match, normalized across LoL and Valorant."""

    league: League
    start: datetime
    state: MatchState
    title: str
    stream_platform: StreamPlatform | None
    stream_channel: str | None
    description: str
    has_real_content: bool
    best_of: int | None
    match_id: str | None
