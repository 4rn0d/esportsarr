# esportsarr (Dispatcharr plugin)

Consolidates per-league esports Twitch channels into a fixed number of
generic channels per game, switching the active stream to whichever live
match currently holds priority. See the top-level README for how this fits
with the scraper.

## Local testing (before touching the server)

Only `allocator.py` has zero Django/Dispatcharr dependency and can run
outside the real environment:

```bash
cd plugin
pip install -e ".[dev]"
pytest
```

`channel_sync.py` and the scheduler thread in `plugin.py` can't be
meaningfully unit-tested without a real Dispatcharr instance — see
"Deploying" below for how to verify those instead.

## Deploying to the Debian server

This project doesn't run any commands on your server directly — these are
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

## First run — do this before enabling the automatic scheduler

1. **Verify the Stream lookup assumption.** `channel_sync._find_stream_for_twitch_channel`
   assumes Twitcharr's `Stream.url` contains `twitch.tv/<channel>`. Check one
   real row (Django admin, or `docker exec` into the container and use
   `python manage.py shell`):
   ```python
   from apps.channels.models import Stream
   Stream.objects.filter(name__icontains="lcs").values("name", "url", "tvg_id")
   ```
   If none of `url`/`tvg_id`/`name` actually contain the plain Twitch channel
   login (e.g. `lcs`, `valorant_americas`), update the three filters in
   `_find_stream_for_twitch_channel` (`plugin/esportsarr/channel_sync.py`)
   to match whatever Twitcharr actually stores before relying on this.

2. **Run "Create Channels"** (the plugin action in the Dispatcharr UI). This
   creates the channel group + generic channels + EPG source. Idempotent —
   safe to run again later if you change `slots_per_game`.

3. **Run "Sync Now" manually** during a window when a tracked league has a
   known live match, then check in the Dispatcharr UI (Channels screen, same
   view as the screenshot from the original brainstorm) that:
   - The live match's stream is now `order=0` (top) on the right generic
     channel (e.g. "Valorant 1").
   - The channel's EPG guide entry shows the match title/time, not just
     Online/Offline.

4. Only once that looks right, leave `poll_interval_seconds` at its default
   (60s) — the background scheduler thread starts automatically whenever the
   plugin loads in the Celery worker process (not the web-server process,
   same split Twitcharr uses) and will keep re-running "Sync Now" on its own
   from then on.

## Publishing to the official Dispatcharr Plugins repo

Listed as an "external" plugin per `github.com/Dispatcharr/Plugins`'s own
recommendation: our code stays in this repo, only a thin `plugin.json` gets
submitted to their repo. `plugin.json` here already has `license`, `author`
(`4rn0d`), `source_type: "external"`, `source_url`, and `repo_url` filled in.

Zipping, tagging, and releasing are now automated by
`.github/workflows/release-please.yml` (see that section below) — once this
repo is pushed, everything except the actual GitHub-account actions is
hands-off:

1. Push this repo to `github.com/4rn0d/esportsarr`, writing
   commits as [Conventional Commits](https://www.conventionalcommits.org/)
   (`fix: ...`, `feat: ...`, `feat!: ...` or a `BREAKING CHANGE:` footer for a
   major bump) from here on — release-please reads these to decide the next
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

Uses the default `GITHUB_TOKEN` (no extra secret needed) — the one tradeoff
is that commits made by release-please itself won't trigger other workflows
in this repo (a GitHub Actions limitation to prevent infinite loops), which
doesn't matter here since `scraper/`'s cron workflow is independent of this
one.

## Adjusting overflow/priority behavior

`league_priority_lol` / `league_priority_valorant` (plugin settings, no code
change needed) control which league keeps a slot when more matches are live
simultaneously than there are slots. Earlier in the list = higher priority —
by default the international/global events (Worlds/MSI/First Stand,
Champions/VALORANT Masters/Game Changers Championship) are listed first, so
they always outrank regional leagues. The string must match Riot's league
`name` exactly (`python -m esportsarr.list_leagues --game <game>` in the
scraper repo) — e.g. `"Game Changers NA"`, not `"Game Changers Americas"`.

Assignment is sticky: a live match keeps its slot until it ends, even if a
higher-priority match starts in the meantime — **this is never overridden,
an already-live match is never bumped out of its slot.** What *does* happen
is reservation of *empty* slots, split into two windows because esports
broadcasts typically go live on Twitch ~1h before the official match time
(pre-show), not right at it:

- `reservation_lookahead_minutes` (default 60): how far ahead an upcoming
  match can preview/reserve a slot that's genuinely idle — nothing live
  wants it either way, so there's no cost to previewing it from the full
  pre-show window.
- `reservation_priority_minutes` (default 30, must be <= the lookahead
  above): how close to start an upcoming match has to be to actually take a
  slot *away* from a lower-priority match that's already live there. Beyond
  this window but still inside the wider lookahead, it can only preview an
  uncontested slot — it never costs a live regional match its slot just
  because an international happens to be scheduled sooner.

Either way, a reserved/previewed slot keeps showing whatever stream was
already on that channel — it does not go blank while waiting.

See `allocator.py`'s docstring and `tests/test_allocator.py` for the exact
policy and edge cases, including the near-vs-far distinction.

## What the guide shows for each slot

`apply_assignment` (`channel_sync.py`) computes a guide entry for every slot
on every tick, not just occupied ones — otherwise Dispatcharr's own generic
placeholder filler ("Lunchtime Laziness...", "Evening Escapism...") shows
through instead:

- **Live match**: the real match, same as the stream switch itself.
- **Reserved slot** (an anticipated higher-priority match within
  `reservation_lookahead_minutes`, holding the slot per the section above):
  a "coming up" entry for that match, at its real start time. The stream
  itself is untouched — only the guide previews it.
- **Genuinely idle** (nothing live, nothing anticipated): an explicit
  "No Match Scheduled" placeholder, refreshed every tick, instead of stale
  or generic filler content.

### How the guide is actually written — a local XMLTV file, not raw ProgramData

An earlier version wrote `ProgramData` rows directly via the Django ORM.
That works, but fighting Dispatcharr's own EPG machinery around it caused
two real problems, both confirmed against Dispatcharr's actual source
(2026-07-28):

- `source_type="dummy"` (which sounds like "manually-managed, left alone")
  actually makes `EPGGridAPIView`, the real guide-grid endpoint,
  unconditionally overlay **any** channel on that source with its own
  auto-generated humorous filler — regardless of real `ProgramData` already
  existing for it. That's where "Lunchtime Laziness"/"Evening Escapism" come
  from, and it silently wins over anything we write.
- Switching to `source_type="xmltv"` avoids that, but then Dispatcharr's own
  EPG pipeline reacts to a channel's `epg_data` link changing by trying to
  fetch/parse a URL — which fails since we never set one, leaving the source
  stuck showing "Error" in the UI forever (harmless to the data, but
  fighting a status field that isn't ours to manage).

The actual fix: `apply_assignment` no longer writes `ProgramData` at all —
it returns this game's guide entries, and `_run_sync` (`plugin.py`) collects
them across every game and calls `write_guide()` **once** per tick.
`write_guide` renders one combined XMLTV file to `GUIDE_FILE_PATH` and sets
`EPGSource.file_path` to it (no `url`) — Dispatcharr's own local-file EPG
support (`file_path` set, `url` empty) parses a file directly with **no
network fetch attempted at all**, so there's no error status to fight. It
then calls `refresh_epg_data(epg_source.id, force=True)` — a real,
callable Dispatcharr task, not a private API — which parses the file into
`EPGData`/`ProgramData` atomically: a bad file never destroys existing
guide data, it just fails and leaves the previous guide in place.
