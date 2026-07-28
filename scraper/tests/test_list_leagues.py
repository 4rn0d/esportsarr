from __future__ import annotations

import responses

from esportsarr.list_leagues import run
from esportsarr.models import Game
from esportsarr.riot_api import LOL_HOST


@responses.activate
def test_run_prints_every_league_name_and_id_sorted_alphabetically(capsys):
    responses.add(
        responses.GET,
        f"{LOL_HOST.base_url}/getLeagues",
        json={
            "data": {
                "leagues": [
                    {"id": "222", "name": "LEC", "slug": "lec"},
                    {"id": "111", "name": "LCS", "slug": "lcs"},
                ]
            }
        },
        status=200,
    )

    run(Game.LOL)

    output = capsys.readouterr().out
    # Sorted alphabetically, not by API response order (LEC came first above).
    assert output.index("'LCS'") < output.index("'LEC'")
    assert "id=111" in output
    assert "id=222" in output
