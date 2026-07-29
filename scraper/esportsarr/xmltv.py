"""Builds an XMLTV feed for the individual per-league Twitcharr channels.

Only upcoming/live matches go in the guide. Completed matches aren't useful
in a forward-looking EPG. Riot's API doesn't give an explicit match end time,
so we estimate one; a Bo3/Bo5 broadcast block including pre/post-show
commentary reliably runs a few hours.
"""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from datetime import timedelta

from .models import MatchEvent, MatchState

DEFAULT_MATCH_DURATION = timedelta(hours=3)

XMLTV_TIME_FORMAT = "%Y%m%d%H%M%S %z"
XMLTV_LANG = "en"
CATEGORY = "Esports"

GUIDE_STATES = (MatchState.UNSTARTED, MatchState.IN_PROGRESS)


def build_xmltv(matches: list[MatchEvent]) -> str:
    tv = ElementTree.Element("tv", attrib={"generator-info-name": "esportsarr"})

    guide_matches = [match for match in matches if match.state in GUIDE_STATES and match.has_real_content]

    seen_channel_ids: set[str] = set()
    for match in guide_matches:
        channel_id = match.league.epg_channel_id
        if channel_id in seen_channel_ids:
            continue
        seen_channel_ids.add(channel_id)
        channel_el = ElementTree.SubElement(tv, "channel", attrib={"id": channel_id})
        display_name_el = ElementTree.SubElement(channel_el, "display-name")
        display_name_el.text = match.league.display_name

    for match in guide_matches:
        stop = match.start + DEFAULT_MATCH_DURATION
        programme_el = ElementTree.SubElement(
            tv,
            "programme",
            attrib={
                "start": match.start.strftime(XMLTV_TIME_FORMAT),
                "stop": stop.strftime(XMLTV_TIME_FORMAT),
                "channel": match.league.epg_channel_id,
            },
        )
        title_el = ElementTree.SubElement(programme_el, "title", attrib={"lang": XMLTV_LANG})
        title_el.text = match.title
        desc_el = ElementTree.SubElement(programme_el, "desc", attrib={"lang": XMLTV_LANG})
        desc_el.text = match.description
        category_el = ElementTree.SubElement(programme_el, "category", attrib={"lang": XMLTV_LANG})
        category_el.text = CATEGORY

    ElementTree.indent(tv, space="  ")
    xml_body = ElementTree.tostring(tv, encoding="unicode", xml_declaration=False)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{xml_body}\n'
