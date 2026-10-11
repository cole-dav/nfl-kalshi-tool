"""
Big Data Bowl-inspired views built from public nflverse data (the tracking
replays themselves live in bdb_tracking.py).

  * leaderboards(season) -- tracking-derived player metrics the NFL publishes
    weekly through Next Gen Stats (CPOE, time to throw, rush yards over
    expected, separation, YAC over expected) plus PFR charting (missed-tackle
    rate, pressures). Stand-ins for the contest metrics (SLIME, RYAE, tackle
    conversion), which need raw tracking to compute.
  * tells(team) -- pre-snap look -> what happened. Offense: current season
    from FTN charting (QB alignment, backfield count, motion, no-huddle) plus
    last season's personnel groupings from participation. Defense: box count
    -> blitz rate this season (FTN), and box count -> man/blitz rate plus the
    coverage mix with EPA allowed last season (participation; nflverse only
    publishes coverage after a season ends).
  * routes(team) -- last season's targeted-route tree per receiver from
    participation (route of the targeted receiver) joined to pbp.

Team codes in and out are nflverse codes.
"""

from __future__ import annotations

import os
from functools import lru_cache

import numpy as np
import pandas as pd

import nflverse_data as nd

MIN = {"pass": 50, "rush": 20, "rec": 10, "tackle": 20, "cov": 15}


def _rows(df: pd.DataFrame, value: str, vol: str, extra: dict[str, str] | None = None) -> list[dict]:
    out = []
    for r in df.itertuples(index=False):
        row = {"name": getattr(r, "name"), "team": getattr(r, "team"), "value": float(getattr(r, value)),
               "vol": int(getattr(r, vol)), "id": getattr(r, "player_gsis_id", None)}
        for k, col in (extra or {}).items():
            v = getattr(r, col)
            row[k] = None if pd.isna(v) else round(float(v), 2)
        out.append(row)
    return out


def _board(key, title, unit, note, df, value, vol, vol_label, higher_better=True, dec=1, extra=None, neutral=False):
    df = df.dropna(subset=[value]).sort_values(value, ascending=not higher_better)
    wavg = float(np.average(df[value], weights=df[vol])) if len(df) else None
    return {"key": key, "title": title, "unit": unit, "note": note, "dec": dec, "higherBetter": higher_better,
            "volLabel": vol_label, "neutral": neutral, "avg": wavg, "rows": _rows(df, value, vol, extra)}


def _ngs_season(stat_type: str, season: int) -> pd.DataFrame:
    d = nd.load_nextgen(stat_type, season)
    d = d[(d["week"] == 0) & (d["season_type"] == "REG")] if (d["week"] == 0).any() else d
    return d.rename(columns={"player_display_name": "name", "team_abbr": "team"})


