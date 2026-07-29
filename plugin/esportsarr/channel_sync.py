"""Django ORM operations for the esportsarr plugin.

This plugin builds and owns its own Stream row per channel/platform instead
of reusing Twitcharr's, since attaching one of Twitcharr's own Streams to
our channels made Twitcharr's periodic cleanup delete them (it prunes any
Channel linked to a Stream it owns whose tvg_id isn't in its own tracked
list). We only borrow the shared StreamProfile it installs (Dispatcharr has
no built-in way to play a raw twitch.tv/youtube.com URL otherwise).

Guide content is written as a local XMLTV file (EPGSource.file_path, no
url) rather than raw ProgramData rows: a "dummy" source makes Dispatcharr
overlay its own generic filler regardless of real data, and file_path skips
the network-fetch path entirely so there's no bogus "download failed"
status to fight.

All Django imports are deferred into function bodies so this module stays
importable/testable without Django installed.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timedelta
from typing import Any

GAME_DISPLAY_NAMES = {"lol": "LoL", "valorant": "Valorant"}

GAME_LOGO_URLS = {
    "lol": "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcRD8UiwV0BGL9ggO2lM5VzxOy8CoABmOoGPexI3kRh1elWh1mEPgL3-0WxI&s=10",
    "valorant": "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcRFZ3lPk7mJOvXkYbPR9O93DNZw_q-Jt-DBQtl9qjsfW3u--2SpMoKeMyc&s=10",
}

EPG_SOURCE_NAME = "Esportsarr"

# YouTube profile is an unverified guess (no Twitcharr-equivalent exists for
# it yet) -- see plugin/README.md.
PLATFORM_STREAM_PROFILE_SETTINGS = {
    "twitch": "twitch_stream_profile_name",
    "youtube": "youtube_stream_profile_name",
}
PLATFORM_URL_BUILDERS = {
    "twitch": lambda channel: f"https://twitch.tv/{channel}",
    "youtube": lambda channel: f"https://www.youtube.com/@{channel}/live",
}

OWNED_STREAM_TAG = "esportsarr"

# Riot gives no match end time. Fallback for a match with no reported (or
# unrecognized) best-of format. Same estimate the scraper uses for
# esports.xmltv.
PROGRAMME_DURATION = timedelta(hours=3)

# Same table as scraper/esportsarr/xmltv.py -- kept in sync manually, the two
# packages don't share code. Bo7 isn't used by any league we track yet
# (Rocket League, planned) -- this estimate is an unvalidated placeholder.
BEST_OF_DURATIONS = {
    1: timedelta(hours=1),
    3: timedelta(hours=2, minutes=45),
    5: timedelta(hours=5, minutes=30),
    7: timedelta(hours=7, minutes=30),
}


def duration_for_match(match: dict[str, Any]) -> timedelta:
    return BEST_OF_DURATIONS.get(match.get("best_of"), PROGRAMME_DURATION)

# The guide used to start exactly at "now" and never earlier, so a slot idle
# for a while before "now" had zero programme data for that stretch --
# Dispatcharr's grid renders that as a blank hole instead of "No Match
# Scheduled" (confirmed as a real bug, 2026-07-29). Starting the guide this
# far before "now" instead guarantees continuous coverage.
GUIDE_LOOKBACK_HOURS = 12

OFFLINE_PROGRAM_TITLE = "No Match Scheduled"

# A single filler entry spanning hours/days looks broken in most EPG grids
# (one giant unbroken block); chunking it keeps the grid looking like a real
# programming schedule instead.
MAX_FILLER_BLOCK = timedelta(minutes=45)


def _filler_entries(tvg_id: str, name: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    block_start = start
    while block_start < end:
        block_end = min(block_start + MAX_FILLER_BLOCK, end)
        entries.append(
            {
                "tvg_id": tvg_id,
                "name": name,
                "title": OFFLINE_PROGRAM_TITLE,
                "description": "",
                "start": block_start,
                "end": block_end,
            }
        )
        block_start = block_end
    return entries

GUIDE_FILE_PATH = "/app/data/plugins/esportsarr/.state/esportsarr-guide.xmltv"
GUIDE_TIME_FORMAT = "%Y%m%d%H%M%S %z"
GUIDE_LANG = "en"
GUIDE_CATEGORY = "Esports"


class StreamProfileNotFoundError(LookupError):
    """The shared StreamProfile for a platform (see _get_stream_profile) doesn't exist."""


def _generic_channel_name(game: str, slot_index: int) -> str:
    return f"{GAME_DISPLAY_NAMES[game]} {slot_index + 1}"


def _generic_channel_tvg_id(game: str, slot_index: int) -> str:
    return f"esportsarr.{game}.{slot_index + 1}"


