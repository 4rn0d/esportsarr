"""Django ORM operations for the esportsarr plugin.

Field names below were verified directly against Dispatcharr's actual models
on 2026-07-27:
  https://raw.githubusercontent.com/Dispatcharr/Dispatcharr/main/apps/channels/models.py
  https://raw.githubusercontent.com/Dispatcharr/Dispatcharr/main/apps/epg/models.py

The one part that could NOT be verified against a real install: how Twitcharr
names/URLs the Stream row it creates for a given Twitch channel. `url` is the
most reliable bet (a real Twitch stream URL always contains "twitch.tv/<channel>"
regardless of how Twitcharr names things internally), with tvg_id/name as
fallbacks matching this project's own naming convention. If stream lookups
fail on the real install, check one Stream row in Django admin and adjust
_find_source_stream_for_twitch_channel accordingly — see plugin/README.md.

This plugin never attaches a Stream it doesn't own to one of its own
Channels. An earlier version's `_find_stream_for_twitch_channel` looked up
and directly reused Twitcharr's own Stream row via ChannelStream — which
made our "Valorant 1"/"Valorant 2" channels silently match Twitcharr's own
cleanup query in streamlink_setup.py's `_prune_unmanaged()`:
    Channel.objects.filter(streams__custom_properties__owner=OWNER_TAG)
        .exclude(tvg_id__in=keep_tvg_ids).delete()
Confirmed against Twitcharr's actual source (2026-07-28) as the cause of
our generic channels repeatedly being created then deleted within minutes,
specifically only once they'd been linked to a live match's stream (LoL
channels, never yet linked to any stream during testing, were never
touched). `_get_or_create_owned_stream` fixes this by cloning the playback
config (url/stream_profile/m3u_account/logo_url) from whatever Stream
Twitcharr already set up for that Twitch channel into a *separate* Stream
row this plugin creates and owns (tagged `custom_properties.owner` with
this plugin's own tag, `is_custom=True`), keyed by a stable tvg_id so
repeated ticks update the same clone instead of multiplying rows. Only that
owned clone is ever attached via ChannelStream — Twitcharr's ownership-
based prune query can never match a channel of ours again, regardless of
what it does to its own channels.

Guide data is NOT written as ProgramData rows directly via the ORM. An
earlier version did that, and to keep Dispatcharr's own guide-grid endpoint
(EPGGridAPIView) from overlaying its auto-generated humorous filler on top,
had to use `source_type="xmltv"` — which then made Dispatcharr's *own* EPG
pipeline try to fetch/parse a URL we never set every time a channel's
epg_data link changed, failing and leaving the source stuck showing "Error"
in the UI forever (confirmed harmless to the actual data, but needlessly
alarming, and fighting a status field that isn't ours to manage). The
correct, natively-supported way to do this: EPGSource.file_path (with no
`url`) tells Dispatcharr to parse a local XMLTV file directly — no network
fetch attempted at all, so no error status — and `refresh_epg_data()` is a
real, callable task that parses it into EPGData/ProgramData itself,
atomically (a bad file never destroys existing guide data — confirmed
against that task's source, 2026-07-28). apply_assignment builds the guide
entries; write_guide (called once per tick, after every game's assignment)
renders them to one XMLTV file covering every channel and triggers that
task — Dispatcharr owns EPGData/ProgramData creation/updates from there.

All Django imports are deferred into function bodies so this module (and the
plugin package as a whole, apart from this file) stays importable/testable
without Django installed.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timedelta, timezone
from typing import Any

GAME_DISPLAY_NAMES = {"lol": "LoL", "valorant": "Valorant"}

GAME_LOGO_URLS = {
    "lol": "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcRD8UiwV0BGL9ggO2lM5VzxOy8CoABmOoGPexI3kRh1elWh1mEPgL3-0WxI&s=10",
    "valorant": "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcRFZ3lPk7mJOvXkYbPR9O93DNZw_q-Jt-DBQtl9qjsfW3u--2SpMoKeMyc&s=10",
}

EPG_SOURCE_NAME = "Esportsarr"

# Tags Stream rows this plugin creates/owns, distinct from Twitcharr's own
# OWNER_TAG. Never match Twitcharr's own filter value, whatever it is — this
# is our own tag, not an attempt to imitate or collide with theirs.
OWNED_STREAM_TAG = "esportsarr"

# Riot's API gives a match start time but no end time; a Bo3/Bo5 broadcast
# block including pre/post-show reliably runs a few hours. Same estimate the
# scraper uses for esports.xmltv (scraper/esportsarr/xmltv.py).
PROGRAMME_DURATION = timedelta(hours=3)

OFFLINE_PROGRAM_TITLE = "No Match Scheduled"
# A genuinely idle slot gets this placeholder instead of Dispatcharr's own
# generic dummy-EPG filler bleeding through. Short and re-written every tick
# rather than left long, so a missed poll can't leave a block whose
# end_time has already passed sitting there looking wrong.
OFFLINE_PROGRAM_DURATION = timedelta(minutes=15)

GUIDE_FILE_PATH = "/app/data/plugins/esportsarr/.state/esportsarr-guide.xmltv"
GUIDE_TIME_FORMAT = "%Y%m%d%H%M%S %z"


class StreamNotFoundError(LookupError):
    """No existing Dispatcharr Stream matches a scheduled match's Twitch channel."""


