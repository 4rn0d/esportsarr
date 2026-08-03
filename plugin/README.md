# esportsarr (Dispatcharr plugin)

Consolidates per-league esports channels (Twitch, plus YouTube for
YouTube-only leagues like LPL) into a fixed number of generic channels per
game, switching the active stream to whichever live match currently holds
priority. See the top-level README for how this fits with the scraper.

## How the sync works: one daily plan, applied every tick

The live 60s tick does **not** decide which match plays where. Once a day
(`plan_refresh_interval_hours`, default 24), `plan_builder.build_weekly_plan`
runs the actual allocation policy (`allocator.assign_slots`/
`project_schedule`, plus supplemental-content gap-filling) once, over the
whole `schedule_projection_days` window, and the result is persisted to
`.state/weekly-plan.json`. Every 60s tick in between just looks up what that
stored plan says is current right now (`plugin._current_occupant`) and
reconciles it against live reality -- a match cancelled/ended earlier than
planned, Twitch stream-title verification for Game Changers
(`plugin._reconcile_with_reality`) -- it never re-runs the allocator itself.

This replaced an earlier design where the live tick called
`assign_slots` for "right now" and separately called `project_schedule` to
preview the week ahead for the guide -- two independent live simulations of
the same policy that could disagree (the root cause of several bugs: a
guide showing a stale league while the live stream was actually correct,
supplemental-content picks only ever decided the instant a tick happened to
reach an empty slot rather than something inspectable in advance). Now
there's one plan, computed once, and everything else reads from it.

`sync_now` always rebuilds the plan immediately (ignoring the refresh
interval) before applying it, so it's actually useful for verifying
behavior against a known live match right now.

## Local testing (before touching the server)

`allocator.py`, `plan_builder.py`, `stream_verification.py`, and
`supplemental_content.py` have zero Django/Dispatcharr dependency and can
run outside the real environment:

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

5. **Optional: enable supplemental content.** `enable_supplemental_content`
   is off by default because it needs `yt-dlp` installed in the Dispatcharr
   Python environment (`pip install yt-dlp` there, or confirm it's already
   present -- it's a real dependency of this plugin per `pyproject.toml`,
   not bundled). Once installed, turn the setting on and configure
   `replay_channels_lol`/`replay_channels_valorant` if the defaults don't
   suit you. See "Filling idle time with supplemental content" below for
   what this actually does.

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

Continuity survives a real gap between two matches on the same channel, not
just an instant handoff. `assign_slots` returns `channel_by_slot`
(`allocator.py`), remembering each slot's last real channel even while the
slot sits idle for a while (a broadcast going dark for a break between
matches); the caller passes it back in as `last_channel_by_slot` on the next
call. Without this, the memory of "this slot was showing channel X" was lost
the instant the slot went empty, so the next match on that same channel
landed wherever priority/order happened to put it instead of reclaiming its
usual slot -- confirmed as a real bug, 2026-07-30 (Arnaud: two leagues
airing back-to-back, not simultaneously, on the same Twitch channel).

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

Matches are told apart by `match_id` (Riot's own `event.match.id`, added to
`schedule.json` for exactly this) first, falling back to `(league, start)`
only when it's missing. Game Changers events regularly schedule multiple
concurrent matches with the identical start time -- confirmed against real
Riot data, 2026-08-03: two separate Game Changers EMEA matches both starting
at `2026-05-13T15:00:00Z`. Without `match_id`, `(league, start)` alone
couldn't tell them apart, so the allocator treated the second one as already
accounted for by the first and never gave it a slot, even with one sitting
empty -- it simply vanished from the guide with no trace, not even shown as
dropped for lack of capacity.

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
airing, so they were only ever reserved, never displayed). `plan_builder.py`'s
`_classify_matches` also treats an `"unstarted"` match as live once its
scheduled start has passed, up to `STALE_LIVE_GRACE_MINUTES` (12h) -- past
that, it's presumed stale data rather than a genuinely long-running match
and falls back to the ordinary near/far reservation buckets.

## Verifying the live stream actually matches the schedule