def leaderboards(season: int | None = None) -> dict:
    season = season or nd.current_season()
    boards = []

    p = _ngs_season("passing", season)
    p = p[p["attempts"] >= MIN["pass"]]
    boards += [
        _board("cpoe", "Completion % over expected", "%", "NGS expected completion % from receiver separation, "
               "air distance and pressure at the throw.", p, "completion_percentage_above_expectation",
               "attempts", "ATT", extra={"cmp": "completion_percentage", "xcmp": "expected_completion_percentage"}),
        _board("ttt", "Time to throw", "s", "Snap to release. Long holds pair with sack/pressure props.", p,
               "avg_time_to_throw", "attempts", "ATT", higher_better=False, dec=2),
        _board("aggr", "Aggressiveness", "%", "Share of throws into tight windows (defender within 1 yd).", p,
               "aggressiveness", "attempts", "ATT"),
    ]

    r = _ngs_season("rushing", season)
    r = r[r["rush_attempts"] >= MIN["rush"]]
    boards += [
        _board("ryoe", "Rush yards over expected / att", "yd", "NGS expected yards from blocker and defender "
               "positions at the handoff -- the 2020 Big Data Bowl winner's model.", r,
               "rush_yards_over_expected_per_att", "rush_attempts", "ATT", dec=2,
               extra={"ypc": "avg_rush_yards", "box8": "percent_attempts_gte_eight_defenders"}),
        _board("box8", "Rushes vs 8+ in the box", "%", "Stacked boxes faced; high = defenses selling out on "
               "the run.", r, "percent_attempts_gte_eight_defenders", "rush_attempts", "ATT", neutral=True),
    ]

    c = _ngs_season("receiving", season)
    c = c[c["targets"] >= MIN["rec"]]
    boards += [
        _board("sep", "Separation at catch / incompletion", "yd", "Distance to nearest defender when the ball "
               "arrives.", c, "avg_separation", "targets", "TGT", dec=2, extra={"cushion": "avg_cushion"}),
        _board("yacoe", "YAC over expected", "yd", "Yards after catch beyond the NGS expectation per "
               "reception.", c, "avg_yac_above_expectation", "receptions", "REC", dec=2,
               extra={"yac": "avg_yac", "xyac": "avg_expected_yac"}),
        _board("airshare", "Share of team intended air yards", "%", "Downfield role; tracks yardage-prop "
               "ceiling.", c, "percent_share_of_intended_air_yards", "targets", "TGT"),
    ]

    d = nd.load_pfr_def_week(season)
    d = (d.groupby(["pfr_player_name"], as_index=False)
         .agg(team=("team", "last"), tackles=("def_tackles_combined", "sum"), missed=("def_missed_tackles", "sum"),
              pressures=("def_pressures", "sum"), sacks=("def_sacks", "sum"), targets=("def_targets", "sum"),
              yds=("def_yards_allowed", "sum"), rating=("def_passer_rating_allowed", "mean"),
              games=("week", "nunique"))
         .rename(columns={"pfr_player_name": "name"}))
    d["attempts"] = d["tackles"] + d["missed"]
    d["miss_pct"] = 100 * d["missed"] / d["attempts"].where(d["attempts"] > 0)
    d["ypt"] = d["yds"] / d["targets"].where(d["targets"] > 0)
    boards += [
        _board("mtk", "Missed-tackle rate", "%", "PFR charting; missed / (tackles + missed). The public cousin of "
               "the 2024 winners' tackle-conversion metric.", d[d["attempts"] >= MIN["tackle"]], "miss_pct",
               "attempts", "ATT", higher_better=False),
        _board("prs", "Pressures", "", "QB pressures (hurries + hits + sacks), season total.",
               d[d["pressures"] > 0], "pressures", "games", "G", dec=0, extra={"sacks": "sacks"}),
        _board("cov", "Yards allowed per target", "yd", "Primary defender in coverage, PFR charting.",
               d[d["targets"] >= MIN["cov"]], "ypt", "targets", "TGT", higher_better=False, dec=2),
    ]
    weeks = sorted(int(w) for w in nd.load_nextgen("passing", season)["week"].unique() if w > 0)
    return {"season": season, "throughWeek": max(weeks) if weeks else None, "boards": boards}


# -- pre-snap tells -------------------------------------------------------------


def _scrimmage(pbp: pd.DataFrame) -> pd.DataFrame:
    p = pbp[pbp["play_type"].isin(["pass", "run"]) & pbp["posteam"].notna()].copy()
    p["is_pass"] = (p["play_type"] == "pass").astype(float)
    return p


def _split(df: pd.DataFrame, team_mask: pd.Series, by: str, labels: dict, outcome: str = "is_pass",
           min_n: int = 5) -> list[dict]:
    """Team rate of `outcome` per pre-snap bucket next to the league rate."""
    lg = df.groupby(by)[outcome].agg(["mean", "size"])
    tm = df[team_mask].groupby(by).agg(rate=(outcome, "mean"), n=(outcome, "size"), epa=("epa", "mean"))
    total = int(tm["n"].sum()) or 1
    rows = []
    for key, label in labels.items():
        if key not in tm.index or tm.loc[key, "n"] < min_n:
            continue
        t = tm.loc[key]
        rows.append({"label": label, "n": int(t["n"]), "share": float(t["n"] / total), "rate": float(t["rate"]),
                     "league": float(lg.loc[key, "mean"]) if key in lg.index else None,
                     "epa": None if pd.isna(t["epa"]) else float(t["epa"])})
    return rows


def _personnel(s: str) -> str | None:
    if not isinstance(s, str) or not s:
        return None
    n = {k: 0 for k in ("RB", "FB", "TE", "WR")}
    for part in s.split(","):
        bits = part.strip().split(" ")
        if len(bits) == 2 and bits[1] in n:
            n[bits[1]] = int(bits[0])
    return f"{n['RB'] + n['FB']}{n['TE']}"


