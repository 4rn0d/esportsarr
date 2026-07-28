"""Shared data types for the esports schedule scraper."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Game(str, Enum):
    LOL = "lol"
    VALORANT = "valorant"


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
    # Dispatcharr EPG channel id for the existing per-league Twitcharr channel,
    # e.g. "twitch.lcs". Used by xmltv.py to attach programme data.
    epg_channel_id: str


@dataclass(frozen=True)
class MatchEvent:
    """A single scheduled/live/completed match, normalized across LoL and Valorant."""

    league: League
    start: datetime
    state: MatchState
    title: str
    twitch_channel: str | None
