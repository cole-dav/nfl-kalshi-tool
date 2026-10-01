"""
Pass-zone heat maps from nflverse play-by-play: the field split into
left / middle / right (pbp `pass_location`) x four air-yard depth bands, with
attempts, completion %, yards / attempt and EPA / attempt per cell.

Three views share one grid:
  - a passer's own throws (`passer_player_id`),
  - a receiver's targets (`receiver_player_id`),
  - what a defense has allowed (`defteam`), with a league rank per cell.

Every cell carries the league-wide average for that cell, so the UI can color
it as better/worse than league rather than by raw EPA (deep throws are always
high-variance; behind-the-line throws are always low ypa).

Sacks, spikes, two-point tries and passes with no charted location/depth are
excluded. All team codes are nflverse codes.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import nflverse_data as nd

LOCATIONS = ("left", "middle", "right")
# (key, label, lo, hi) on air_yards, inclusive.
DEPTHS = (
    ("deep", "DEEP 20+", 20, 99),
    ("intermediate", "INT 10-19", 10, 19),
    ("short", "SHORT 1-9", 1, 9),
    ("behind", "BEHIND LOS", -99, 0),
)


def _depth(air: pd.Series) -> pd.Series:
    out = pd.Series(pd.NA, index=air.index, dtype="object")
    for key, _, lo, hi in DEPTHS:
        out[air.between(lo, hi)] = key
    return out


@nd.memo_on_data("pbp")
def attempts(season: int | None = None) -> pd.DataFrame:
    """One row per charted pass attempt with its zone."""
    p = nd.load_pbp(season)
    a = p[(p["pass_attempt"] == 1) & (p["sack"] != 1) & (p["two_point_attempt"] != 1)
          & p["pass_location"].isin(LOCATIONS) & p["air_yards"].notna() & p["posteam"].notna()]
    a = a[["game_id", "week", "posteam", "defteam", "passer_player_id", "receiver_player_id",
           "pass_location", "air_yards", "complete_pass", "yards_gained", "epa",
           "pass_touchdown", "interception"]].copy()
    a["depth"] = _depth(a["air_yards"])
    return a.rename(columns={"pass_location": "loc"})


def _cell_stats(df: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    g = df.groupby(by)
    return pd.DataFrame({
        "att": g.size(),
        "cmp": g["complete_pass"].mean(),
        "ypa": g["yards_gained"].mean(),
        "epa": g["epa"].mean(),
        "td": g["pass_touchdown"].sum(),
        "int": g["interception"].sum(),
    })


@nd.memo_on_data("pbp")
def league_cells(season: int | None = None) -> pd.DataFrame:
    a = attempts(season)
    s = _cell_stats(a, ["depth", "loc"])
    # share of all league attempts that land in this cell
    s["share"] = s["att"] / len(a)
    return s


@nd.memo_on_data("pbp")
def defense_cells(season: int | None = None) -> pd.DataFrame:
    """Per (defteam, depth, loc) allowed stats with a league rank on EPA/att
    allowed (1 = stingiest)."""
    a = attempts(season)
    s = _cell_stats(a, ["defteam", "depth", "loc"]).reset_index()
    s["epa_rank"] = s.groupby(["depth", "loc"])["epa"].rank(method="min").astype(int)
    return s.set_index(["defteam", "depth", "loc"])


def _num(v, digits=3):
    return None if v is None or (isinstance(v, float) and np.isnan(v)) else round(float(v), digits)


def _grid(stats: pd.DataFrame, total: int, season: int, ranks: pd.Series | None = None) -> dict:
    lg = league_cells(season)
    cells = []
    for dkey, _, _, _ in DEPTHS:
        for loc in LOCATIONS:
            r = stats.loc[(dkey, loc)] if (dkey, loc) in stats.index else None
            l = lg.loc[(dkey, loc)] if (dkey, loc) in lg.index else None
            att = int(r["att"]) if r is not None else 0
            cells.append({
                "depth": dkey, "loc": loc, "att": att,
                "share": _num(att / total) if total else None,
                "cmp": _num(r["cmp"]) if att else None,
                "ypa": _num(r["ypa"], 2) if att else None,
                "epa": _num(r["epa"]) if att else None,
                "td": int(r["td"]) if att else 0,
                "int": int(r["int"]) if att else 0,
                "rank": int(ranks.loc[(dkey, loc)]) if ranks is not None and (dkey, loc) in ranks.index else None,
                "lg": {"cmp": _num(l["cmp"]), "ypa": _num(l["ypa"], 2), "epa": _num(l["epa"]),
                       "share": _num(l["share"])} if l is not None else None,
            })
    return {"season": season, "total_att": total, "cells": cells}


def _season_or_prior(fn, season: int | None, min_att: int):
    """Build the current season's grid; fall back to last season when this one
    hasn't got `min_att` attempts yet (week 1, injured/backup QBs)."""
    season = season or nd.current_season()
    out = fn(season)
    if out["total_att"] < min_att:
        try:
            prior = fn(season - 1)
            if prior["total_att"] > out["total_att"]:
                prior["fallback_from"] = season
                return prior
        except Exception as e:
            print(f"pass zones {season - 1} unavailable: {e}")
    return out


def player_zones(gsis_id: str, role: str = "pass", season: int | None = None, fallback: bool = True) -> dict:
    """role 'pass' = the player's throws, 'target' = the player's targets."""
    col = "passer_player_id" if role == "pass" else "receiver_player_id"

    def build(s):
        a = attempts(s)
        mine = a[a[col] == gsis_id]
        return {"role": role, "id": gsis_id, **_grid(_cell_stats(mine, ["depth", "loc"]), len(mine), s)}

    return _season_or_prior(build, season, 20 if fallback else 0)


def defense_zones(team: str, season: int | None = None, fallback: bool = True) -> dict:
    def build(s):
        dc = defense_cells(s)
        mine = dc.xs(team, level="defteam") if team in dc.index.get_level_values(0) else dc.iloc[0:0]
        total = int(mine["att"].sum()) if len(mine) else 0
        n_teams = dc.index.get_level_values(0).nunique()
        return {"role": "defense", "team": team, "out_of": int(n_teams),
                **_grid(mine, total, s, mine["epa_rank"] if len(mine) else None)}

    return _season_or_prior(build, season, 60 if fallback else 0)


def team_starting_qb(team: str, season: int | None = None) -> str | None:
    """Passer with the most attempts in the team's most recent game."""
    season = season or nd.current_season()
    for s in (season, season - 1):
        a = attempts(s)
        t = a[a["posteam"] == team]
        if t.empty:
            continue
        last = t[t["week"] == t["week"].max()]
        return last["passer_player_id"].value_counts().idxmax()
    return None


def matchup_zones(passer_id: str | None, defense: str, role: str = "pass", season: int | None = None) -> dict:
    return {
        "depths": [{"key": k, "label": l} for k, l, _, _ in DEPTHS],
        "locations": list(LOCATIONS),
        "player": player_zones(passer_id, role, season) if passer_id else None,
        "defense": defense_zones(defense, season),
    }


def warm(season: int | None = None) -> None:
    season = season or nd.current_season()
    for s in (season, season - 1):
        league_cells(s)
        defense_cells(s)