def tells(team: str, season: int | None = None) -> dict:
    season = season or nd.current_season()
    prev = season - 1
    sections = []

    # Offense, this season: FTN pre-snap charting.
    pbp = _scrimmage(nd.load_pbp(season))
    ftn = nd.load_ftn(season)
    j = pbp.merge(ftn, left_on=["game_id", "play_id"], right_on=["nflverse_game_id", "nflverse_play_id"])
    off = j["posteam"] == team
    j["motion"] = j["is_motion"].astype(float)
    j["nohuddle"] = j["is_no_huddle"].astype(float)
    sections.append({"side": "offense", "season": season, "title": "Pass rate by pre-snap look",
                     "metric": "PASS %", "groups": [
        {"name": "QB alignment", "rows": _split(j, off, "qb_location", {"U": "Under center", "S": "Shotgun", "P": "Pistol"})},
        {"name": "Backs in backfield", "rows": _split(j, off, "n_offense_backfield", {0: "Empty", 1: "1 back", 2: "2 backs", 3: "3 backs"})},
        {"name": "Motion", "rows": _split(j, off, "motion", {1.0: "Motion", 0.0: "No motion"})},
        {"name": "Tempo", "rows": _split(j, off, "nohuddle", {1.0: "No huddle", 0.0: "Huddle"})},
    ]})

    # Defense, this season: box count -> blitz (FTN).
    dp = j[j["is_pass"] == 1].copy()
    dp["blitz"] = (dp["n_blitzers"] > 0).astype(float)
    dp["box"] = dp["n_defense_box"].clip(upper=8)
    box_labels = {5: "5 or fewer", 6: "6 in box", 7: "7 in box", 8: "8+ in box"}
    dp.loc[dp["box"] < 5, "box"] = 5
    sections.append({"side": "defense", "season": season, "title": "Blitz rate on dropbacks by box count",
                     "metric": "BLITZ %", "groups": [
        {"name": "Defenders in box", "rows": _split(dp, dp["defteam"] == team, "box", box_labels, "blitz")},
    ]})

    # Last season: personnel (offense) and coverage (defense), from participation.
    try:
        pp = _scrimmage(nd.load_pbp(prev))
        part = nd.load_participation(prev)
        q = pp.merge(part, left_on=["game_id", "play_id"], right_on=["nflverse_game_id", "play_id"])
    except Exception:
        q = None
    if q is not None and len(q):
        q["pers"] = q["offense_personnel"].map(_personnel)
        top = [k for k in q.loc[q["posteam"] == team, "pers"].value_counts().index[:6] if k]
        sections.append({"side": "offense", "season": prev, "title": "Pass rate by personnel",
                         "metric": "PASS %", "groups": [
            {"name": "Personnel (RB+FB, TE)", "rows": _split(q, q["posteam"] == team, "pers", {k: k for k in top})},
        ]})
        dq = q[(q["is_pass"] == 1) & q["defense_man_zone_type"].isin(["MAN_COVERAGE", "ZONE_COVERAGE"])].copy()
        dq["man"] = (dq["defense_man_zone_type"] == "MAN_COVERAGE").astype(float)
        dq["box"] = dq["defenders_in_box"].clip(lower=5, upper=8)
        dmask = dq["defteam"] == team
        cov_labels = {"COVER_0": "Cover 0", "COVER_1": "Cover 1", "2_MAN": "2-Man", "COVER_2": "Cover 2",
                      "COVER_3": "Cover 3", "COVER_4": "Cover 4", "COVER_6": "Cover 6", "COVER_9": "Cover 9"}
        # Coverage mix: the "rate" is the shell's share of the team's dropbacks.
        mix = _split(dq, dmask, "defense_coverage_type", cov_labels, "man", min_n=3)
        lg_share = dq["defense_coverage_type"].value_counts(normalize=True)
        for row, key in zip(mix, [k for k in cov_labels if any(r["label"] == cov_labels[k] for r in mix)]):
            row["rate"], row["league"] = row["share"], float(lg_share.get(key, 0))
        sections.append({"side": "defense", "season": prev, "title": "Man rate on dropbacks by box count",
                         "metric": "MAN %", "groups": [
            {"name": "Defenders in box", "rows": _split(dq, dmask, "box", box_labels, "man")},
        ]})
        sections.append({"side": "defense", "season": prev, "title": "Coverage mix and EPA allowed",
                         "metric": "SHARE", "mix": True, "groups": [{"name": "Coverage shell", "rows": mix}]})
    return {"team": team, "season": season, "sections": sections}


# -- route trees ----------------------------------------------------------------


