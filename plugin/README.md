# esportsarr (Dispatcharr plugin)

Consolidates per-league esports channels (Twitch, plus YouTube for
YouTube-only leagues like LPL) into a fixed number of generic channels per
game, switching the active stream to whichever live match currently holds
priority. See the top-level README for how this fits with the scraper.

## Local testing (before touching the server)

Only `allocator.py` has zero Django/Dispatcharr dependency and can run
outside the real environment:

```bash
cd plugin
pip install -e ".[dev]"
pytest
```

`channel_sync.py` and the scheduler thread in `plugin.py` can't be
meaningfully unit-tested without a real Dispatcharr instance, see
"Deploying" below for how to verify those instead.

## Deploying to the Debian server

This project doesn't run any commands on your server directly. These are
the steps to run yourself:

1. Copy the plugin folder into the Dispatcharr container's plugin directory:
   ```bash
   docker cp plugin/esportsarr <dispatcharr-container>:/app/data/plugins/esportsarr
   ```
2. Reload plugins so Dispatcharr picks it up:
   ```bash
   curl -X POST http://<dispatcharr-host>/api/plugins/plugins/reload/
   ```
3. In the Dispatcharr UI, open the plugin's settings and fill in at least
   `schedule_url` (the raw GitHub URL to `scraper/output/schedule.json` once
   the scraper repo is pushed and the workflow has run at least once).

## First run, do this before enabling the automatic scheduler

1. **Verify the StreamProfile settings.** This plugin builds its own Stream
   rows directly (`https://twitch.tv/<channel>` or
   `https://www.youtube.com/@<channel>/live`), but Dispatcharr can't play
   either URL without a streamlink-capable `StreamProfile`.
   - **Twitch**: Twitcharr installs one, and `twitch_stream_profile_name`
     (default `"Twitcharr (ad-free, low-latency)"`) must match its exact
     name. Twitcharr only needs to have run once, ever. It does **not** need
     to be tracking any of the specific leagues/channels esportsarr manages.
     Confirmed working (2026-07-29).
   - **YouTube** (for LPL): nothing is confirmed to exist for this yet.
     `youtube_stream_profile_name` defaults to the *same* profile as Twitch,
     purely as an unverified guess (streamlink's YouTube plugin ships in the
     same package, so it might resolve `/@handle/live` URLs fine, but this
     has not been tested against a real LPL broadcast). If it gray-screens,
     you'll need a streamlink- or yt-dlp-capable `StreamProfile` set up for
     YouTube specifically, then point the setting at its exact name. See
     "Why generic channels use their own Stream, not Twitcharr's" below for
     the full reasoning, and channel_sync.py's module docstring for what was
     checked (including why youtubearr's own approach doesn't transfer
     directly).
   Check the real names if unsure:
   ```python
   from apps.channels.models import StreamProfile
   StreamProfile.objects.values("name")
   ```

2. **Run "Create Channels"** (the plugin action in the Dispatcharr UI). This
   creates the channel group + generic channels + EPG source, and sets each
   channel's own `stream_profile` to the Twitch-capable one above (the
   default assumption; `apply_assignment` switches it to whichever
   platform's profile is actually live on a slot once a real match airs
   there, e.g. LPL vs LCS on the same "LoL 1" channel over time).
   Idempotent, safe to run again later if you change `slots_per_game` or
   either profile-name setting. **Required, not optional**: a channel's own
   `stream_profile` is separate from whatever profile its active Stream has,
   takes priority when the channel itself is played, and falls back to
   Dispatcharr's system-default (non-Twitch-capable) profile if never set,
   confirmed as the cause of a channel gray-screening even though its Stream
   played fine standalone (2026-07-28).

3. **Run "Sync Now" manually** during a window when a tracked league has a
   known live match, then check in the Dispatcharr UI (Channels screen, same
   view as the screenshot from the original brainstorm) that:
   - The live match's stream is now `order=0` (top) on the right generic
     channel (e.g. "Valorant 1").
   - The channel's EPG guide entry shows the match title/time, not just
     Online/Offline.
   - The channel actually plays. Do this once for a Twitch league and,
     separately, once during an LPL match specifically, since the YouTube
     side is unverified, see step 1.

