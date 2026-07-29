"""Tests for channel_sync's pure functions, `_build_guide_xmltv`,
`build_guide_entries`, and `_round_down_to_quarter_hour` are the only ones
with zero Django dependency (everything else uses models deferred into
function bodies, so it can't run without a real Dispatcharr instance; see
channel_sync.py's module docstring)."""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from datetime import datetime, timedelta, timezone

from esportsarr.channel_sync import (
    GUIDE_CATEGORY,
    GUIDE_LANG,
    OFFLINE_PROGRAM_TITLE,
    PROGRAMME_DURATION,
    _build_guide_xmltv,
    _round_down_to_quarter_hour,
    build_guide_entries,
)


def _entry(tvg_id: str, name: str, title: str, hour: int = 20, description: str = "") -> dict:
    start = datetime(2026, 7, 28, hour, 0, tzinfo=timezone.utc)
    entry = {"tvg_id": tvg_id, "name": name, "title": title, "start": start, "end": start.replace(hour=hour + 1)}
    if description:
        entry["description"] = description
    return entry


def test_build_guide_xmltv_produces_well_formed_xml_with_expected_channels_and_programmes():
    entries = [
        _entry("esportsarr.lol.1", "LoL 1", "T1 vs Gen.G"),
        _entry("esportsarr.valorant.1", "Valorant 1", "Sentinels vs 100T", hour=21),
    ]

    xml_text = _build_guide_xmltv(entries)
    root = ElementTree.fromstring(xml_text)

    channel_ids = {el.get("id") for el in root.findall("channel")}
    assert channel_ids == {"esportsarr.lol.1", "esportsarr.valorant.1"}

    programmes = root.findall("programme")
    assert len(programmes) == 2
    titles = {p.find("title").text for p in programmes}
    assert titles == {"T1 vs Gen.G", "Sentinels vs 100T"}


def test_build_guide_xmltv_deduplicates_repeated_channel_ids():
    # Same tvg_id shouldn't get a duplicate <channel> element even if
    # multiple entries reference it (shouldn't normally happen, one entry
    # per slot per tick, but the dedup must hold regardless).
    entries = [
        _entry("esportsarr.lol.1", "LoL 1", "T1 vs Gen.G"),
        _entry("esportsarr.lol.1", "LoL 1", "T1 vs Gen.G"),
    ]

    xml_text = _build_guide_xmltv(entries)
    root = ElementTree.fromstring(xml_text)

    assert len(root.findall("channel")) == 1
    assert len(root.findall("programme")) == 2


def test_build_guide_xmltv_escapes_special_characters_in_titles():
    entries = [_entry("esportsarr.lol.1", "LoL 1", "Team <A> & \"B\"'s Squad")]

    xml_text = _build_guide_xmltv(entries)
    # Must parse without raising. A naive string-concat XML builder would
    # produce invalid XML here and ElementTree.fromstring would blow up.
    root = ElementTree.fromstring(xml_text)
    assert root.find("programme/title").text == "Team <A> & \"B\"'s Squad"


def test_build_guide_xmltv_on_empty_entries_produces_a_valid_empty_guide():
    xml_text = _build_guide_xmltv([])
    root = ElementTree.fromstring(xml_text)

    assert root.findall("channel") == []
    assert root.findall("programme") == []


def test_build_guide_xmltv_includes_category_only_for_real_matches_not_filler():
    real = _entry("esportsarr.lol.1", "LoL 1", "T1 vs Gen.G", description="LCS: Playoffs")
    filler = _entry("esportsarr.lol.1", "LoL 1", "No Match Scheduled")  # no description

    xml_text = _build_guide_xmltv([real, filler])
    root = ElementTree.fromstring(xml_text)

    programmes = root.findall("programme")
    assert programmes[0].find("category").text == GUIDE_CATEGORY
    assert programmes[1].find("category") is None


def test_build_guide_xmltv_tags_title_desc_and_category_with_lang():
    entries = [_entry("esportsarr.lol.1", "LoL 1", "T1 vs Gen.G", description="LCS: Playoffs")]

    xml_text = _build_guide_xmltv(entries)
    root = ElementTree.fromstring(xml_text)
    programme = root.find("programme")

    assert programme.find("title").get("lang") == GUIDE_LANG
    assert programme.find("desc").get("lang") == GUIDE_LANG
    assert programme.find("category").get("lang") == GUIDE_LANG


def test_build_guide_xmltv_includes_desc_element_when_description_is_present():
    entries = [_entry("esportsarr.lol.1", "LoL 1", "T1 vs Gen.G", description="LCS: Playoffs")]

    xml_text = _build_guide_xmltv(entries)
    root = ElementTree.fromstring(xml_text)

    assert root.find("programme/desc").text == "LCS: Playoffs"


def test_build_guide_xmltv_omits_desc_element_when_description_is_absent():
    entries = [_entry("esportsarr.lol.1", "LoL 1", "No Match Scheduled")]

    xml_text = _build_guide_xmltv(entries)
    root = ElementTree.fromstring(xml_text)

    assert root.find("programme/desc") is None


def _future_match(title: str, start: datetime, description: str = "") -> dict:
    match = {"start": start.isoformat(), "title": title, "state": "unstarted"}
    if description:
        match["description"] = description
    return match


NOW = datetime(2026, 7, 28, 15, 53, tzinfo=timezone.utc)  # deliberately not on a quarter-hour
PROJECTION_END = NOW + timedelta(days=7)


def _claim(match: dict, at: datetime | None = None) -> tuple[datetime, dict]:
    """A match claimed with no contention, i.e. at its own real start,
    unless `at` says otherwise (a match that had to wait for its slot)."""
    return (at or datetime.fromisoformat(match["start"]), match)


