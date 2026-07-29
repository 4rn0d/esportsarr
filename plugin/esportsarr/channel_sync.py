"""Django ORM operations for the esportsarr plugin.

Field names below were verified directly against Dispatcharr's actual models
on 2026-07-27:
  https://raw.githubusercontent.com/Dispatcharr/Dispatcharr/main/apps/channels/models.py
  https://raw.githubusercontent.com/Dispatcharr/Dispatcharr/main/apps/epg/models.py

This plugin builds and owns its own Stream row per channel/platform
(`_get_or_create_owned_stream`) instead of depending on Twitcharr already
tracking that exact channel. Two earlier designs were tried and rejected:

- Directly reusing Twitcharr's own Stream row via ChannelStream made our
  "Valorant 1"/"Valorant 2" channels silently match Twitcharr's own cleanup
  query in streamlink_setup.py's `_prune_unmanaged()`:
      Channel.objects.filter(streams__custom_properties__owner=OWNER_TAG)
          .exclude(tvg_id__in=keep_tvg_ids).delete()
  Confirmed against Twitcharr's actual source (2026-07-28) as the cause of
  our generic channels repeatedly being created then deleted within minutes.
- Cloning an existing Twitcharr-managed Stream's url/profile avoided that,
  but still required Twitcharr to already be tracking the *specific* Twitch
  channel a match happened to be on, just so a Stream row existed to copy
  from, annoying, since adding a new league meant configuring Twitcharr for
  a channel it otherwise had no reason to care about.

The actual constraint is narrower than either design assumed: confirmed via
Dispatcharr's own StreamProfile model plus Twitcharr's source (2026-07-28),
Dispatcharr has *no* built-in way to play a raw twitch.tv URL. Twitch
playback only works because Twitcharr installs its own streamlink-based
StreamProfile (name checked via `twitch_stream_profile_name`, a setting, not
hardcoded, see `_get_stream_profile`) and attaches it to every Stream it
creates. That profile is a single system-wide object, unrelated to which
channels Twitcharr happens to track. So this plugin only needs Twitcharr to
have run once, ever, never to know about any specific league's channel. The
channel/handle (and therefore the playable URL) is already known directly
from the scraper's own `epg_channel_id` mapping, so `_get_or_create_owned_stream`
builds the Stream itself (`PLATFORM_URL_BUILDERS`), borrowing only the
shared per-platform profile (`PLATFORM_STREAM_PROFILE_SETTINGS`), tagged
`custom_properties.owner` with this plugin's own tag and `is_custom=True`,
keyed by a stable tvg_id so repeated ticks update the same row instead of
multiplying them. Only that owned Stream is ever attached via ChannelStream.
Twitcharr's ownership-based prune query can never match a channel of ours.

LPL is YouTube-only (no Twitch stream exists for it at all), so this same
pattern extends to a second platform. Unlike Twitch, no equivalent plugin is
confirmed to install a working streamlink/yt-dlp-capable StreamProfile for
YouTube automatically (checked youtubearr's source, 2026-07-29: it resolves
a temporary direct media URL via yt-dlp itself and plays it through
Dispatcharr's stock "proxy" profile, a different mechanism entirely, not a
reusable streamlink profile). `youtube_stream_profile_name` defaults to the
same value as the Twitch setting as an unverified first guess (streamlink's
YouTube plugin ships in the same package, so it might just work), not a
confirmed-working setup, see plugin/README.md.

Guide data is NOT written as ProgramData rows directly via the ORM. An
earlier version did that, and to keep Dispatcharr's own guide-grid endpoint
(EPGGridAPIView) from overlaying its auto-generated humorous filler on top,
had to use `source_type="xmltv"`, which then made Dispatcharr's *own* EPG
pipeline try to fetch/parse a URL we never set every time a channel's
epg_data link changed, failing and leaving the source stuck showing "Error"
in the UI forever (confirmed harmless to the actual data, but needlessly
alarming, and fighting a status field that isn't ours to manage). The
correct, natively-supported way to do this: EPGSource.file_path (with no
`url`) tells Dispatcharr to parse a local XMLTV file directly, no network
fetch attempted at all, so no error status, and `refresh_epg_data()` is a
real, callable task that parses it into EPGData/ProgramData itself,
atomically (a bad file never destroys existing guide data, confirmed
against that task's source, 2026-07-28). build_guide_entries builds the
guide entries, from allocator.project_schedule's forward-looking per-slot
match lists rather than one tick's reactive state, see its own docstring;
write_guide (called once per tick, after every game's assignment) renders
them to one XMLTV file covering every channel and triggers that task.
Dispatcharr owns EPGData/ProgramData creation/updates from there.

All Django imports are deferred into function bodies so this module (and the
plugin package as a whole, apart from this file) stays importable/testable
without Django installed.
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

# Most leagues are Twitch; LPL is YouTube-only. Both the settings key holding
# that platform's StreamProfile name, and the template for building a stable
# watch URL from the scraper's stream_channel, are keyed by platform so
# adding a third one later is a two-line change, not a rewrite. The YouTube
# URL format and profile are unverified against a real install as of
# 2026-07-29 (no YouTube-capable StreamProfile exists yet), unlike the
# Twitch side, which has been confirmed working, see plugin/README.md.
PLATFORM_STREAM_PROFILE_SETTINGS = {
    "twitch": "twitch_stream_profile_name",
    "youtube": "youtube_stream_profile_name",
}
PLATFORM_URL_BUILDERS = {
    "twitch": lambda channel: f"https://twitch.tv/{channel}",
    "youtube": lambda channel: f"https://www.youtube.com/@{channel}/live",
}

# Tags Stream rows this plugin creates/owns, distinct from Twitcharr's own
# OWNER_TAG. Never match Twitcharr's own filter value, whatever it is, this
# is our own tag, not an attempt to imitate or collide with theirs.
OWNED_STREAM_TAG = "esportsarr"

# Riot's API gives a match start time but no end time; a Bo3/Bo5 broadcast
# block including pre/post-show reliably runs a few hours. Same estimate the
# scraper uses for esports.xmltv (scraper/esportsarr/xmltv.py).
PROGRAMME_DURATION = timedelta(hours=3)

OFFLINE_PROGRAM_TITLE = "No Match Scheduled"
# A genuinely idle stretch gets this placeholder instead of Dispatcharr's own
# generic dummy-EPG filler bleeding through. build_guide_entries sizes each
# placeholder to the actual known gap (up to the next real match, or to
# projection_end if nothing's scheduled at all), no fixed duration needed
# here anymore now that the whole projection window is covered every tick.

GUIDE_FILE_PATH = "/app/data/plugins/esportsarr/.state/esportsarr-guide.xmltv"
GUIDE_TIME_FORMAT = "%Y%m%d%H%M%S %z"
GUIDE_LANG = "en"
GUIDE_CATEGORY = "Esports"


class StreamProfileNotFoundError(LookupError):
    """The shared Twitch-capable StreamProfile (see _get_stream_profile) doesn't exist."""