def _generic_channel_name(game: str, slot_index: int) -> str:
    return f"{GAME_DISPLAY_NAMES[game]} {slot_index + 1}"


def _generic_channel_tvg_id(game: str, slot_index: int) -> str:
    return f"esportsarr.{game}.{slot_index + 1}"


def _get_or_create_epg_source():
    from apps.epg.models import EPGSource

    epg_source, _ = EPGSource.objects.get_or_create(
        name=EPG_SOURCE_NAME,
        # is_active must be True — Dispatcharr's parse task checks it as a
        # general "do EPG operations for this source at all" gate, not just
        # "should we fetch a URL". False blocked our own deliberate
        # refresh_epg_data() call too (confirmed: "EPG source N is not
        # active. Skipping." in the logs), not merely unwanted auto-fetches.
        # refresh_interval stays at its default (0 = no periodic schedule)
        # since we trigger refreshes ourselves every tick.
        defaults={"source_type": "xmltv", "file_path": GUIDE_FILE_PATH, "is_active": True},
    )
    # get_or_create's defaults only apply on creation — self-heal an existing
    # row too (e.g. one created before this fix, still "dummy", missing
    # file_path, or inactive).
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
    (game, slot), and the dedicated EPG source. Safe to run more than once —
    triggered manually via the plugin's "Create Channels" action."""
    from apps.channels.models import Channel, ChannelGroup

    group, _ = ChannelGroup.objects.get_or_create(name=settings["channel_group_name"])
    epg_source = _get_or_create_epg_source()

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
                },
            )
            if was_created:
                created.append(channel.name)
            elif channel.logo_id != logo.id:
                channel.logo = logo
                channel.save(update_fields=["logo"])

    return {"status": "ok", "created_channels": created, "epg_source_id": epg_source.id}


def _find_source_stream_for_twitch_channel(twitch_channel: str):
    """Locates whatever Stream Twitcharr (or anything else) already set up
    for this Twitch channel, to copy its playback config from. Excludes our
    own previously-cloned streams so a later tick can't mistake its own
    clone for the source and start copying from itself."""
    from apps.channels.models import Stream

    candidates = Stream.objects.exclude(custom_properties__owner=OWNED_STREAM_TAG)
    stream = (
        candidates.filter(url__icontains=f"twitch.tv/{twitch_channel}").first()
        or candidates.filter(tvg_id__iexact=f"twitch.{twitch_channel}").first()
        or candidates.filter(name__iexact=twitch_channel).first()
    )
    if stream is None:
        raise StreamNotFoundError(
            f"No existing Stream found for Twitch channel {twitch_channel!r}. "
            "Expected Twitcharr to have already created one for it — checked "
            "Stream.url, .tvg_id, and .name. If Twitcharr uses a different "
            "field/format on this install, update _find_source_stream_for_twitch_channel."
        )
    return stream


