# esportsarr (scraper)

Fetches LoL + Valorant match schedules from Riot's esports API and writes
`output/esports.xmltv` + `output/schedule.json`.

## Setup

```bash
cd scraper
pip install -e ".[dev]"
```

## Run

```bash
python -m esportsarr.main --output-dir output
```

## Test

```bash
pytest
```

`tests/test_riot_api.py` mocks the Riot API with the `responses` library — no
network calls or API key needed to run the test suite.

## Adding a league

First, find the exact league name Riot uses — don't guess it:

```bash
python -m esportsarr.list_leagues --game valorant
python -m esportsarr.list_leagues --game lol
```

This prints every league's real `name` and `id` from Riot's own `getLeagues`
response, e.g. `'Game Changers NA' (id=106976737954740691)`. Then edit
`esportsarr/channel_map.py`'s `TRACKED_LEAGUES` tuple, using that exact
`display_name` string (exact match, not substring — e.g. "LCK" vs
"LCK Challengers" are different leagues; "GC NA" is not the same string as
"Game Changers NA" and will silently never match). The `epg_channel_id` must
match the existing Twitcharr-managed Dispatcharr channel's EPG id for that
league — reuse an existing one if the new league broadcasts on the same
Twitch channel (e.g. regional Game Changers matches airing on the main
regional VCT channel).

A league name that doesn't match anything is logged and skipped, not fatal —
it won't take down the other leagues sharing its game host. Check the
scraper's GitHub Actions run logs if a league you just added isn't showing up
in the guide.

## GitHub repo references

`esportsarr/riot_api.py`'s `SCRAPER_USER_AGENT` and
`plugin/esportsarr/plugin.json` already point at
`github.com/4rn0d/esportsarr` — update both if you ever fork
this under a different account/repo name.
