"""Pass-zone grid: depth bins, per-cell stats, defense ranks, season fallback."""

from __future__ import annotations

import pandas as pd
import pytest

import pass_zones as pz


def _att(defteam, passer, loc, air, cmp, yds, epa, td=0, intc=0, week=1, posteam="BUF"):
    return {"game_id": f"g{week}", "week": week, "posteam": posteam, "defteam": defteam,
            "passer_player_id": passer, "receiver_player_id": "WR1", "loc": loc, "air_yards": air,
            "complete_pass": cmp, "yards_gained": yds, "epa": epa, "pass_touchdown": td, "interception": intc}


@pytest.fixture
def fake_attempts(monkeypatch):
    rows = [
        _att("MIA", "QB1", "left", 25, 1, 30, 2.0, td=1),
        _att("MIA", "QB1", "left", 22, 0, 0, -0.5),
        _att("MIA", "QB1", "middle", 0, 1, 3, -0.2),
        _att("NYJ", "QB1", "right", 5, 1, 6, 0.3, week=2),
        _att("NYJ", "QB2", "left", 30, 0, 0, -1.0, intc=1, week=2, posteam="MIA"),
    ]
    a = pd.DataFrame(rows)
    a["depth"] = pz._depth(a["air_yards"])
    monkeypatch.setattr(pz, "attempts", lambda season=None: a)
    lg = pz._cell_stats(a, ["depth", "loc"])
    lg["share"] = lg["att"] / len(a)
    monkeypatch.setattr(pz, "league_cells", lambda season=None: lg)
    dc = pz._cell_stats(a, ["defteam", "depth", "loc"]).reset_index()
    dc["epa_rank"] = dc.groupby(["depth", "loc"])["epa"].rank(method="min").astype(int)
    monkeypatch.setattr(pz, "defense_cells", lambda season=None: dc.set_index(["defteam", "depth", "loc"]))
    return a


def test_depth_bins():
    s = pd.Series([-3, 0, 1, 9, 10, 19, 20, 55])
    assert list(pz._depth(s)) == ["behind", "behind", "short", "short", "intermediate", "intermediate", "deep", "deep"]


def test_player_grid(fake_attempts):
    g = pz.player_zones("QB1", "pass", season=2026, fallback=False)
    assert g["total_att"] == 4 and len(g["cells"]) == 12
    cell = {(c["depth"], c["loc"]): c for c in g["cells"]}
    dl = cell[("deep", "left")]
    assert dl["att"] == 2 and dl["cmp"] == 0.5 and dl["ypa"] == 15.0 and dl["epa"] == 0.75 and dl["td"] == 1
    assert dl["share"] == 0.5
    # league deep-left includes QB2's interception
    assert dl["lg"]["epa"] == pytest.approx((2.0 - 0.5 - 1.0) / 3, abs=1e-3)
    empty = cell[("intermediate", "middle")]
    assert empty["att"] == 0 and empty["epa"] is None


def test_defense_grid_ranks(fake_attempts):
    g = pz.defense_zones("NYJ", season=2026, fallback=False)
    assert g["total_att"] == 2 and g["out_of"] == 2
    dl = next(c for c in g["cells"] if (c["depth"], c["loc"]) == ("deep", "left"))
    # NYJ allowed -1.0 on its one deep-left attempt; MIA allowed +0.75 -> NYJ stingiest
    assert dl["rank"] == 1


def test_falls_back_to_prior_season_when_thin(fake_attempts):
    g = pz.player_zones("QB1", "pass", season=2026)  # 4 attempts < 20 -> prior season
    # fake data is identical per season, so the prior isn't bigger: keep current
    assert g["season"] == 2026 and "fallback_from" not in g


def test_starting_qb_is_latest_games_passer(fake_attempts):
    assert pz.team_starting_qb("BUF", 2026) == "QB1"
    assert pz.team_starting_qb("MIA", 2026) == "QB2"
