"""Serializes the full match list to schedule.json — the single source of
truth the Dispatcharr plugin (piece 2) polls to decide stream priority.
Unlike xmltv.py this includes every state (including completed), since the
plugin needs to know when a live match has just ended, not only what's next.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .models import MatchEvent


def _match_to_dict(match: MatchEvent) -> dict:
    return {
        "league": match.league.display_name,
        "game": match.league.game.value,
        "start": match.start.isoformat(),
        "state": match.state.value,
        "title": match.title,
        "twitch_channel": match.twitch_channel,
    }


def build_schedule_json(matches: list[MatchEvent], *, generated_at: datetime | None = None) -> str:
    payload = {
        "generated_at": (generated_at or datetime.now(timezone.utc)).isoformat(),
        "matches": [_match_to_dict(match) for match in matches],
    }
    return json.dumps(payload, indent=2)