def routes(team: str, season: int | None = None, min_targets: int = 15) -> dict:
    season = (season or nd.current_season()) - 1
    pbp = nd.load_pbp(season)
    part = nd.load_participation(season)
    p = pbp[(pbp["pass_attempt"] == 1) & pbp["receiver_player_id"].notna() & (pbp["posteam"] == team)]
    q = p.merge(part[["nflverse_game_id", "play_id", "route", "defense_man_zone_type"]],
                left_on=["game_id", "play_id"], right_on=["nflverse_game_id", "play_id"])
    q = q[q["route"].fillna("").str.len() > 0]
    out = []
    for (pid, name), g in q.groupby(["receiver_player_id", "receiver_player_name"]):
        if len(g) < min_targets:
            continue
        rows = []
        for route, rg in g.groupby("route"):
            rows.append({"route": route, "tgt": len(rg), "share": len(rg) / len(g),
                         "catch": float(rg["complete_pass"].mean()), "ypt": float(rg["yards_gained"].fillna(0).mean()),
                         "epa": float(rg["epa"].mean()), "adot": float(rg["air_yards"].mean())})
        rows.sort(key=lambda r: -r["tgt"])
        mz = {}
        for k, lab in (("MAN_COVERAGE", "man"), ("ZONE_COVERAGE", "zone")):
            sub = g[g["defense_man_zone_type"] == k]
            if len(sub):
                mz[lab] = {"tgt": len(sub), "epa": float(sub["epa"].mean()), "ypt": float(sub["yards_gained"].fillna(0).mean())}
        out.append({"id": pid, "name": name, "tgt": len(g), "epa": float(g["epa"].mean()), "routes": rows,
                    "vs": mz})
    out.sort(key=lambda r: -r["tgt"])
    return {"team": team, "season": season, "receivers": out}


# -- research-ticket panel: one player's ranks, THIS season only -----------------


def _name_key(s: str) -> str:
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def player_ngs(gsis_id: str | None, name: str) -> dict:
    """The player's rank on every current-season board he qualifies for.
    Betting-facing, so it only ever reads the current season."""
    season = nd.current_season()
    data = leaderboards(season)
    key = _name_key(name)
    out = []
    for b in data["boards"]:
        rows = b["rows"]
        i = next((i for i, r in enumerate(rows) if (gsis_id and r.get("id") == gsis_id)), None)
        if i is None:
            i = next((i for i, r in enumerate(rows) if _name_key(r["name"]) == key), None)
        if i is None:
            continue
        r = rows[i]
        out.append({"key": b["key"], "title": b["title"], "unit": b["unit"], "dec": b["dec"], "value": r["value"],
                    "rank": i + 1, "out_of": len(rows), "avg": b["avg"], "higherBetter": b["higherBetter"],
                    "neutral": b["neutral"], "vol": r["vol"], "volLabel": b["volLabel"]})
    return {"season": season, "throughWeek": data["throughWeek"], "name": name, "metrics": out}


# -- Viz Lab: draft classes 2023-25 (BDB 2027) -- historical, Lab only -----------

BDB27 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bdb", "2027", "nfl-big-data-bowl-2027")
GROUPS = {
    "receivers": {"label": "Receivers", "positions": {"WR", "TE"}, "min": ("routes", 100)},
    "blockers": {"label": "Pass protection", "positions": {"T", "G", "C"}, "min": ("pass_blocks", 100)},
    "rushers": {"label": "Pass rush", "positions": {"DE", "OLB", "DT", "NT", "ILB", "MLB"}, "min": ("rushes", 100)},
    "coverage": {"label": "Coverage", "positions": {"CB", "SS", "FS", "ILB", "MLB", "OLB"}, "min": ("cov_snaps", 100)},
}
# (key, label, decimals, which end is better: "hi" / "lo" / None for volume or descriptive columns)
COLUMNS = {
    "receivers": [("routes", "ROUTES", 0, None), ("targets", "TGT", 0, None), ("tprr", "TGT/ROUTE", 2, "hi"),
                  ("sep", "SEP @ THROW", 2, "hi"), ("cushion", "CUSHION", 1, None), ("yprr", "YDS/ROUTE", 2, "hi"),
                  ("yacoe", "YAC OVER EXP", 2, "hi")],
    "blockers": [("pass_blocks", "PASS BLOCKS", 0, None), ("press_rate", "PRESSURE %", 1, "lo"),
                 ("sacks", "SACKS ALLOWED", 1, "lo"), ("peak_pp", "AVG PEAK P(PRESSURE)", 3, "lo")],
    "rushers": [("rushes", "RUSHES", 0, None), ("get_off", "GET-OFF (S)", 3, "lo"), ("press_rate", "PRESSURE %", 1, "hi"),
                ("ttp", "TIME TO PRESSURE", 2, "lo"), ("sacks", "SACKS", 1, "hi"), ("quick", "QUICK PRESSURES", 0, "hi")],
    "coverage": [("cov_snaps", "COVERAGE SNAPS", 0, None), ("man_share", "MAN %", 1, None), ("tackles", "TACKLES", 0, "hi"),
                 ("tfl", "TFL", 0, "hi"), ("ff", "FORCED FUMBLES", 0, "hi")],
}


