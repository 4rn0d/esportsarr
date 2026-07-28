from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from datetime import datetime, timezone

from esportsarr.models import Game, League, MatchEvent, MatchState
from esportsarr.xmltv import DEFAULT_MATCH_DURATION, build_xmltv

LCS = League(display_name="LCS", game=Game.LOL, epg_channel_id="twitch.lcs")
LEC = League(display_name="LEC", game=Game.LOL, epg_channel_id="twitch.lec")


def _match(league: League, state: MatchState, title: str, hour: int = 20) -> MatchEvent:
    return MatchEvent(
        league=league,
        start=datetime(2026, 7, 27, hour, 0, tzinfo=timezone.utc),
        state=state,
        title=title,
        twitch_channel="lcs",
    )


def test_build_xmltv_produces_well_formed_xml_with_expected_channels_and_programmes():
    matches = [
        _match(LCS, MatchState.IN_PROGRESS, "Sentinels vs Cloud9"),
        _match(LEC, MatchState.UNSTARTED, "G2 vs Fnatic", hour=22),
    ]

    xml_text = build_xmltv(matches)
    root = ElementTree.fromstring(xml_text)

    channel_ids = {el.get("id") for el in root.findall("channel")}
    assert channel_ids == {"twitch.lcs", "twitch.lec"}

    programmes = root.findall("programme")
    assert len(programmes) == 2
    titles = {p.find("title").text for p in programmes}
    assert titles == {"Sentinels vs Cloud9", "G2 vs Fnatic"}


def test_build_xmltv_excludes_completed_matches():
    matches = [_match(LCS, MatchState.COMPLETED, "Old match")]

    xml_text = build_xmltv(matches)
    root = ElementTree.fromstring(xml_text)

    assert root.findall("programme") == []
    assert root.findall("channel") == []


def test_build_xmltv_escapes_special_characters_in_titles():
    matches = [_match(LCS, MatchState.UNSTARTED, "Team <A> & \"B\"'s Squad")]

    xml_text = build_xmltv(matches)
    # Must parse without raising — a naive string-concat XML builder would
    # produce invalid XML here and ElementTree.fromstring would blow up.
    root = ElementTree.fromstring(xml_text)
    assert root.find("programme/title").text == "Team <A> & \"B\"'s Squad"


def test_build_xmltv_stop_time_is_start_plus_default_duration():
    match = _match(LCS, MatchState.UNSTARTED, "Sentinels vs Cloud9")
    xml_text = build_xmltv([match])
    root = ElementTree.fromstring(xml_text)

    programme = root.find("programme")
    start = datetime.strptime(programme.get("start"), "%Y%m%d%H%M%S %z")
    stop = datetime.strptime(programme.get("stop"), "%Y%m%d%H%M%S %z")
    assert stop - start == DEFAULT_MATCH_DURATION