def test_build_guide_entries_fills_the_gap_before_a_future_match_and_after_it():
    match = _future_match("Sentinels vs 100T", NOW + timedelta(hours=2))
    entries = build_guide_entries("valorant", [[_claim(match)]], NOW, PROJECTION_END)

    assert len(entries) == 3
    leading, real, trailing = entries

    assert leading["title"] == OFFLINE_PROGRAM_TITLE
    assert leading["start"] == _round_down_to_quarter_hour(NOW)
    assert leading["end"] == datetime.fromisoformat(match["start"])

    assert real["title"] == "Sentinels vs 100T"
    assert real["start"] == datetime.fromisoformat(match["start"])
    assert real["end"] == datetime.fromisoformat(match["start"]) + PROGRAMME_DURATION

    assert trailing["title"] == OFFLINE_PROGRAM_TITLE
    assert trailing["start"] == real["end"]
    assert trailing["end"] == PROJECTION_END


def test_build_guide_entries_has_no_leading_filler_for_an_already_live_match():
    # Match started in the past (relative to `now`). Nothing to fill before it.
    match = _future_match("Sentinels vs 100T", NOW - timedelta(hours=1))
    entries = build_guide_entries("valorant", [[_claim(match)]], NOW, PROJECTION_END)

    assert entries[0]["title"] == "Sentinels vs 100T"
    assert entries[0]["start"] == datetime.fromisoformat(match["start"])


def test_build_guide_entries_displays_a_contended_match_at_its_actual_claim_time_not_its_own_start():
    # A match that had to wait for its slot to free up (allocator.py's
    # `claimed_at`) must be displayed starting when it actually took over
    # the slot, not its own independent real start -- otherwise it looks
    # like it overlaps whatever the slot was still showing.
    match = _future_match("Shopify Rebellion Black vs 2GAME", NOW + timedelta(hours=1))
    claimed_at = NOW + timedelta(hours=3)  # had to wait 2 extra hours for the slot
    entries = build_guide_entries("valorant", [[_claim(match, at=claimed_at)]], NOW, PROJECTION_END)

    real = entries[1]
    assert real["start"] == claimed_at
    # End still comes from the match's own real start + estimate, not
    # claimed_at -- joining late doesn't make the broadcast run any longer.
    assert real["end"] == datetime.fromisoformat(match["start"]) + PROGRAMME_DURATION


def test_build_guide_entries_fills_the_gap_between_two_consecutive_matches():
    first = _future_match("Sentinels vs 100T", NOW + timedelta(hours=1))
    second = _future_match("LOUD vs NRG", NOW + timedelta(hours=6))  # starts well after `first` ends
    entries = build_guide_entries("valorant", [[_claim(first), _claim(second)]], NOW, PROJECTION_END)

    titles = [e["title"] for e in entries]
    assert titles == [
        OFFLINE_PROGRAM_TITLE,
        "Sentinels vs 100T",
        OFFLINE_PROGRAM_TITLE,
        "LOUD vs NRG",
        OFFLINE_PROGRAM_TITLE,
    ]
    gap_filler = entries[2]
    assert gap_filler["start"] == datetime.fromisoformat(first["start"]) + PROGRAMME_DURATION
    assert gap_filler["end"] == datetime.fromisoformat(second["start"])


def test_build_guide_entries_carries_the_match_description_through_but_not_the_filler():
    match = _future_match("Sentinels vs 100T", NOW + timedelta(hours=1), description="VCT Americas: Week 3")
    entries = build_guide_entries("valorant", [[_claim(match)]], NOW, PROJECTION_END)

    leading_filler, real, trailing_filler = entries
    assert real["description"] == "VCT Americas: Week 3"
    assert leading_filler["description"] == ""
    assert trailing_filler["description"] == ""


def test_build_guide_entries_produces_one_continuous_filler_when_nothing_is_projected():
    entries = build_guide_entries("valorant", [[]], NOW, PROJECTION_END)

    assert len(entries) == 1
    assert entries[0]["title"] == OFFLINE_PROGRAM_TITLE
    assert entries[0]["start"] == _round_down_to_quarter_hour(NOW)
    assert entries[0]["end"] == PROJECTION_END


def test_build_guide_entries_uses_the_right_tvg_id_and_name_per_game_and_slot():
    entries = build_guide_entries("lol", [[], []], NOW, PROJECTION_END)

    assert [e["tvg_id"] for e in entries] == ["esportsarr.lol.1", "esportsarr.lol.2"]
    assert [e["name"] for e in entries] == ["LoL 1", "LoL 2"]


def test_round_down_to_quarter_hour_rounds_mid_quarter_moments_down():
    assert _round_down_to_quarter_hour(datetime(2026, 7, 28, 15, 53, 12, 500, tzinfo=timezone.utc)) == datetime(
        2026, 7, 28, 15, 45, tzinfo=timezone.utc
    )


def test_round_down_to_quarter_hour_leaves_an_exact_boundary_unchanged():
    assert _round_down_to_quarter_hour(datetime(2026, 7, 28, 15, 30, 0, tzinfo=timezone.utc)) == datetime(
        2026, 7, 28, 15, 30, tzinfo=timezone.utc
    )


def test_round_down_to_quarter_hour_rounds_down_to_the_top_of_the_hour():
    assert _round_down_to_quarter_hour(datetime(2026, 7, 28, 15, 5, tzinfo=timezone.utc)) == datetime(
        2026, 7, 28, 15, 0, tzinfo=timezone.utc
    )


def test_round_down_to_quarter_hour_preserves_timezone():
    result = _round_down_to_quarter_hour(datetime(2026, 7, 28, 15, 53, tzinfo=timezone.utc))
    assert result.tzinfo == timezone.utc