def _generic_channel_name(game: str, slot_index: int) -> str:
    return f"{GAME_DISPLAY_NAMES[game]} {slot_index + 1}"


def _generic_channel_tvg_id(game: str, slot_index: int) -> str:
    return f"esportsarr.{game}.{slot_index + 1}"


QUARTER_HOUR_MINUTES = 15


def _round_down_to_quarter_hour(moment: datetime) -> datetime:
    """A guide reads cleaner with block boundaries on the quarter-hour
    (:00/:15/:30/:45) instead of whatever odd second a poll tick happened to
    land on. Only used for the offline placeholder's start. A live/reserved
    match's guide entry always keeps its own real time, never rounded."""
    return moment.replace(minute=(moment.minute // QUARTER_HOUR_MINUTES) * QUARTER_HOUR_MINUTES, second=0, microsecond=0)


def _get_or_create_epg_source():
    from apps.epg.models import EPGSource

    epg_source, _ = EPGSource.objects.get_or_create(
        name=EPG_SOURCE_NAME,
        # is_active must be True. Dispatcharr's parse task checks it as a
        # general "do EPG operations for this source at all" gate, not just
        # "should we fetch a URL". False blocked our own deliberate
        # refresh_epg_data() call too (confirmed: "EPG source N is not
        # active. Skipping." in the logs), not merely unwanted auto-fetches.
        # refresh_interval stays at its default (0 = no periodic schedule)
        # since we trigger refreshes ourselves every tick.
        defaults={"source_type": "xmltv", "file_path": GUIDE_FILE_PATH, "is_active": True},
    )
    # get_or_create's defaults only apply on creation. Self-heal an existing
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
    (game, slot), and the dedicated EPG source. Safe to run more than once,
    triggered manually via the plugin's "Create Channels" action."""
    from apps.channels.models import Channel, ChannelGroup

    group, _ = ChannelGroup.objects.get_or_create(name=settings["channel_group_name"])
    epg_source = _get_or_create_epg_source()
    # Channel.stream_profile is separate from Stream.stream_profile and takes
    # priority when the channel itself is played (falls back to Dispatcharr's
    # system-default profile if unset, confirmed against Channel's
    # effective_stream_profile_obj/get_stream_profile, 2026-07-28). Setting
    # it only on the owned Stream (_get_or_create_owned_stream) is not
    # enough, the channel gray-screens without this too. Twitch is the
    # default here since almost every league is Twitch; apply_assignment
    # switches it to whichever platform's profile is actually live on a slot
    # once a real match airs there (see its own docstring).
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
    """Dispatcharr has no built-in way to play a raw twitch.tv/youtube.com
    URL, confirmed against Dispatcharr's own StreamProfile model and
    Twitcharr's source (2026-07-28): Twitch playback only works at all
    because Twitcharr installs its own StreamProfile (a streamlink-based
    command/parameters template) and attaches it to every Stream it creates.
    That profile is a single system-wide object, not tied to any specific
    channel, so this plugin only needs Twitcharr to have run once (creating
    it), never to be tracking the specific league/channel a match is on.
    `twitch_stream_profile_name`/`youtube_stream_profile_name` are settings
    rather than hardcoded names because the exact profile name is an
    external identifier this plugin doesn't own. It can drift if Twitcharr
    renames it or a different install uses its own streamlink-capable
    profile. The YouTube side has no Twitcharr-equivalent confirmed to exist
    yet, unlike Twitch. See plugin/README.md for what to set it up with."""
    from apps.channels.models import StreamProfile

    name = settings[PLATFORM_STREAM_PROFILE_SETTINGS[platform]]
    try:
        return StreamProfile.objects.get(name=name)
    except StreamProfile.DoesNotExist as exc:
        raise StreamProfileNotFoundError(
            f"No StreamProfile named {name!r} exists for playing {platform} URLs. "
            "Twitcharr creates one for Twitch the first time it runs, even if it "
            "isn't tracking any of esportsarr's channels, install/run it at least "
            "once for the Twitch side. For YouTube, no equivalent plugin creates "
            "one automatically as of this writing, you'll need a streamlink- or "
            "yt-dlp-capable StreamProfile set up manually, then point the "
            "matching setting at its exact name."
        ) from exc


def _get_or_create_owned_stream(settings: dict, platform: str, stream_channel: str):
    """Builds this plugin's own Stream for a channel on the given platform
    directly, no lookup against an existing Twitcharr-managed Stream needed,
    since the channel/handle (and therefore the URL) is already known from
    the scraper's own league mapping. Only the shared per-platform
    StreamProfile (see _get_stream_profile) is borrowed."""
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
    """Given the allocator's `assignment` for one game (the live match, if
    any, currently occupying each slot), reassigns ChannelStream priority on
    the generic channels and makes sure each is linked to this plugin's
    EPGSource. Guide *content* is built separately now, from
    `build_guide_entries` working off `allocator.project_schedule`'s
    forward-looking per-slot match lists. That covers a whole future
    window accurately instead of this function guessing one tick at a time
    (an earlier version did exactly that, including a "next up" preview
    based on a same-estimated-duration heuristic; the projection is simply
    better-informed, so that guesswork was removed rather than duplicated).
    Also keeps a live channel's `stream_profile` in sync with whichever
    platform (Twitch/YouTube) is actually occupying its slot right now,
    since different matches on the same slot over time can be on different
    platforms (e.g. LoL 1 shows LCS today, LPL tomorrow).
    """
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
        # Delete, don't just demote to order=1: a channel that had been
        # linked to a Twitcharr-owned Stream before this plugin switched
        # to cloning its own (see _get_or_create_owned_stream) would
        # otherwise keep that old link forever, order=1 or not, and
        # Twitcharr's own prune query matches on *any* linked stream
        # carrying its owner tag, not just the active one. We only ever
        # need the current match's stream; nothing reads old links back.
        ChannelStream.objects.filter(channel=channel).exclude(stream=stream).delete()

        # Channel.stream_profile (set once by create_channels) takes priority
        # over the Stream's own for channel-level playback, and a slot can
        # show different platforms over time (e.g. LoL 1 airs LCS today,
        # LPL tomorrow) - keep it pointed at whichever platform is actually
        # live here right now, not just whatever create_channels set once.
        stream_profile = _get_stream_profile(settings, platform)
        if channel.stream_profile_id != stream_profile.id:
            channel.stream_profile = stream_profile
            channel.save(update_fields=["stream_profile"])


def build_guide_entries(
    game: str,
    projected_by_slot: list[list[dict[str, Any]]],
    now: datetime,
    projection_end: datetime,
) -> list[dict[str, Any]]:
    """Converts `allocator.project_schedule`'s per-slot chronological match
    lists into XMLTV-ready guide entries covering `[now, projection_end)`
    with zero gaps: before the first known match, between two consecutive
    ones, and after the last one, an explicit "No Match Scheduled"
    placeholder fills the space. Otherwise Dispatcharr's guide grid would
    show its own "No program data" gap or auto-generated filler instead.
    This is the sole source of guide *content*; `apply_assignment` only
    handles ChannelStream/EPGData linking now (see its docstring). Pure, no
    Django dependency, every displayed time is either a match's own real
    scheduled/actual start (never rewritten, even if that means two
    consecutive entries end up implying an overrun) or an exact boundary
    already known from a neighboring real entry; only the very first filler
    segment's start is rounded (see `_round_down_to_quarter_hour`), since
    `now` is the one boundary that's inherently an arbitrary instant. Each
    real match entry also carries its `description` (competition/stage
    context, e.g. "LCS: Playoffs" from the scraper's riot_api.py) straight
    through into the XMLTV `<desc>` element (see `_build_guide_xmltv`).
    Filler entries have none, there's no match to describe.
    """
    entries: list[dict[str, Any]] = []
    for slot_index, projected in enumerate(projected_by_slot):
        tvg_id = _generic_channel_tvg_id(game, slot_index)
        name = _generic_channel_name(game, slot_index)
        cursor = _round_down_to_quarter_hour(now)

        for match in projected:
            start = datetime.fromisoformat(match["start"])
            end = start + PROGRAMME_DURATION
            if start > cursor:
                entries.append(
                    {
                        "tvg_id": tvg_id,
                        "name": name,
                        "title": OFFLINE_PROGRAM_TITLE,
                        "description": "",
                        "start": cursor,
                        "end": start,
                    }
                )
            entries.append(
                {
                    "tvg_id": tvg_id,
                    "name": name,
                    "title": match["title"],
                    # Competition/stage context (e.g. "LCS: Playoffs") from
                    # the scraper's riot_api.py, empty for the offline
                    # filler blocks below, which have no match to describe.
                    "description": match.get("description", ""),
                    "start": start,
                    "end": end,
                }
            )
            cursor = end

        if cursor < projection_end:
            entries.append(
                {
                    "tvg_id": tvg_id,
                    "name": name,
                    "title": OFFLINE_PROGRAM_TITLE,
                    "description": "",
                    "start": cursor,
                    "end": projection_end,
                }
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
    """Renders every game's guide entries (collected by the caller across
    all build_guide_entries calls this tick) into one XMLTV file and
    triggers Dispatcharr's own local-file EPG refresh to parse it, see
    module docstring for why this replaces writing ProgramData via the ORM."""
    from apps.epg.tasks import refresh_epg_data

    epg_source = _get_or_create_epg_source()

    os.makedirs(os.path.dirname(GUIDE_FILE_PATH), exist_ok=True)
    with open(GUIDE_FILE_PATH, "w", encoding="utf-8") as f:
        f.write(_build_guide_xmltv(entries))

    refresh_epg_data(epg_source.id, force=True)