Riot's schedule declares one fixed Twitch channel per league, but some
broadcasts split concurrent games onto a secondary channel that the
schedule has no way to reflect -- Game Changers especially, where multiple
games commonly air at once (Arnaud, 2026-07-30). Without a check, the
plugin would confidently point a slot at the schedule's declared channel
even when that channel is actually showing a *different* concurrent match.

`stream_verification.py` cross-checks this for any currently-live match
whose league is in `LIVE_CHANNEL_CANDIDATES` (Game Changers NA and EMEA
today) against every known candidate channel for that league, in order
(the schedule's own declared channel is listed first, so the common case
of nothing having split still costs exactly one lookup). It fetches each
candidate's live title via Twitch's public, unauthenticated GQL endpoint
(`gql.twitch.tv/gql`) with the same shared client-id Streamlink and the
Twitcharr plugin use (`kimne78kx3ncx6brgo4mv6wki5h1ko`) -- no Twitch
Developer app or credentials needed -- and checks whether both of the
match's own participant team names (parsed from its `description`, e.g.
"SK Nebula vs G2 Gozen") appear in that title. The first candidate whose
title actually matches wins; if none do (or every candidate is offline/the
request fails), the match is marked unstreamable for that tick
(`stream_platform`/`stream_channel` cleared to `None`) rather than risk
showing the wrong game -- it simply won't compete for a slot until a later
tick can verify it, same treatment as a contentless placeholder event.

This runs twice, for two different reasons: once inside
`plan_builder.build_weekly_plan` (so the plan itself already reflects
reality at build time), and again every live tick inside
`plugin._reconcile_with_reality` (so a channel split that starts *after*
the plan was last built still gets caught within a tick, not a full day
later). Both use `plan_builder.is_genuinely_live`, the same shared helper
`_classify_matches` uses, not the raw `state` field directly -- there's
nothing real to check against before a match actually starts airing.
Checking `state == "in_progress"` directly here was a real bug (confirmed
2026-07-30): Riot's `state` flag is exactly as unreliable for Game Changers
matches as the whole reason this verification exists in the first place, so
a match stuck on `"unstarted"` past its real start never got verified at
all, silently keeping the schedule's default declared channel -- identical
to whatever *other* concurrent match on the same league happened to verify
correctly, since Game Changers NA/EMEA only have one static
`epg_channel_id` regardless of which specific match it is. Checking every
tick regardless (rather than only when genuinely ambiguous) keeps the logic
simple at the cost of a modest handful of extra HTTP requests per poll.
Adding a new league to `LIVE_CHANNEL_CANDIDATES` needs no other code
changes; adding a YouTube-based one would need a `yt-dlp`-based equivalent
of `fetch_twitch_stream_title` (see youtubearr's plugin for the pattern --
"Zero API Quota: uses yt-dlp instead of the YouTube Data API").

## Filling idle time with supplemental content

Off by default (`enable_supplemental_content`). When on,
`plan_builder._fill_game_projection_gaps` runs once, at plan-build time,
against every idle stretch across the *whole* week-ahead plan -- not just
"whatever's empty this tick" -- so Plat Chat/replay picks are decided up
front and inspectable in the stored plan, the same way real matches are. It
only ever touches a stretch `assign_slots`/`project_schedule` already left
empty -- it never competes with or bumps a real esports match, same
guarantee as everything else in the priority system (Arnaud, 2026-07-30).

