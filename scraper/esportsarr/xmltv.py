"""Builds an XMLTV feed for the individual per-league Twitcharr channels.

Only upcoming/live matches go in the guide. Completed matches aren't useful
in a forward-looking EPG. Riot's API doesn't give an explicit match end time,
so we estimate one; a Bo3/Bo5 broadcast block including pre/post-show
commentary reliably runs a few hours.
"""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from datetime import timedelta

from .models import Game, MatchEvent, MatchState

DEFAULT_MATCH_DURATION = timedelta(hours=3)

# Riot gives no match end time. A broadcast block runs roughly this long
# depending on format, including pre/post-show. Per-game since individual
# games/maps run a different length in each: a LoL game is ~30-40min, a
# Valorant map ~40-45min, so the same best-of count adds up to a
# meaningfully different broadcast length per game. Unrecognized/missing
# formats (e.g. no "strategy" reported) fall back to DEFAULT_MATCH_DURATION.
LOL_BEST_OF_DURATIONS = {
    1: timedelta(hours=1),
    3: timedelta(hours=2),
    5: timedelta(hours=3, minutes=20),
}
# Bo7 isn't used by any Valorant league we track yet (Rocket League, planned
# for a different game entirely) -- this estimate is an unvalidated
# placeholder until then.
VALORANT_BEST_OF_DURATIONS = {
    1: timedelta(hours=1),
    3: timedelta(hours=3),
    5: timedelta(hours=5, minutes=30),
    7: timedelta(hours=7, minutes=30),
}
BEST_OF_DURATIONS_BY_GAME = {
    Game.LOL: LOL_BEST_OF_DURATIONS,
    Game.VALORANT: VALORANT_BEST_OF_DURATIONS,
}

XMLTV_TIME_FORMAT = "%Y%m%d%H%M%S %z"
XMLTV_LANG = "en"
CATEGORY = "Esports"

GUIDE_STATES = (MatchState.UNSTARTED, MatchState.IN_PROGRESS)


def _duration_for_match(match: MatchEvent) -> timedelta:
    durations = BEST_OF_DURATIONS_BY_GAME.get(match.league.game, {})
    return durations.get(match.best_of, DEFAULT_MATCH_DURATION)


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
        stop = match.start + _duration_for_match(match)
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
