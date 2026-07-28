# esportsarr

Two independent pieces that together turn the Twitcharr-managed esports
Twitch channels (LCS, LEC, LCK, VCT Americas/EMEA/Pacific) into something
closer to real TV: a proper match guide instead of "Online/Offline", and a
smaller set of consolidated channels ("LoL 1"/"LoL 2", "Valorant 1"/
"Valorant 2") that automatically switch to whichever match is currently live.

## `scraper/` — data producer, runs on GitHub Actions

Fetches match schedules from Riot's official (if undocumented) esports API
for LoL and Valorant, and publishes two files every 15 minutes:

- `scraper/output/esports.xmltv` — a normal XMLTV guide for the *existing*
  per-league Twitcharr channels. Assign it as an additional EPG source on
  those channels in Dispatcharr's UI; it doesn't touch Twitcharr's own EPG.
- `scraper/output/schedule.json` — the full match list (including live/
  completed state), consumed by the plugin below.

See [`scraper/README.md`](scraper/README.md).

## `plugin/` — Dispatcharr plugin, runs on the Debian server

A real Dispatcharr plugin (`esportsarr`) that polls `schedule.json`
every ~60s and, per game, reassigns which Twitch stream is the active one on
a fixed number of generic channels — "sticky" assignment, so a live match
keeps its channel until it ends; a higher-priority match that starts later
waits for a slot to free up rather than preempting it.

See [`plugin/README.md`](plugin/README.md) for deployment steps.

## Why two pieces instead of one

The switch/failover logic needs near-real-time reaction to matches starting
and ending, which only a process running inside Dispatcharr (with direct
Django ORM access) can do reasonably. The schedule *data*, by contrast, only
needs to change every few minutes and has no reason to run inside
Dispatcharr's own process — a GitHub Actions cron job is simpler to operate
and doesn't add load or a new dependency on the Debian server.

## Known unknowns / things to verify against the real install

- **Stream lookup** (`plugin/esportsarr/channel_sync.py`,
  `_find_stream_for_twitch_channel`): assumes Twitcharr's `Stream.url`
  contains `twitch.tv/<channel>`. Verify against one real Stream row before
  relying on automatic sync — see `plugin/README.md`.
- **Riot's public API key** (`scraper/esportsarr/riot_api.py`): a
  community-known key embedded in lolesports.com/valorantesports.com's own
  web client, verified working on 2026-07-27. Not officially supported by
  Riot — if it stops working, check
  [vickz84259/lolesports-api-docs](https://github.com/vickz84259/lolesports-api-docs)
  for an updated one.