QUARTER_HOUR_MINUTES = 15


def _round_down_to_quarter_hour(moment: datetime) -> datetime:
    return moment.replace(minute=(moment.minute // QUARTER_HOUR_MINUTES) * QUARTER_HOUR_MINUTES, second=0, microsecond=0)


def _get_or_create_epg_source():
    from apps.epg.models import EPGSource

    epg_source, _ = EPGSource.objects.get_or_create(
        name=EPG_SOURCE_NAME,
        # is_active gates all EPG operations on this source, not just fetches.
        defaults={"source_type": "xmltv", "file_path": GUIDE_FILE_PATH, "is_active": True},
    )
    if epg_source.source_type != "xmltv" or epg_source.file_path != GUIDE_FILE_PATH or epg_source.url or not epg_source.is_active:
        epg_source.source_type = "xmltv"
        epg_source.file_path = GUIDE_FILE_PATH
        epg_source.url = None
        epg_source.is_active = True
        epg_source.save(update_fields=["source_type", "file_path", "url", "is_active"])
    return epg_source


def _get_or_create_logo(game: str):
    from apps.channels.models import Logo

    logo, _ = Logo.objects.get_or_create(
        url=GAME_LOGO_URLS[game], defaults={"name": f"{GAME_DISPLAY_NAMES[game]} Esports"}
    )
    return logo


def create_channels(settings: dict) -> dict:
    """Idempotently creates the channel group, one generic channel per
    (game, slot), and the dedicated EPG source."""
    from apps.channels.models import Channel, ChannelGroup

    group, _ = ChannelGroup.objects.get_or_create(name=settings["channel_group_name"])
    epg_source = _get_or_create_epg_source()
    # Channel.stream_profile (separate from Stream.stream_profile) must be
    # set or the channel gray-screens; apply_assignment keeps it in sync
    # with whichever platform is actually live once a match airs.
    stream_profile = _get_stream_profile(settings, "twitch")

    slots = int(settings["slots_per_game"])
    created = []
    for game in GAME_DISPLAY_NAMES:
        logo = _get_or_create_logo(game)
        for slot_index in range(slots):
            channel, was_created = Channel.objects.get_or_create(
                tvg_id=_generic_channel_tvg_id(game, slot_index),
                defaults={
                    "name": _generic_channel_name(game, slot_index),
                    "channel_group": group,
                    "logo": logo,
                    "stream_profile": stream_profile,
                },
            )
            if was_created:
                created.append(channel.name)
            else:
                update_fields = []
                if channel.logo_id != logo.id:
                    channel.logo = logo
                    update_fields.append("logo")
                if channel.stream_profile_id != stream_profile.id:
                    channel.stream_profile = stream_profile
                    update_fields.append("stream_profile")
                if update_fields:
                    channel.save(update_fields=update_fields)

    return {"status": "ok", "created_channels": created, "epg_source_id": epg_source.id}


def _get_stream_profile(settings: dict, platform: str):
    from apps.channels.models import StreamProfile

    name = settings[PLATFORM_STREAM_PROFILE_SETTINGS[platform]]
    try:
        return StreamProfile.objects.get(name=name)
    except StreamProfile.DoesNotExist as exc:
        raise StreamProfileNotFoundError(
            f"No StreamProfile named {name!r} exists for playing {platform} URLs."
        ) from exc


def _get_or_create_owned_stream(settings: dict, platform: str, stream_channel: str):
    from apps.channels.models import Stream

    profile = _get_stream_profile(settings, platform)
    url = PLATFORM_URL_BUILDERS[platform](stream_channel)
    owned, _ = Stream.objects.update_or_create(
        tvg_id=f"esportsarr.stream.{platform}.{stream_channel}",
        defaults={
            "name": stream_channel,
            "url": url,
            "stream_profile": profile,
            "is_custom": True,
            "custom_properties": {"owner": OWNED_STREAM_TAG},
        },
    )
    return owned


def apply_assignment(settings: dict, game: str, assignment: list[dict[str, Any] | None]) -> None:
    """Reassigns ChannelStream priority for one game's slots and links each
    channel to this plugin's EPGSource. Guide content is built separately
    by build_guide_entries."""
    from apps.channels.models import Channel, ChannelStream
    from apps.epg.models import EPGData

    epg_source = _get_or_create_epg_source()

    for slot_index, match in enumerate(assignment):
        tvg_id = _generic_channel_tvg_id(game, slot_index)
        try:
            channel = Channel.objects.get(tvg_id=tvg_id)
        except Channel.DoesNotExist as exc:
            raise RuntimeError(
                f"Channel {tvg_id!r} does not exist yet. Run the plugin's "
                "'Create Channels' action once before enabling the scheduler."
            ) from exc

        epg_data, _ = EPGData.objects.get_or_create(
            tvg_id=tvg_id,
            epg_source=epg_source,
            defaults={"name": channel.name},
        )
        if channel.epg_data_id != epg_data.id:
            channel.epg_data = epg_data
            channel.save(update_fields=["epg_data"])

        if match is None:
            continue

        platform = match["stream_platform"]
        stream = _get_or_create_owned_stream(settings, platform, match["stream_channel"])
        ChannelStream.objects.update_or_create(channel=channel, stream=stream, defaults={"order": 0})
        # Delete rather than demote: an old link to a Twitcharr-owned stream
        # would still match its prune query even at order=1.
        ChannelStream.objects.filter(channel=channel).exclude(stream=stream).delete()

        stream_profile = _get_stream_profile(settings, platform)
        if channel.stream_profile_id != stream_profile.id:
            channel.stream_profile = stream_profile
            channel.save(update_fields=["stream_profile"])


def build_guide_entries(
    game: str,
    projected_by_slot: list[list[tuple[datetime, dict[str, Any]]]],
    now: datetime,
    projection_end: datetime,
) -> list[dict[str, Any]]:
    """Converts project_schedule's `(claimed_at, match)` lists into
    XMLTV-ready guide entries covering `[now - GUIDE_LOOKBACK_HOURS,
    projection_end)` with no gaps, filling idle stretches (including before
    "now") with an explicit "No Match Scheduled" entry so the guide never
    has a stretch with zero programme data. Displayed start is `claimed_at`
    (when the slot actually started showing the match), not the match's own
    start, so a match delayed by slot contention never appears to overlap
    what the slot was still showing."""
    entries: list[dict[str, Any]] = []
    for slot_index, projected in enumerate(projected_by_slot):
        tvg_id = _generic_channel_tvg_id(game, slot_index)
        name = _generic_channel_name(game, slot_index)
        cursor = _round_down_to_quarter_hour(now - timedelta(hours=GUIDE_LOOKBACK_HOURS))

        for claimed_at, match in projected:
            start = claimed_at
            end = datetime.fromisoformat(match["start"]) + duration_for_match(match)
            entries.extend(_filler_entries(tvg_id, name, cursor, start))
            entries.append(
                {
                    "tvg_id": tvg_id,
                    "name": name,
                    "title": match["title"],
                    "description": match.get("description", ""),
                    "start": start,
                    "end": end,
                }
            )
            cursor = end

        entries.extend(_filler_entries(tvg_id, name, cursor, projection_end))

    return entries


def _build_guide_xmltv(entries: list[dict[str, Any]]) -> str:
    tv = ElementTree.Element("tv", attrib={"generator-info-name": "esportsarr"})

    seen_tvg_ids: set[str] = set()
    for entry in entries:
        if entry["tvg_id"] in seen_tvg_ids:
            continue
        seen_tvg_ids.add(entry["tvg_id"])
        channel_el = ElementTree.SubElement(tv, "channel", attrib={"id": entry["tvg_id"]})
        display_name_el = ElementTree.SubElement(channel_el, "display-name")
        display_name_el.text = entry["name"]

    for entry in entries:
        programme_el = ElementTree.SubElement(
            tv,
            "programme",
            attrib={
                "start": entry["start"].strftime(GUIDE_TIME_FORMAT),
                "stop": entry["end"].strftime(GUIDE_TIME_FORMAT),
                "channel": entry["tvg_id"],
            },
        )
        title_el = ElementTree.SubElement(programme_el, "title", attrib={"lang": GUIDE_LANG})
        title_el.text = entry["title"]
        description = entry.get("description")
        if description:
            desc_el = ElementTree.SubElement(programme_el, "desc", attrib={"lang": GUIDE_LANG})
            desc_el.text = description
            category_el = ElementTree.SubElement(programme_el, "category", attrib={"lang": GUIDE_LANG})
            category_el.text = GUIDE_CATEGORY

    ElementTree.indent(tv, space="  ")
    xml_body = ElementTree.tostring(tv, encoding="unicode", xml_declaration=False)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{xml_body}\n'


def write_guide(entries: list[dict[str, Any]]) -> None:
    from apps.epg.tasks import refresh_epg_data

    epg_source = _get_or_create_epg_source()

    os.makedirs(os.path.dirname(GUIDE_FILE_PATH), exist_ok=True)
    with open(GUIDE_FILE_PATH, "w", encoding="utf-8") as f:
        f.write(_build_guide_xmltv(entries))

    refresh_epg_data(epg_source.id, force=True)
