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

Edit `esportsarr/channel_map.py`'s `TRACKED_LEAGUES` tuple. The
`display_name` must exactly match the `name` field Riot's `getLeagues`
endpoint returns (exact match, not substring — e.g. "LCK" vs
"LCK Challengers" are different leagues). The `epg_channel_id` must match the
existing Twitcharr-managed Dispatcharr channel's EPG id for that league.

## GitHub repo references

`esportsarr/riot_api.py`'s `SCRAPER_USER_AGENT` and
`plugin/esportsarr/plugin.json` already point at
`github.com/4rn0d/esportsarr` — update both if you ever fork
this under a different account/repo name.
