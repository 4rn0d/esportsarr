"""Tests for esportsarr.schedule_export, this is the exact JSON shape the
Dispatcharr plugin parses (plugin/esportsarr/plugin.py's _fetch_schedule), so
a field-name typo here would silently break the plugin against real data."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from esportsarr.models import Game, League, MatchEvent, MatchState, StreamPlatform
from esportsarr.schedule_export import build_schedule_json

LCS = League(display_name="LCS", game=Game.LOL, epg_channel_id="twitch.lcs")

MATCH = MatchEvent(
    league=LCS,
    start=datetime(2026, 7, 27, 20, 0, tzinfo=timezone.utc),
    state=MatchState.IN_PROGRESS,
    title="Sentinels vs Cloud9",
    stream_platform=StreamPlatform.TWITCH,
    stream_channel="lcs",
    description="LCS: Playoffs · Bo3",
    has_real_content=True,
    best_of=3,
)


def test_build_schedule_json_includes_every_field_the_plugin_reads():
    payload = json.loads(build_schedule_json([MATCH]))

    [match_dict] = payload["matches"]
    assert match_dict == {
        "league": "LCS",
        "game": "lol",
        "start": "2026-07-27T20:00:00+00:00",
        "state": "in_progress",
        "title": "Sentinels vs Cloud9",
        "stream_platform": "twitch",
        "stream_channel": "lcs",
        "description": "LCS: Playoffs · Bo3",
        "best_of": 3,
    }


def test_build_schedule_json_serializes_no_stream_platform_as_null():
    unstreamable = MatchEvent(
        league=LCS,
        start=datetime(2026, 7, 27, 20, 0, tzinfo=timezone.utc),
        state=MatchState.IN_PROGRESS,
        title="Sentinels vs Cloud9",
        stream_platform=None,
        stream_channel=None,
        description="LCS: Playoffs",
        has_real_content=True,
        best_of=None,
    )

    [match_dict] = json.loads(build_schedule_json([unstreamable]))["matches"]

    assert match_dict["stream_platform"] is None
    assert match_dict["stream_channel"] is None


def test_build_schedule_json_uses_the_provided_generated_at_timestamp():
    generated_at = datetime(2026, 7, 28, 9, 30, tzinfo=timezone.utc)
    payload = json.loads(build_schedule_json([], generated_at=generated_at))

    assert payload["generated_at"] == "2026-07-28T09:30:00+00:00"
