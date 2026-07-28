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
_find_stream_for_twitch_channel accordingly — see plugin/README.md.

All Django imports are deferred into function bodies so this module (and the
plugin package as a whole, apart from this file) stays importable/testable
without Django installed.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

GAME_DISPLAY_NAMES = {"lol": "LoL", "valorant": "Valorant"}

EPG_SOURCE_NAME = "Esportsarr"

# Riot's API gives a match start time but no end time; a Bo3/Bo5 broadcast
# block including pre/post-show reliably runs a few hours. Same estimate the
# scraper uses for esports.xmltv (scraper/esportsarr/xmltv.py).
PROGRAMME_DURATION = timedelta(hours=3)


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
        # "dummy" = Dispatcharr's own category for a manually-managed EPG
        # source (we write EPGData/ProgramData ourselves, never fetch a URL).
        defaults={"source_type": "dummy", "is_active": False},
    )
    return epg_source


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
        for slot_index in range(slots):
            channel, was_created = Channel.objects.get_or_create(
                tvg_id=_generic_channel_tvg_id(game, slot_index),
                defaults={
                    "name": _generic_channel_name(game, slot_index),
                    "channel_group": group,
                },
            )
            if was_created:
                created.append(channel.name)

    return {"status": "ok", "created_channels": created, "epg_source_id": epg_source.id}


def _find_stream_for_twitch_channel(twitch_channel: str):
    from apps.channels.models import Stream

    stream = (
        Stream.objects.filter(url__icontains=f"twitch.tv/{twitch_channel}").first()
        or Stream.objects.filter(tvg_id__iexact=f"twitch.{twitch_channel}").first()
        or Stream.objects.filter(name__iexact=twitch_channel).first()
    )
    if stream is None:
        raise StreamNotFoundError(
            f"No existing Stream found for Twitch channel {twitch_channel!r}. "
            "Expected Twitcharr to have already created one for it — checked "
            "Stream.url, .tvg_id, and .name. If Twitcharr uses a different "
            "field/format on this install, update _find_stream_for_twitch_channel."
        )
    return stream


def apply_assignment(settings: dict, game: str, assignment: list[dict[str, Any] | None]) -> None:
    """Given the allocator's output for one game, reassigns ChannelStream
    priority on the generic channels and writes the matching EPGData/
    ProgramData row. A None slot is left untouched (no live match currently
    holds it) — this plugin never clears a channel's last-known stream."""
    from apps.channels.models import Channel, ChannelStream
    from apps.epg.models import EPGData, ProgramData

    epg_source = _get_or_create_epg_source()

    for slot_index, match in enumerate(assignment):
        tvg_id = _generic_channel_tvg_id(game, slot_index)
        try:
            channel = Channel.objects.get(tvg_id=tvg_id)
        except Channel.DoesNotExist as exc:
            raise RuntimeError(
                f"Channel {tvg_id!r} does not exist yet — run the plugin's "
                "'Create Channels' action once before enabling the scheduler."
            ) from exc

        if match is None:
            continue

        stream = _find_stream_for_twitch_channel(match["twitch_channel"])
        ChannelStream.objects.update_or_create(channel=channel, stream=stream, defaults={"order": 0})
        ChannelStream.objects.filter(channel=channel).exclude(stream=stream).update(order=1)

        epg_data, _ = EPGData.objects.get_or_create(
            tvg_id=tvg_id,
            epg_source=epg_source,
            defaults={"name": channel.name},
        )
        if channel.epg_data_id != epg_data.id:
            channel.epg_data = epg_data
            channel.save(update_fields=["epg_data"])

        start = datetime.fromisoformat(match["start"])
        ProgramData.objects.update_or_create(
            epg=epg_data,
            program_id=f"{match['league']}:{match['start']}",
            defaults={
                "start_time": start,
                "end_time": start + PROGRAMME_DURATION,
                "title": match["title"],
                "tvg_id": tvg_id,
            },
        )
