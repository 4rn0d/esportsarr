"""CLI entrypoint: fetch matches for every tracked league, write
output/esports.xmltv (per-league EPG) and output/schedule.json (source of
truth for the Dispatcharr plugin). Run via `python -m esports_schedule.main`.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .channel_map import TRACKED_LEAGUES
from .riot_api import fetch_matches_for_leagues
from .schedule_export import build_schedule_json
from .xmltv import build_xmltv

DEFAULT_OUTPUT_DIR = Path("output")
XMLTV_FILENAME = "esports.xmltv"
SCHEDULE_FILENAME = "schedule.json"


def run(output_dir: Path) -> None:
    matches = fetch_matches_for_leagues(list(TRACKED_LEAGUES))

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / XMLTV_FILENAME).write_text(build_xmltv(matches), encoding="utf-8")
    (output_dir / SCHEDULE_FILENAME).write_text(build_schedule_json(matches), encoding="utf-8")

    print(f"Wrote {len(matches)} matches to {output_dir / XMLTV_FILENAME} and {output_dir / SCHEDULE_FILENAME}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory to write {XMLTV_FILENAME} and {SCHEDULE_FILENAME} into (default: {DEFAULT_OUTPUT_DIR})",
    )
    args = parser.parse_args()
    run(args.output_dir)


if __name__ == "__main__":
    main()
