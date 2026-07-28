"""Tests for channel_sync's pure guide-XML building — `_build_guide_xmltv`
is the only function in this module with zero Django dependency (everything
else uses models deferred into function bodies, so it can't run without a
real Dispatcharr instance; see channel_sync.py's module docstring)."""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from datetime import datetime, timezone

from esportsarr.channel_sync import _build_guide_xmltv


def _entry(tvg_id: str, name: str, title: str, hour: int = 20) -> dict:
    start = datetime(2026, 7, 28, hour, 0, tzinfo=timezone.utc)
    return {"tvg_id": tvg_id, "name": name, "title": title, "start": start, "end": start.replace(hour=hour + 1)}


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
    # multiple entries reference it (shouldn't normally happen — one entry
    # per slot per tick — but the dedup must hold regardless).
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
    # Must parse without raising — a naive string-concat XML builder would
    # produce invalid XML here and ElementTree.fromstring would blow up.
    root = ElementTree.fromstring(xml_text)
    assert root.find("programme/title").text == "Team <A> & \"B\"'s Squad"


def test_build_guide_xmltv_on_empty_entries_produces_a_valid_empty_guide():
    xml_text = _build_guide_xmltv([])
    root = ElementTree.fromstring(xml_text)

    assert root.findall("channel") == []
    assert root.findall("programme") == []