- **Plat Chat VALORANT**: a live weekly VALORANT talk show
  (`youtube.com/@PlatChatVALORANT`), checked first for any empty Valorant
  slot. `fetch_plat_chat_schedule` lists the channel's streams via yt-dlp
  (metadata only, no JS runtime needed -- we never resolve a playable
  format ourselves, same as the real leagues) and looks for the next
  upcoming/live episode, returning its schedule (video id, topic, episode
  number, real start time) independent of "now" so it's cacheable. The
  separate, pure `plat_chat_match_if_live(schedule, now)` decides whether
  it's actually airing at `now` -- derived purely from `real_start` +
  `PLAT_CHAT_DURATION_SECONDS`, deliberately not from YouTube's own
  `live_status` flag, which can be stale once `schedule` came from a cache
  read hours earlier (same principle as trusting Riot's schedule timing
  over its own unreliable state flag elsewhere in this plugin). Episode
  number is parsed straight from the video's own title -- their format is
  reliably `"{topic} — Plat Chat VALORANT Ep. {N}"` (confirmed against real
  titles, e.g. "The BEST teams in VCT right now are..? — Plat Chat VALORANT
  Ep. 274"). Duration is a fixed 3.5h estimate (the middle of Arnaud's
  stated "three to four hours" range) since it's live and the real end is
  unknown, same caveat as esports match duration estimates. Only ever fills
  one slot, even if several are empty.