@lru_cache(maxsize=1)
def _bdb27() -> tuple[pd.DataFrame, pd.DataFrame]:
    import polars as pl
    if not os.path.exists(os.path.join(BDB27, "player_play.csv")):
        raise ValueError("BDB 2027 data not found under data/bdb/2027/")
    pp = pl.read_csv(os.path.join(BDB27, "player_play.csv"), null_values="NA", infer_schema_length=100000)
    games = pl.read_csv(os.path.join(BDB27, "games.csv")).filter(pl.col("season_type") != "PRE")
    pp = pp.join(games.select("game_id", "season"), on="game_id")
    players = pd.read_csv(os.path.join(BDB27, "players.csv"))
    combine = pd.read_csv(os.path.join(BDB27, "combine_results.csv"), na_values="NA")
    players = players.merge(combine[["nfl_id", "forty", "ngs_athleticism_score"]], on="nfl_id", how="left")
    return pp.to_pandas(), players


def draft_classes(group: str, season: int) -> dict:
    if group not in GROUPS:
        raise ValueError(f"unknown group {group!r}")
    pp, players = _bdb27()
    cfg = GROUPS[group]
    ids = set(players.loc[players["nfl_position"].isin(cfg["positions"]), "nfl_id"])
    d = pp[(pp["season"] == season) & pp["nfl_id"].isin(ids)]
    g = d.groupby("nfl_id")
    if group == "receivers":
        t = pd.DataFrame({
            "routes": g["route_ran"].count(), "targets": g["target"].sum(),
            "sep": g["separation_at_pass_forward"].mean(), "cushion": g["cushion"].mean(),
            "rec_yards": g["rec_yards"].sum(),
            "yacoe": (d["yards_after_catch"] - d["expected_yards_after_catch"]).groupby(d["nfl_id"]).mean(),
        })
        t["tprr"] = t["targets"] / t["routes"]
        t["yprr"] = t["rec_yards"] / t["routes"]
    elif group == "blockers":
        t = pd.DataFrame({"pass_blocks": g["pressure_allowed"].count(),
                          "press_rate": 100 * g["pressure_allowed"].mean(), "sacks": g["sack_allowed"].sum(),
                          "peak_pp": g["peak_pressure_probability_allowed"].mean()})
    elif group == "rushers":
        rushes = g["player_get_off"].count()
        t = pd.DataFrame({"rushes": rushes, "get_off": g["player_get_off"].mean(),
                          "press_rate": 100 * g["time_to_pressure"].count() / rushes.where(rushes > 0),
                          "ttp": g["time_to_pressure"].mean(), "sacks": g["sack"].sum(),
                          "quick": g["quick_pressure"].sum()})
    else:
        cov = d[d["coverage_assignment"].notna() & (d["coverage_assignment"] != "NO_ASSIGNMENT")]
        cg = cov.groupby("nfl_id")
        t = pd.DataFrame({"cov_snaps": cg.size(), "man_share": 100 * (cov["coverage_assignment"] == "MAN")
                          .groupby(cov["nfl_id"]).mean(),
                          "tackles": g["tackle"].sum(), "tfl": g["tackle_for_loss"].sum(),
                          "ff": g["caused_forced_fumble"].sum()})
    col, minimum = cfg["min"]
    t = t[t[col].fillna(0) >= minimum].join(players.set_index("nfl_id"), how="left")
    team = d.groupby("nfl_id")["team_abbr"].agg(lambda s: s.mode().iat[0] if len(s.mode()) else None)
    rows = []
    for nid, r in t.iterrows():
        row = {"id": int(nid), "name": r["display_name"], "pos": r["nfl_position"], "team": team.get(nid),
               "draft": int(r["draft_year"]), "pick": None if pd.isna(r["draft_overall_pick"]) else int(r["draft_overall_pick"]),
               "forty": None if pd.isna(r["forty"]) else float(r["forty"]),
               "ath": None if pd.isna(r["ngs_athleticism_score"]) else float(r["ngs_athleticism_score"])}
        for k, _, _, _ in COLUMNS[group]:
            v = r[k]
            row[k] = None if pd.isna(v) else float(v)
        rows.append(row)
    rows.sort(key=lambda x: -(x[col] or 0))
    return {"group": group, "label": cfg["label"], "season": season, "seasons": [2023, 2024, 2025],
            "minimum": {"col": col, "n": minimum}, "columns": [{"key": k, "label": l, "dec": dc, "better": bt} for k, l, dc, bt in COLUMNS[group]],
            "groups": {k: v["label"] for k, v in GROUPS.items()}, "rows": rows}
