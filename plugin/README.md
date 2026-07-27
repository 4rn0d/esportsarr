# esports_scheduler (Dispatcharr plugin)

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
   docker cp plugin/esports_scheduler <dispatcharr-container>:/app/data/plugins/esports_scheduler
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
   `_find_stream_for_twitch_channel` (`plugin/esports_scheduler/channel_sync.py`)
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

1. Push this repo to `github.com/4rn0d/dispatcharr-esports-epg`, writing
   commits as [Conventional Commits](https://www.conventionalcommits.org/)
   (`fix: ...`, `feat: ...`, `feat!: ...` or a `BREAKING CHANGE:` footer for a
   major bump) from here on — release-please reads these to decide the next
   version.
2. release-please opens a "release PR" on its own after any `fix:`/`feat:`
   commit lands on `main`, showing the computed version bump. Merge it when
   ready; that merge is what actually cuts the tag, GitHub Release, and
   uploads `esports-scheduler.zip` (see workflow below).
3. Fork `github.com/Dispatcharr/Plugins`, add
   `plugins/esports-scheduler/plugin.json` (copy of this project's
   `plugin/esports_scheduler/plugin.json`) plus an optional README/logo, and
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
- Patch `plugin/esports_scheduler/plugin.json`'s `"version"` field directly
  (via release-please's `extra-files` JSON updater) as part of the release PR.
- On merge, tag as `esports-scheduler-v{version}` (matching `plugin.json`'s
  `source_url` placeholder exactly), create the GitHub Release, zip
  `plugin/esports_scheduler/`, and attach it as `esports-scheduler.zip`.

Uses the default `GITHUB_TOKEN` (no extra secret needed) — the one tradeoff
is that commits made by release-please itself won't trigger other workflows
in this repo (a GitHub Actions limitation to prevent infinite loops), which
doesn't matter here since `scraper/`'s cron workflow is independent of this
one.

## Adjusting overflow/priority behavior

`league_priority_lol` / `league_priority_valorant` (plugin settings, no code
change needed) control which league keeps a slot when more matches are live
simultaneously than there are slots. Earlier in the list = higher priority.
Assignment is sticky: a live match keeps its slot until it ends, even if a
higher-priority match starts in the meantime — see `allocator.py`'s
docstring and `tests/test_allocator.py` for the exact policy and edge cases.
