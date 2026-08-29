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
        "stream_platform": match.stream_platform.value if match.stream_platform else None,
        "stream_channel": match.stream_channel,
        "description": match.description,
        "best_of": match.best_of,
        "match_id": match.match_id,
    }


def build_schedule_json(
    matches: list[MatchEvent],
    *,
    supplemental: dict | None = None,
    generated_at: datetime | None = None,
) -> str:
    payload = {
        "generated_at": (generated_at or datetime.now(timezone.utc)).isoformat(),
        "matches": [_match_to_dict(match) for match in matches],
    }
    if supplemental is not None:
        payload["supplemental"] = supplemental
    return json.dumps(payload, indent=2)