- **Replays**: any Valorant/LoL slot still empty after Plat Chat gets an
  official match replay instead. `fetch_replay_candidates` lists recent
  uploads (with a real, exact duration from yt-dlp, not an estimate) from
  the channels configured in `replay_channels_lol`/`replay_channels_valorant`
  (comma-separated YouTube URLs -- defaults to Riot's own
  `@lolesportsvods` for LoL; VCT has no single central VOD channel like
  LoL does, so Valorant defaults to rotating across the three official
  regional channels). `pick_replay` chooses deterministically from a seed
  including the date, game, and slot index, so the same replay holds for a
  whole idle stretch instead of changing every 60s poll tick, but still
  varies day to day and across slots. A replay VOD's own title is
  free-text and inconsistent across leagues (confirmed against real
  titles: "FLY v C9 - PLAYOFFS 2025 LTA North Split 2 - W11D2 - Game 05"
  vs "G2 v MKOI | 2025 LEC Spring Playoffs | Grand..." -- different
  separators, different wording, same channel), so rather than parse that
  structure, `_extract_replay_league` searches the title for a known
  league name (`KNOWN_REPLAY_LEAGUES`, not exhaustive -- extend as new
  replay sources surface leagues not covered) and uses it as the short
  guide title, moving the full original title into the description; with
  no recognized league it falls back to the full title with no separate
  description (Arnaud, 2026-07-30: "the title of lol1 should only be LTA
  North and the rest be the description"). "Replay" itself (`league_name`)
  is an internal identifier only, never shown -- "this is a rerun" is the
  `<previously-shown/>` tag, not a category string or title text.

Both are just another match-shaped dict fed through the exact same
pipeline real matches use (`stream_platform`/`stream_channel`,
`duration_for_match`, `project_schedule`) -- a replay's `stream_platform`
is `"youtube_vod"` (a plain `youtube.com/watch?v=...` URL, distinct from
`"youtube"`'s `/@handle/live` for genuinely live channels), and
`duration_for_match` prefers an explicit `duration_seconds` field on the
match dict over the best-of tables when present, which is how supplemental
content gets its exact/estimated duration without needing a fake
`best_of` value.

One honest limitation: the virtual match's `start` is always regenerated
as "now" on the tick it's created, not the real moment the idle stretch
actually began, since that isn't tracked separately. In practice this
mostly means the guide's displayed start time for supplemental content is
approximate, not the precise instant a real match ended and the slot went
idle.

**Fetches are cached, not repeated every plan rebuild.** A replay candidate
list and Plat Chat's schedule barely change within a day, and now that
picks are only made once a day anyway (see "How the sync works" above),
`get_cached_replay_candidates`/`get_cached_plat_chat_schedule` reuse a
fetch from a local JSON file (`supplemental_content.CACHE_FILE_PATH`) until
it's older than `CACHE_TTL` (24h), so yt-dlp doesn't necessarily run again
on every single plan rebuild either (Arnaud, 2026-07-30: "preselect it...
instead of doing constant fetches"). Every *pick* for the whole week is
made once, at plan-build time (deterministically, via `pick_replay`'s
seed) -- unlike the old per-tick design, whether Plat Chat is airing at a
given moment is decided once per plan too (`plat_chat_match_if_live`
against the cached schedule's `real_start`), not re-checked live every
tick, since the live tick only applies whatever the plan already decided.

Whether something is live or a rerun is the standard XMLTV `<live/>` /
`<previously-shown/>` empty tag, not a category string (Arnaud, 2026-07-30,
pointing at a real third-party guide's XML: "it has a episode-num tag and a
previously-shown tag... also a live tag. That's what I'm looking for.").
Every real match and Plat Chat entry gets `<live/>`; every replay gets
`<previously-shown/>` instead; filler ("No Match Scheduled") gets neither.
`<category>` supports multiple elements per programme (also confirmed
against that same real guide's XML, which had four): a real match gets
both `"Esports"` and `"Sports"` (`channel_sync.DEFAULT_CATEGORIES` --
"make sure to add the sports category to the live matches"); Plat Chat and
replays get just `"Esports"` (`supplemental_content.SUPPLEMENTAL_CATEGORIES`)
since neither is itself a live sports match. Plat Chat also gets an
`<episode-num system="onscreen">Episode 274</episode-num>` from its parsed
episode number. Guide entries also carry an `icon` (the video's own
YouTube thumbnail, `i.ytimg.com/vi/<id>/maxresdefault.jpg`, no extra fetch
needed since that URL pattern is universal).

**A "nothing found" result is cached much more briefly than a real find.**
`get_cached_plat_chat_schedule`/`get_cached_replay_candidates` trust a
genuine result (a real schedule, a non-empty candidate list) for the full
`CACHE_TTL` (24h), but a `None`/empty result only for `NEGATIVE_CACHE_TTL`
(1h). Without this split, a check that happens to run before an episode is
announced or goes live would cache "nothing" for the entire day, silently
hiding Plat Chat even after it does get scheduled an hour later (confirmed
as a real bug, 2026-07-30 -- Plat Chat never appeared because the first,
cold-cache check found nothing and that null result was then trusted all
day).

**Replay candidates below `MIN_REPLAY_DURATION_SECONDS` (30 minutes) are
excluded.** `fetch_replay_candidates` previously only skipped entries with
no duration at all (still-live/upcoming uploads), which let short clips and
highlight reels into the replay pool alongside full match VODs (confirmed
as a real bug, 2026-07-30 -- clips as short as ~12 minutes were shown as
full replay blocks in the guide). A full match VOD reliably runs well past
half an hour, so anything shorter is treated as a clip, not a candidate.

**Requires `yt-dlp` installed in the Dispatcharr Python environment** (a
real dependency in `pyproject.toml`, not bundled) -- see "First run" above.

## What the guide shows, a week-ahead projection, not a one-tick snapshot

The guide covers `GUIDE_LOOKBACK_HOURS` (12h, `channel_sync.py`) before "now"
through `schedule_projection_days` (default 7) into the future. The guide
*file* is rewritten every tick (cheap -- it's just formatting the already-
computed stored plan into XMLTV), but the underlying match data only
changes when the plan itself is rebuilt, once a day. Either way there are
zero gaps -- including before "now". Otherwise Dispatcharr's
own generic placeholder filler ("Lunchtime Laziness...", "Evening
Escapism...") would show through instead, and a slot idle for a while
before "now" would have zero programme data for that stretch, which
Dispatcharr's grid renders as a blank hole rather than "No Match Scheduled"
(confirmed as a real bug, 2026-07-29 -- a slot with a still-live match
happened to already cover the time before "now" and looked fine, while an
idle one right next to it was visibly blank). This used to be a purely
reactive, one-entry-per-slot guess at "what's happening right now plus
maybe one preview"; it's now a genuine forward simulation:

`_classify_matches` (`plan_builder.py`) also feeds recently-`completed` matches
into the projection, not just live/upcoming ones, as long as they started
within `GUIDE_LOOKBACK_HOURS` -- otherwise the guide has zero record of what
actually aired in a slot once its match ends, and the historical portion
degrades to one giant "No Match Scheduled" the moment nothing's currently
live there, even though real matches did air (same 2026-07-29 bug: only
slots with something *still* live looked right, everything else showed a
12h-wide filler instead of the real history).

- `allocator.project_schedule` replays the exact same `assign_slots` policy
  (priority ranking, sticky live matches, same-channel continuity) across
  the whole lookback-to-future window. It takes a required `now` and forces
  `initial_assignment`/`initial_channel_by_slot` (this tick's real, accurate
  assignment) in **exactly at `now`**, not just at the start of the whole
  replay. Before `GUIDE_LOOKBACK_HOURS` added recently-completed matches to
  the replay, "the start of the whole replay" and "now" were the same
  instant, so this distinction didn't matter -- once the replay's earliest
  point became hours in the past, it did: replaying that history purely
  from schedule timing (`start` + `duration_fn`, with no access to the live
  sync's real Riot `state`-flag corrections) can compute a different answer
  for "now" than what's actually live (confirmed as a real bug, 2026-07-30:
  the guide displayed a stale league at "now" while the actually-applied
  live stream was a different, correct one). Anything before `now` is an
  untouched historical reconstruction from schedule timing alone; anything
  at or after `now` is guaranteed consistent with the real accurate state.
  Two matches from different leagues that overlap in time and both want the
  same slot are resolved exactly like live sync: the higher-priority one
  shows, the lower-priority one is simply **absent** from the guide for
  that window, never queued or shown after the fact.
- `channel_sync.build_guide_entries` turns that per-slot match sequence
  into actual guide entries, filling every gap (before the first known
  match, between two consecutive ones, and after the last one through to
  the end of the projection window) with "No Match Scheduled" placeholders.
  A gap is chunked into consecutive `MAX_FILLER_BLOCK` (45min) entries
  rather than one placeholder spanning the whole gap -- a single block
  spanning hours or days reads as broken/frozen in most EPG grids, the same
  way real TV guides never show one program title stretching across an
  entire idle overnight slot. Only the very first chunk's start (right at
  "now") is rounded down to the nearest :00/:15/:30/:45
  (`_round_down_to_quarter_hour`). A guide reads oddly with a block starting
  at 3:53pm. Every other boundary is already an exact real timestamp (a
  match's own start, a 45min chunk boundary, or the projection window's
  edge), so nothing else needs rounding.
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
  self-corrects on the next plan rebuild (or immediately via "Sync Now").
  It never affects the actual live stream switch: `_current_occupant`
  (`plugin.py`) independently checks the same real end estimate every tick
  when deciding what the plan says is current, so a slot correctly goes
  idle once its match's real estimated end passes even between rebuilds.
  `channel_sync.duration_for_match` estimates
  by best-of format, per game -- `LOL_BEST_OF_DURATIONS` (Bo1 ~1h, Bo3 ~2h,
  Bo5 ~3h20) vs `VALORANT_BEST_OF_DURATIONS` (Bo1 ~1h, Bo3 ~3h, Bo5 ~5h30) --
  rather than one flat 3h for every match, since a LoL game (~30-40min) runs
  noticeably shorter than a Valorant map (~40-45min), so the same best-of
  count adds up to a meaningfully different real broadcast length depending
  on the game (Arnaud, 2026-07-30). Bo7 has an entry in Valorant's table too
  (~7h30) even though no league we track uses it yet (Rocket League,
  planned, a third game entirely -- it'll need its own table); that estimate
  is an unvalidated placeholder. Must be kept in sync manually with the
  identical tables in `scraper/esportsarr/xmltv.py` -- the two packages
  don't share code.
- Every real entry also carries a `description` (match participants first,
  e.g. "Sentinels vs Cloud9", then stage/matchday context, then the best-of
  format when Riot reports one -- e.g. "Sentinels vs Cloud9 · Playoffs · Bo3")
  straight through from the scraper's `schedule.json` into the XMLTV `<desc>`
  element. `title` is always just the league name (e.g. "LCS") so the
  programme name is stable and never blank; `description` is where the
  actual match info lives. Filler ("No Match Scheduled") entries have none.
  There's no match to describe.

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