def _get_or_create_owned_stream(twitch_channel: str):
    """Clones the playback config of whatever Stream already exists for this
    Twitch channel into a separate row this plugin owns, so ChannelStream
    never links one of our Channels directly to a Stream Twitcharr owns —
    see this module's docstring for why that matters."""
    from apps.channels.models import Stream

    source = _find_source_stream_for_twitch_channel(twitch_channel)
    owned, _ = Stream.objects.update_or_create(
        tvg_id=f"esportsarr.stream.{twitch_channel}",
        defaults={
            "name": source.name,
            "url": source.url,
            "logo_url": source.logo_url,
            "m3u_account": source.m3u_account,
            "stream_profile": source.stream_profile,
            "is_custom": True,
            "custom_properties": {"owner": OWNED_STREAM_TAG},
        },
    )
    return owned


def apply_assignment(
    settings: dict,
    game: str,
    assignment: list[dict[str, Any] | None],
    reserved_for: list[dict[str, Any] | None] | None = None,
) -> list[dict[str, Any]]:
    """Given the allocator's output for one game, reassigns ChannelStream
    priority on the generic channels and returns this game's guide entries —
    the caller collects these across every game and calls write_guide() once
    per tick, since all games' channels share one EPGSource/guide file.

    - Live match (`assignment[i]` is not None): its stream becomes `order=0`,
      guide entry shows the match itself.
    - No live match but `reserved_for[i]` is set: a "coming up" guide entry
      for the anticipated match. `ChannelStream` is left untouched — this
      plugin never clears a channel's last-known stream just because a slot
      is reserved rather than occupied.
    - Neither: an explicit "No Match Scheduled" placeholder, so a genuinely
      idle slot shows something honest instead of Dispatcharr's own generic
      dummy-EPG filler.
    """
    from apps.channels.models import Channel, ChannelStream
    from apps.epg.models import EPGData

    reserved_for = reserved_for or [None] * len(assignment)
    epg_source = _get_or_create_epg_source()
    now = datetime.now(timezone.utc)
    entries: list[dict[str, Any]] = []

    for slot_index, match in enumerate(assignment):
        tvg_id = _generic_channel_tvg_id(game, slot_index)
        name = _generic_channel_name(game, slot_index)
        try:
            channel = Channel.objects.get(tvg_id=tvg_id)
        except Channel.DoesNotExist as exc:
            raise RuntimeError(
                f"Channel {tvg_id!r} does not exist yet — run the plugin's "
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

        if match is not None:
            stream = _get_or_create_owned_stream(match["twitch_channel"])
            ChannelStream.objects.update_or_create(channel=channel, stream=stream, defaults={"order": 0})
            ChannelStream.objects.filter(channel=channel).exclude(stream=stream).update(order=1)

            start = datetime.fromisoformat(match["start"])
            entries.append(
                {"tvg_id": tvg_id, "name": name, "title": match["title"], "start": start, "end": start + PROGRAMME_DURATION}
            )
            continue

        upcoming = reserved_for[slot_index]
        if upcoming is not None:
            start = datetime.fromisoformat(upcoming["start"])
            entries.append(
                {"tvg_id": tvg_id, "name": name, "title": upcoming["title"], "start": start, "end": start + PROGRAMME_DURATION}
            )
            continue

        entries.append(
            {"tvg_id": tvg_id, "name": name, "title": OFFLINE_PROGRAM_TITLE, "start": now, "end": now + OFFLINE_PROGRAM_DURATION}
        )

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
        title_el = ElementTree.SubElement(programme_el, "title")
        title_el.text = entry["title"]

    ElementTree.indent(tv, space="  ")
    xml_body = ElementTree.tostring(tv, encoding="unicode", xml_declaration=False)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{xml_body}\n'


def write_guide(entries: list[dict[str, Any]]) -> None:
    """Renders every game's guide entries (collected by the caller across
    all apply_assignment calls this tick) into one XMLTV file and triggers
    Dispatcharr's own local-file EPG refresh to parse it — see module
    docstring for why this replaces writing ProgramData via the ORM."""
    from apps.epg.tasks import refresh_epg_data

    epg_source = _get_or_create_epg_source()

    os.makedirs(os.path.dirname(GUIDE_FILE_PATH), exist_ok=True)
    with open(GUIDE_FILE_PATH, "w", encoding="utf-8") as f:
        f.write(_build_guide_xmltv(entries))

    refresh_epg_data(epg_source.id, force=True)