4. Only once that looks right, leave `poll_interval_seconds` at its default
   (60s). The background scheduler thread starts automatically whenever the
   plugin loads in the Celery worker process (not the web-server process,
   same split Twitcharr uses) and will keep re-running "Sync Now" on its own
   from then on.

## Publishing to the official Dispatcharr Plugins repo

Listed as an "external" plugin per `github.com/Dispatcharr/Plugins`'s own
recommendation: our code stays in this repo, only a thin `plugin.json` gets
submitted to their repo. `plugin.json` here already has `license`, `author`
(`4rn0d`), `source_type: "external"`, `source_url`, and `repo_url` filled in.

Zipping, tagging, and releasing are now automated by
`.github/workflows/release-please.yml` (see that section below). Once this
repo is pushed, everything except the actual GitHub-account actions is
hands-off:

1. Push this repo to `github.com/4rn0d/esportsarr`, writing
   commits as [Conventional Commits](https://www.conventionalcommits.org/)
   (`fix: ...`, `feat: ...`, `feat!: ...` or a `BREAKING CHANGE:` footer for a
   major bump) from here on. release-please reads these to decide the next
   version.
2. release-please opens a "release PR" on its own after any `fix:`/`feat:`
   commit lands on `main`, showing the computed version bump. Merge it when
   ready; that merge is what actually cuts the tag, GitHub Release, and
   uploads `esportsarr.zip` (see workflow below).
3. Fork `github.com/Dispatcharr/Plugins`, add
   `plugins/esportsarr/plugin.json` (copy of this project's
   `plugin/esportsarr/plugin.json`) plus an optional README/logo, and
   open a PR. Their CI checks: valid semver, folder is lowercase-kebab-case,
   `author` matches the GitHub account opening the PR, CodeQL + ClamAV scans.
4. Any future update: same `fix:`/`feat:` commits, merge the release-please
   PR when it opens, then open a follow-up PR against Dispatcharr/Plugins
   updating just the `version` field in their copy to match the new tag.

### How the version bump is automated

`release-please-config.json` + `.release-please-manifest.json` (repo root)
and `.github/workflows/release-please.yml` do the following on every push to
`main`:

- Parse commit messages since the last release to compute the next semver.
- Patch `plugin/esportsarr/plugin.json`'s `"version"` field directly
  (via release-please's `extra-files` JSON updater) as part of the release PR.
- On merge, tag as `esportsarr-v{version}` (matching `plugin.json`'s
  `source_url` placeholder exactly), create the GitHub Release, zip
  `plugin/esportsarr/`, and attach it as `esportsarr.zip`.

Uses the default `GITHUB_TOKEN` (no extra secret needed). The one tradeoff
is that commits made by release-please itself won't trigger other workflows
in this repo (a GitHub Actions limitation to prevent infinite loops), which
doesn't matter here since `scraper/`'s cron workflow is independent of this
one.

## Adjusting overflow/priority behavior

Priority is split into three tiered settings per game -- e.g.
`league_priority_lol_international` / `_regional` / `_qualifiers` (and the
same three for Valorant) -- rather than one long comma list, so each field
stays short and readable as leagues get added. Tiers rank International >
Regional > Qualifiers; within a tier, earlier in that tier's list = higher
priority. All three tiers are concatenated, in that order, before being fed
to the allocator, so this is a pure UI reorganization -- the resulting
priority order is identical to the old single-field layout. A league name
must match Riot's `league` field exactly (`python -m esportsarr.list_leagues
--game <game>` in the scraper repo), e.g. `"Game Changers NA"`, not
`"Game Changers Americas"`.

`sync_now`'s result includes a `priority_warnings` entry per game listing
any league it saw live in this fetch that isn't in any of that game's three
tiers -- catching both a forgotten entry and a typo'd one (a typo means the
*correct* name shows up here as unranked, since it never matched what you
typed).

Assignment is sticky: a live match keeps its slot until it ends, even if a
higher-priority match starts in the meantime. **This is never overridden,
an already-live match is never bumped out of its slot.** What *does* happen
is reservation of *empty* slots, split into two windows because esports
broadcasts typically go live on Twitch ~1h before the official match time
(pre-show), not right at it:

When a match ends and its league's broadcast immediately moves on to its
next match on the exact same Twitch channel (a series continuing back to
back), that new match claims the *same* generic channel it was already on
-- but only among matches that already won a slot by priority. Same-channel
continuity decides *which* slot number a priority winner lands on, never
*whether* it wins one: it can never cost a genuinely higher-priority match
its slot just because a lower-priority match happens to share a channel
with whatever used to be there (confirmed as a real bug, 2026-07-30: VCT
EMEA and Game Changers EMEA air on the same regional Twitch channel, and a
Last Chance Qualifier match on a completely different channel was losing
out to a lower-priority Game Changers match purely because the latter
"continued" the freed slot's previous channel).

- `reservation_lookahead_minutes` (default 180): how far ahead an upcoming
  match can preview/reserve a slot that's genuinely idle. Nothing live
  wants it either way, so there's no cost to previewing it from the full
  pre-show window.
- `reservation_priority_minutes` (default 120, must be <= the lookahead
  above): how close to start an upcoming match has to be to actually
  compete for a slot that's about to free up. Wide enough to cover typical
  multi-hour gaps between back-to-back matches sharing a regional channel.
  It never costs a live regional match its slot just because an
  international happens to be scheduled sooner.

Either way, a reserved/previewed slot keeps showing whatever stream was
already on that channel. It does not go blank while waiting.

See `allocator.py`'s docstring and `tests/test_allocator.py` for the exact
policy and edge cases, including the near-vs-far distinction.

The week-ahead guide projection (`project_schedule`) replays these same
near/far reservation windows at every simulated point in time, not just
"now" -- confirmed as a real bug, 2026-07-29: Last Chance Qualifier Americas
outranks Game Changers EMEA, but a match starting at 19:00 lost its first
two hours in the guide because three lower-and-equal-priority matches had
already sticky-locked all 3 slots at 18:00, an hour before LCQ's own start.
Without replaying the reservation windows, the projection has no way to
know LCQ deserved one of those slots before the fact.

Riot's `state` field isn't reliable for every league tier (confirmed as a
real bug, 2026-07-29: Game Changers EMEA matches stayed `"unstarted"` in
`schedule.json` more than 30 minutes after their real start while actually
airing, so they were only ever reserved, never displayed). `plugin.py`'s
`_classify_matches` also treats an `"unstarted"` match as live once its
scheduled start has passed, up to `STALE_LIVE_GRACE_MINUTES` (12h) -- past
that, it's presumed stale data rather than a genuinely long-running match
and falls back to the ordinary near/far reservation buckets.

## What the guide shows, a week-ahead projection, not a one-tick snapshot

The guide covers `GUIDE_LOOKBACK_HOURS` (12h, `channel_sync.py`) before "now"
through `schedule_projection_days` (default 7) into the future, built fresh
every tick, with zero gaps -- including before "now". Otherwise Dispatcharr's
own generic placeholder filler ("Lunchtime Laziness...", "Evening
Escapism...") would show through instead, and a slot idle for a while
before "now" would have zero programme data for that stretch, which
Dispatcharr's grid renders as a blank hole rather than "No Match Scheduled"
(confirmed as a real bug, 2026-07-29 -- a slot with a still-live match
happened to already cover the time before "now" and looked fine, while an
idle one right next to it was visibly blank). This used to be a purely
reactive, one-entry-per-slot guess at "what's happening right now plus
maybe one preview"; it's now a genuine forward simulation:

`_classify_matches` (`plugin.py`) also feeds recently-`completed` matches
into the projection, not just live/upcoming ones, as long as they started
within `GUIDE_LOOKBACK_HOURS` -- otherwise the guide has zero record of what
actually aired in a slot once its match ends, and the historical portion
degrades to one giant "No Match Scheduled" the moment nothing's currently
live there, even though real matches did air (same 2026-07-29 bug: only
slots with something *still* live looked right, everything else showed a
12h-wide filler instead of the real history).

- `allocator.project_schedule` replays the exact same `assign_slots` policy
  (priority ranking, sticky live matches, same-channel continuity) forward
  across every known future match, seeded with this tick's real, current
  assignment so the projected future picks up exactly where "right now"
  left off. Two matches from different leagues that overlap in time and
  both want the same slot are resolved exactly like live sync: the
  higher-priority one shows, the lower-priority one is simply **absent**
  from the guide for that window, never queued or shown after the fact.
- `channel_sync.build_guide_entries` turns that per-slot match sequence
  into actual guide entries, filling every gap (before the first known
  match, between two consecutive ones, and after the last one through to
  the end of the projection window) with an explicit "No Match Scheduled"
  placeholder. Only the very first placeholder's start (right at "now") is
  rounded down to the nearest :00/:15/:30/:45 (`_round_down_to_quarter_hour`).
  A guide reads oddly with a block starting at 3:53pm. Every other
  boundary is already an exact real timestamp (a match's own start, or the
  projection window's edge), so nothing else needs rounding.
- Every real entry's displayed time is always the match's own real
  scheduled/actual start, never rewritten to avoid overlapping a
  neighboring entry. Same as a real TV guide: "Antichambre" stays printed
  at 21h00 even when the game before it runs to 21h10. The schedule
  doesn't get rewritten, the overlap is just the honest picture of a delay.
- A match reported as currently live whose duration estimate has already
  technically elapsed (e.g. a best-of-5 running long) can get dropped from
  the *projected future* portion of the guide slightly earlier than it
  actually ends in reality, the same "Riot gives no real end time, this is
  just an estimate" limitation this whole plugin already accepts, and it
  self-corrects on the very next tick since the guide is fully rebuilt every
  time from the latest known state. It never affects the actual live stream
  switch, which is decided separately and correctly by `assign_slots`'s own
  real, current-tick evaluation. `channel_sync.duration_for_match` estimates
  by best-of format (`BEST_OF_DURATIONS`) rather than one flat 3h for every
  match -- Bo1 ~1h, Bo3 ~2h45, Bo5 ~5h30 -- so this caveat is smaller than it
  used to be, not eliminated. Bo7 has an entry too (~7h30) even though no
  league we track uses it yet (Rocket League, planned); that estimate is an
  unvalidated placeholder. Must be kept in sync manually with the identical
  table in `scraper/esportsarr/xmltv.py` -- the two packages don't share code.
- Every real entry also carries a `description` (competition + stage/matchday
  context plus the best-of format when Riot reports one, e.g. "LCS: Playoffs
  · Bo3", "VCT Americas: Week 3 · Bo3") straight through
  from the scraper's `schedule.json` into the XMLTV `<desc>` element, so the
  guide shows what's actually being played, not just the two team names.
  Filler ("No Match Scheduled") entries have none. There's no match to
  describe.

`apply_assignment` no longer builds guide content at all. It only handles
`ChannelStream`/`EPGData` linking for the actual, current-tick live match.
See `allocator.py`'s and `channel_sync.py`'s own docstrings, plus
`tests/test_allocator.py`/`tests/test_channel_sync.py`, for the exact policy
and edge cases.

### How the guide is actually written, a local XMLTV file, not raw ProgramData

An earlier version wrote `ProgramData` rows directly via the Django ORM.
That works, but fighting Dispatcharr's own EPG machinery around it caused
two real problems, both confirmed against Dispatcharr's actual source
(2026-07-28):

- `source_type="dummy"` (which sounds like "manually-managed, left alone")
  actually makes `EPGGridAPIView`, the real guide-grid endpoint,
  unconditionally overlay **any** channel on that source with its own
  auto-generated humorous filler, regardless of real `ProgramData` already
  existing for it. That's where "Lunchtime Laziness"/"Evening Escapism" come
  from, and it silently wins over anything we write.
- Switching to `source_type="xmltv"` avoids that, but then Dispatcharr's own
  EPG pipeline reacts to a channel's `epg_data` link changing by trying to
  fetch/parse a URL, which fails since we never set one, leaving the source
  stuck showing "Error" in the UI forever (harmless to the data, but
  fighting a status field that isn't ours to manage).

### Why generic channels use their own Stream, not Twitcharr's

An earlier version attached Twitcharr's own Stream row directly to our
generic channels via `ChannelStream`. That caused our "Valorant 1"/
"Valorant 2" channels to get created successfully by "Create Channels" and
then deleted again within minutes, with no error and no manual action,
confirmed against Twitcharr's actual `streamlink_setup.py` source
(2026-07-28) as `sync_channels()`'s own cleanup step:

```python
stale_channels = (
    Channel.objects.filter(streams__custom_properties__owner=OWNER_TAG)
    .exclude(tvg_id__in=keep_tvg_ids)
    .distinct()
)
stale_channels.delete()
```

Twitcharr deletes any Channel linked to a Stream *it* owns, unless that
Channel's `tvg_id` is one of its own tracked ones. Our channel is linked to
one of its Streams (because we reused the row) but our `tvg_id`
(`esportsarr.valorant.1`) obviously isn't in Twitcharr's own list, so its
next periodic sync deletes our channel as "stale." LoL channels never hit
this during testing purely because no LoL match had gone live yet, so
they'd never actually been linked to any stream.

A first fix cloned the working playback config (`url`, `stream_profile`,
`m3u_account`, `logo_url`) from whatever Stream Twitcharr already had for
that Twitch channel into a separate row this plugin owns, safe from the
prune query, but it still required Twitcharr to already be *tracking* that
exact channel just so a Stream existed to copy from. Adding a new league
meant configuring Twitcharr for a channel it otherwise had no reason to
manage.

The actual constraint turned out to be narrower: confirmed against
Dispatcharr's own `StreamProfile` model and Twitcharr's source (2026-07-28),
Dispatcharr has no built-in way to play a raw twitch.tv URL at all. Twitch
playback only works because Twitcharr installs its own streamlink-based
`StreamProfile` and attaches it to every Stream it creates. That profile is
one system-wide object, unrelated to which channels Twitcharr tracks. So
`_get_or_create_owned_stream` now builds the Stream itself
(`PLATFORM_URL_BUILDERS[platform](stream_channel)`, the channel/handle
already known from the scraper's `epg_channel_id` mapping) and only borrows
the shared profile by name (`twitch_stream_profile_name`/
`youtube_stream_profile_name` settings), tagged
`custom_properties={"owner": "esportsarr"}`, stable
`tvg_id=esportsarr.stream.<platform>.<channel>` so repeat ticks update the
same row. Twitcharr now only needs to have run once, ever, never to track
any specific league's channel, and its ownership-based prune query still
can never match one of our channels.

LPL is YouTube-only (no Twitch stream exists for it), so this same pattern
was extended to a second platform (`stream_platform`/`stream_channel` on
every match, derived from `epg_channel_id`'s prefix in
`scraper/esportsarr/riot_api.py`'s `_stream_identity_for_league`). Checked
`youtubearr` (github.com/jeff-gooch/youtubearr) as a possible reference for
the YouTube side, 2026-07-29: it doesn't install a reusable StreamProfile at
all. It resolves a temporary direct media URL via `yt-dlp` itself and plays
that through Dispatcharr's stock "proxy" profile, refreshing the resolved
URL periodically since it expires. That's a fundamentally different
mechanism from Twitcharr's (a stable page URL resolved at play time by a
streamlink-based profile), so nothing there could be borrowed directly.
`youtube_stream_profile_name` defaults to the same profile as Twitch as an
untested guess instead, see the "First run" section above.

The actual fix: guide content is never written as `ProgramData` rows
directly. `build_guide_entries` (see "What the guide shows" above) builds
this game's guide entries, and `_run_sync` (`plugin.py`) collects them
across every game and calls `write_guide()` **once** per tick.
`write_guide` renders one combined XMLTV file to `GUIDE_FILE_PATH` and sets
`EPGSource.file_path` to it (no `url`). Dispatcharr's own local-file EPG
support (`file_path` set, `url` empty) parses a file directly with **no
network fetch attempted at all**, so there's no error status to fight. It
then calls `refresh_epg_data(epg_source.id, force=True)`, a real,
callable Dispatcharr task, not a private API, which parses the file into
`EPGData`/`ProgramData` atomically: a bad file never destroys existing
guide data, it just fails and leaves the previous guide in place.
