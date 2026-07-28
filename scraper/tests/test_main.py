"""Tests for esportsarr.main's schedule windowing — Riot's schedule endpoints
return a league's entire history plus far-future placeholders (observed:
matches from 2023 through a next TBD block over a year out), so this is the
only thing keeping schedule.json from growing unbounded."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from esportsarr.main import SCHEDULE_WINDOW, _within_schedule_window
from esportsarr.models import Game, League, MatchEvent, MatchState

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
LCS = League(display_name="LCS", game=Game.LOL, epg_channel_id="twitch.lcs")


def _match(start: datetime) -> MatchEvent:
    return MatchEvent(league=LCS, start=start, state=MatchState.COMPLETED, title="A vs B", twitch_channel="lcs")


def test_within_schedule_window_keeps_matches_inside_the_window():
    inside = [
        _match(NOW),
        _match(NOW - SCHEDULE_WINDOW),  # exactly on the lower boundary, inclusive
        _match(NOW + SCHEDULE_WINDOW),  # exactly on the upper boundary, inclusive
    ]

    assert _within_schedule_window(inside, NOW, SCHEDULE_WINDOW) == inside


def test_within_schedule_window_drops_matches_outside_the_window():
    too_old = _match(NOW - SCHEDULE_WINDOW - timedelta(seconds=1))
    too_far_ahead = _match(NOW + SCHEDULE_WINDOW + timedelta(seconds=1))
    ancient = _match(datetime(2023, 8, 17, tzinfo=timezone.utc))  # real value seen from Riot's API

    result = _within_schedule_window([too_old, too_far_ahead, ancient], NOW, SCHEDULE_WINDOW)

    assert result == []


def test_within_schedule_window_on_empty_input():
    assert _within_schedule_window([], NOW, SCHEDULE_WINDOW) == []
