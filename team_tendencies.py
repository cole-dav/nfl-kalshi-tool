"""
Team tendencies + game-script read, built from nflverse play-by-play (the same
feed NFL Savant's tendency tab is built on).

Two league-wide tables per season, one row per team:
  - offense: what a team does with the ball (drive outcomes, situational pass
    rates, pace, where the targets/carries go);
  - defense: the same numbers *allowed* -- i.e. where a defense forces the
    ball, how often it gets 3-and-outs, how it holds up in the red zone.

`matchup_tendencies(away, home, spread, total)` pairs each offense against
the opposing defense with league ranks and turns the biggest gaps into a
rule-based "script read" (who likely leads, how that shifts the run/pass
mix, where the targets go, TD-vs-FG in the red zone). It's descriptive, not a
projection model -- the raw numbers are shown next to every read.

All team codes are nflverse codes.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np
import pandas as pd

import nflverse_data as nd

# Neutral script: game still in doubt, not a 2-minute drill.
NEUTRAL_WP = (0.20, 0.80)
TWO_MIN = 120
BIG_LEAD = 8  # one-score games are "neutral"; 8+ is a script-changing lead
# Early-season samples are tiny, so script reads use each rate regressed toward
# the team's prior-season value, as if the prior season were PRIOR_GAMES games.
PRIOR_GAMES = 3


def _plays(pbp: pd.DataFrame) -> pd.DataFrame:
    """Scrimmage plays (pass/run, penalties that negated the play excluded)."""
    p = pbp[pbp["play_type"].isin(["pass", "run"]) & pbp["posteam"].notna()].copy()
    p["is_pass"] = (p["play_type"] == "pass").astype(float)
    p["late_half"] = p["half_seconds_remaining"] <= TWO_MIN
    p["neutral"] = p["wp"].between(*NEUTRAL_WP) & ~p["late_half"]
    p["explosive"] = np.where(p["is_pass"] == 1, p["yards_gained"] >= 20, p["yards_gained"] >= 10).astype(float)
    return p


def _drives(pbp: pd.DataFrame) -> pd.DataFrame:
    """One row per (game, offense, drive). Drives that were just the clock
    running out at the half/game ('End of half') are dropped -- they'd count
    as neither success nor failure."""
    d = (pbp[pbp["posteam"].notna() & pbp["fixed_drive"].notna()]
         .groupby(["game_id", "posteam", "defteam", "fixed_drive"], as_index=False)
         .agg(result=("fixed_drive_result", "first"), first_downs=("drive_first_downs", "max"),
              n_plays=("drive_play_count", "max"), inside20=("drive_inside20", "max"),
              start_yl=("yardline_100", "first"), qtr=("qtr", "first")))
    d = d[d["result"] != "End of half"]
    d["three_out"] = ((d["result"] == "Punt") & (d["first_downs"].fillna(0) == 0)).astype(float)
    d["td"] = (d["result"] == "Touchdown").astype(float)
    d["score"] = d["result"].isin(["Touchdown", "Field goal"]).astype(float)
    d["giveaway"] = (d["result"] == "Turnover").astype(float)
    d["rz_trip"] = (d["inside20"].fillna(0) > 0).astype(float)
    d["rz_td"] = d["td"] * d["rz_trip"]
    return d


def _half_points(pbp: pd.DataFrame) -> pd.DataFrame:
    """Points scored per team per game, split first half / rest of game."""
    rows = []
    for gid, g in pbp.groupby("game_id"):
        h1 = g[g["qtr"] <= 2]
        home, away = g["home_team"].iloc[0], g["away_team"].iloc[0]
        h1_home = h1["total_home_score"].max() if len(h1) else 0
        h1_away = h1["total_away_score"].max() if len(h1) else 0
        fin_home, fin_away = g["total_home_score"].max(), g["total_away_score"].max()
        q1 = g[g["qtr"] == 1]
        q1_home = q1["total_home_score"].max() if len(q1) else 0
        q1_away = q1["total_away_score"].max() if len(q1) else 0
        rows.append({"game_id": gid, "team": home, "opp": away, "q1": q1_home, "h1": h1_home, "full": fin_home})
        rows.append({"game_id": gid, "team": away, "opp": home, "q1": q1_away, "h1": h1_away, "full": fin_away})
    df = pd.DataFrame(rows)
    df["h2"] = df["full"] - df["h1"]
    return df


def _rate(num: pd.Series, den: pd.Series) -> pd.Series:
    return (num / den.replace(0, np.nan)).astype(float)


def _side_table(pbp: pd.DataFrame, rosters: pd.DataFrame, key: str) -> pd.DataFrame:
    """Every metric grouped by `key` ('posteam' = offense, 'defteam' = what
    that defense allowed). Returns one row per team."""
    plays, drives = _plays(pbp), _drives(pbp)
    g = plays.groupby(key)
    out = pd.DataFrame({
        "games": g["game_id"].nunique(),
        "plays_per_game": g.size() / g["game_id"].nunique(),
        "epa_play": g["epa"].mean(),
        "success": g["success"].mean(),
        "explosive": g["explosive"].mean(),
        "pass_rate": g["is_pass"].mean(),
        "no_huddle": g["no_huddle"].mean(),
        "shotgun": g["shotgun"].mean(),
    })

    neu = plays[plays["neutral"]]
    gn = neu.groupby(key)
    out["neutral_pass"] = gn["is_pass"].mean()
    out["proe"] = gn["pass_oe"].mean()  # nflverse: pass rate over expected, in pct points
    early = neu[neu["down"].isin([1, 2])]
    out["early_down_pass"] = early.groupby(key)["is_pass"].mean()

    not_late = plays[~plays["late_half"]]
    # Offense's own score differential; for a defense, the *opponent's* lead.
    out["pass_leading"] = not_late[not_late["score_differential"] >= BIG_LEAD].groupby(key)["is_pass"].mean()
    out["pass_trailing"] = not_late[not_late["score_differential"] <= -BIG_LEAD].groupby(key)["is_pass"].mean()
    out["snaps_leading"] = not_late[not_late["score_differential"] >= BIG_LEAD].groupby(key).size()
    out["snaps_trailing"] = not_late[not_late["score_differential"] <= -BIG_LEAD].groupby(key).size()
    out["pass_1h"] = plays[plays["qtr"] <= 2].groupby(key)["is_pass"].mean()
    # First 15 offensive snaps of each game ~ the scripted opening.
    first = plays.sort_values(["game_id", "play_id"])
    first = first[first.groupby(["game_id", "posteam"]).cumcount() < 15]
    out["pass_script15"] = first.groupby(key)["is_pass"].mean()

    # Pace: seconds between consecutive snaps inside a drive, neutral script.
    pace = plays.sort_values(["game_id", "play_id"]).copy()
    pace["gap"] = pace.groupby(["game_id", "posteam", "fixed_drive"])["game_seconds_remaining"].diff(-1)
    pace = pace[pace["neutral"] & pace["gap"].between(1, 60)]
    out["sec_per_play"] = pace.groupby(key)["gap"].mean()

    third = plays[plays["down"] == 3]
    out["third_conv"] = third.groupby(key)["third_down_converted"].mean()
    out["third_dist"] = third.groupby(key)["ydstogo"].mean()
    fourth = pbp[(pbp["down"] == 4) & (pbp["ydstogo"] <= 2) & (pbp["yardline_100"] <= 60)
                 & pbp["play_type"].isin(["pass", "run", "punt", "field_goal"])]
    out["fourth_go"] = fourth.groupby(key)["play_type"].apply(lambda s: s.isin(["pass", "run"]).mean())

    # Drives
    dk = "posteam" if key == "posteam" else "defteam"
    gd = drives.groupby(dk)
    out["drives_per_game"] = gd.size() / gd["game_id"].nunique()
    out["three_out"] = gd["three_out"].mean()
    out["score_drive"] = gd["score"].mean()
    out["td_drive"] = gd["td"].mean()
    out["giveaway_drive"] = gd["giveaway"].mean()
    out["start_own"] = 100 - gd["start_yl"].mean()  # avg start, own yard line
    out["rz_trips_pg"] = gd["rz_trip"].sum() / gd["game_id"].nunique()
    out["rz_td"] = _rate(gd["rz_td"].sum(), gd["rz_trip"].sum())

    # Where the ball goes: targets by receiver position, depth, location
    pos = rosters.drop_duplicates("gsis_id").set_index("gsis_id")["position"]
    tg = plays[(plays["is_pass"] == 1) & plays["receiver_player_id"].notna()].copy()
    tg["rpos"] = tg["receiver_player_id"].map(pos).fillna("WR").replace({"FB": "RB"})
    n_tg = tg.groupby(key).size()
    for p_ in ("WR", "TE", "RB"):
        sub = tg[tg["rpos"] == p_]
        out[f"tgt_{p_.lower()}"] = _rate(sub.groupby(key).size(), n_tg)
        out[f"epa_tgt_{p_.lower()}"] = sub.groupby(key)["epa"].mean()
    out["adot"] = tg.groupby(key)["air_yards"].mean()
    out["deep_rate"] = _rate(tg[tg["air_yards"] >= 20].groupby(key).size(), n_tg)
    out["short_rate"] = _rate(tg[tg["air_yards"] <= 0].groupby(key).size(), n_tg)
    for loc in ("left", "middle", "right"):
        out[f"tgt_{loc}"] = _rate(tg[tg["pass_location"] == loc].groupby(key).size(), n_tg)

    runs = plays[(plays["is_pass"] == 0) & plays["run_location"].notna()]
    n_run = runs.groupby(key).size()
    for loc in ("left", "middle", "right"):
        sub = runs[runs["run_location"] == loc]
        out[f"run_{loc}"] = _rate(sub.groupby(key).size(), n_run)
        out[f"ypc_{loc}"] = sub.groupby(key)["yards_gained"].mean()
    out["ypc"] = plays[plays["is_pass"] == 0].groupby(key)["yards_gained"].mean()

    db = pbp[(pbp["qb_dropback"] == 1) & pbp[key].notna()]
    out["sack_rate"] = db.groupby(key)["sack"].mean()
    out["epa_dropback"] = db.groupby(key)["epa"].mean()
    out["epa_rush"] = plays[plays["is_pass"] == 0].groupby(key)["epa"].mean()

    # Points by half (offense = scored, defense = allowed)
    hp = _half_points(pbp).groupby("team" if key == "posteam" else "opp")
    out["pts_q1"] = hp["q1"].mean()
    out["pts_1h"] = hp["h1"].mean()
    out["pts_2h"] = hp["h2"].mean()
    out["pts"] = hp["full"].mean()

    return out.reset_index().rename(columns={key: "team"})


@nd.memo_on_data("pbp", "rosters")
def offense_table(season: int | None = None) -> pd.DataFrame:
    return _side_table(nd.load_pbp(season), nd.load_rosters(season), "posteam")


@nd.memo_on_data("pbp", "rosters")
def defense_table(season: int | None = None) -> pd.DataFrame:
    return _side_table(nd.load_pbp(season), nd.load_rosters(season), "defteam")


@lru_cache(maxsize=4)
def coverage_table(season: int) -> pd.DataFrame:
    """Coverage shell + man/zone per defense from participation data (only
    published for completed seasons)."""
    part = nd.load_participation(season)
    pbp = nd.load_pbp(season)[["game_id", "play_id", "defteam", "qb_dropback"]]
    j = part.merge(pbp, left_on=["nflverse_game_id", "play_id"], right_on=["game_id", "play_id"])
    j = j[(j["qb_dropback"] == 1) & j["defense_coverage_type"].notna()]
    rows = []
    for team, g in j.groupby("defteam"):
        mz = g["defense_man_zone_type"].value_counts(normalize=True)
        shells = g["defense_coverage_type"].value_counts(normalize=True).head(3)
        rows.append({"team": team,
                     "man_rate": float(mz.get("MAN_COVERAGE", np.nan)),
                     "top_shells": [{"shell": k.replace("_", "-").title().replace("Cover-", "Cov "), "pct": float(v)}
                                    for k, v in shells.items()]})
    return pd.DataFrame(rows)


# -- metric catalogue -------------------------------------------------------------
# key, label, format, better ('high' | 'low' | None = pure tendency, no good/bad),
# for each side. `def_better` is from the *defense's* point of view.

METRICS = [
    # group, key, label, fmt, off_better, def_better
    ("DRIVES", "three_out", "3-and-out %", "pct", "low", "high"),
    ("DRIVES", "score_drive", "Scoring drive %", "pct", "high", "low"),
    ("DRIVES", "td_drive", "TD drive %", "pct", "high", "low"),
    ("DRIVES", "giveaway_drive", "Turnover drive %", "pct", "low", "high"),
    ("DRIVES", "drives_per_game", "Drives / game", "f1", None, None),
    ("DRIVES", "start_own", "Avg start (own yd)", "f1", "high", "low"),
    ("RED ZONE", "rz_trips_pg", "RZ trips / game", "f1", "high", "low"),
    ("RED ZONE", "rz_td", "RZ TD %", "pct", "high", "low"),
    ("SCORING", "pts_q1", "1Q points", "f1", "high", "low"),
    ("SCORING", "pts_1h", "1H points", "f1", "high", "low"),
    ("SCORING", "pts_2h", "2H points", "f1", "high", "low"),
    ("EFFICIENCY", "epa_play", "EPA / play", "f2", "high", "low"),
    ("EFFICIENCY", "success", "Success rate", "pct", "high", "low"),
    ("EFFICIENCY", "explosive", "Explosive play %", "pct", "high", "low"),
    ("EFFICIENCY", "third_conv", "3rd down conv %", "pct", "high", "low"),
    ("EFFICIENCY", "third_dist", "Avg 3rd down dist", "f1", "low", "high"),
    ("EFFICIENCY", "sack_rate", "Sack rate", "pct", "low", "high"),
    ("PLAY CALLING", "neutral_pass", "Neutral pass rate", "pct", None, None),
    ("PLAY CALLING", "proe", "Pass rate over exp.", "pp", None, None),
    ("PLAY CALLING", "early_down_pass", "Early-down pass (neutral)", "pct", None, None),
    ("PLAY CALLING", "pass_script15", "Pass rate, first 15 plays", "pct", None, None),
    ("PLAY CALLING", "pass_1h", "1H pass rate", "pct", None, None),
    ("PLAY CALLING", "pass_leading", f"Pass rate up {BIG_LEAD}+", "pct", None, None),
    ("PLAY CALLING", "pass_trailing", f"Pass rate down {BIG_LEAD}+", "pct", None, None),
    ("PLAY CALLING", "fourth_go", "4th & ≤2 go rate (opp side)", "pct", None, None),
    ("PACE", "plays_per_game", "Plays / game", "f1", None, None),
    ("PACE", "sec_per_play", "Sec / play (neutral)", "f1", None, None),
    ("PACE", "no_huddle", "No-huddle rate", "pct", None, None),
    ("TARGETS", "tgt_wr", "Target share: WR", "pct", None, None),
    ("TARGETS", "tgt_te", "Target share: TE", "pct", None, None),
    ("TARGETS", "tgt_rb", "Target share: RB", "pct", None, None),
    ("TARGETS", "epa_tgt_wr", "EPA / target: WR", "f2", "high", "low"),
    ("TARGETS", "epa_tgt_te", "EPA / target: TE", "f2", "high", "low"),
    ("TARGETS", "epa_tgt_rb", "EPA / target: RB", "f2", "high", "low"),
    ("TARGETS", "adot", "aDOT", "f1", None, None),
    ("TARGETS", "deep_rate", "Deep (20+ air) target %", "pct", None, None),
    ("TARGETS", "short_rate", "Behind-LOS target %", "pct", None, None),
    ("TARGETS", "tgt_left", "Targets left", "pct", None, None),
    ("TARGETS", "tgt_middle", "Targets middle", "pct", None, None),
    ("TARGETS", "tgt_right", "Targets right", "pct", None, None),
    ("RUSHING", "ypc", "Yards / carry", "f1", "high", "low"),
    ("RUSHING", "epa_rush", "EPA / rush", "f2", "high", "low"),
    ("RUSHING", "run_left", "Runs left", "pct", None, None),
    ("RUSHING", "run_middle", "Runs middle", "pct", None, None),
    ("RUSHING", "run_right", "Runs right", "pct", None, None),
    ("RUSHING", "ypc_left", "YPC left", "f1", "high", "low"),
    ("RUSHING", "ypc_middle", "YPC middle", "f1", "high", "low"),
    ("RUSHING", "ypc_right", "YPC right", "f1", "high", "low"),
]


def _num(v):
    return None if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)


def _metric(tbl: pd.DataFrame, team: str, key: str, better: str | None) -> dict:
    """Value, league rank (1 = best, or 1 = highest for pure tendencies) and
    league average for one team/metric."""
    valid = tbl.dropna(subset=[key])
    row = valid[valid["team"] == team]
    if row.empty:
        return {"value": None, "rank": None, "lg": _num(valid[key].mean()) if len(valid) else None}
    ranks = valid[key].rank(ascending=(better == "low"), method="min")
    return {"value": _num(row[key].iloc[0]), "rank": int(ranks[row.index[0]]),
            "lg": _num(valid[key].mean()), "out_of": int(len(valid))}


def _side_metrics(team: str, side: str, season: int, prior: int, games: int) -> dict:
    tbl = offense_table(season) if side == "off" else defense_table(season)
    try:
        ly = offense_table(prior) if side == "off" else defense_table(prior)
    except Exception:
        ly = None
    out = {}
    for group, key, label, fmt, off_b, def_b in METRICS:
        better = off_b if side == "off" else def_b
        m = _metric(tbl, team, key, better)
        if ly is not None and key in ly.columns:
            r = ly[ly["team"] == team]
            m["ly"] = _num(r[key].iloc[0]) if not r.empty else None
        if m["value"] is not None and m.get("ly") is not None:
            m["blend"] = (games * m["value"] + PRIOR_GAMES * m["ly"]) / (games + PRIOR_GAMES)
        out[key] = m
    return out


def team_tendencies(team: str, season: int | None = None) -> dict:
    season = season or nd.current_season()
    prior = season - 1
    games = offense_table(season)
    g = games.loc[games["team"] == team, "games"]
    n = int(g.iloc[0]) if not g.empty else 0
    cov = None
    try:
        ct = coverage_table(prior)
        r = ct[ct["team"] == team]
        if not r.empty:
            cov = {"season": prior, "man_rate": _num(r["man_rate"].iloc[0]), "top_shells": r["top_shells"].iloc[0]}
    except Exception as e:
        print(f"coverage table {prior} unavailable: {e}")
    return {
        "team": team, "season": season, "prior_season": prior,
        "games": n,
        "prior_weight_games": PRIOR_GAMES,
        "off": _side_metrics(team, "off", season, prior, n),
        "def": _side_metrics(team, "def", season, prior, n),
        "coverage": cov,
    }


# -- script read ------------------------------------------------------------------

def _pct(v):
    return f"{v * 100:.0f}%" if v is not None else "--"


def _implied_points(spread: dict | None, total: dict | None, away: str, home: str) -> dict | None:
    """Kalshi spread {team, line} (team favored by `line`) + total -> implied
    points per team (nflverse codes)."""
    if not spread or not total or spread.get("line") is None or total.get("line") is None:
        return None
    fav = nd.kalshi_to_nflverse_team(spread["team"])
    margin, tot = float(spread["line"]), float(total["line"])
    if fav not in (away, home):
        return None
    dog = home if fav == away else away
    return {"fav": fav, "dog": dog, "margin": margin, "total": tot,
            fav: (tot + margin) / 2, dog: (tot - margin) / 2}


def _v(side: dict, key: str):
    """Value used by the script read: regressed toward last season when available."""
    m = side.get(key, {})
    return m.get("blend", m.get("value"))


def _lg(side: dict, key: str):
    return side.get(key, {}).get("lg")


def _script_read(off_team: str, off: dict, dfn_team: str, dfn: dict, implied: dict | None) -> list[dict]:
    """Rule-based bullets for one offense vs the opposing defense. Each has a
    `tag` (what market it bears on) and `lean` ('up' | 'down' | None)."""
    reads: list[dict] = []

    def add(tag, text, lean=None):
        reads.append({"tag": tag, "text": text, "lean": lean})

    # 1. Expected game state -> run/pass mix
    if implied:
        pts = implied[off_team]
        lead = implied["fav"] == off_team
        pl, pt, pn = _v(off, "pass_leading"), _v(off, "pass_trailing"), _v(off, "neutral_pass")
        state = (f"favored by {implied['margin']:.1f}" if lead else f"+{implied['margin']:.1f} underdog")
        base = f"Implied {pts:.1f} pts ({state}, total {implied['total']:.1f})."
        if implied["margin"] >= 3 and pn is not None:
            if lead and pl is not None:
                add("SCRIPT", f"{base} Likely playing with a lead: passes {_pct(pl)} up {BIG_LEAD}+ vs {_pct(pn)} neutral "
                              f"→ 2H tilts {'run (RB carries up, pass volume down)' if pl < pn else 'pass'}.",
                    None if pl < pn else "up")
            elif not lead and pt is not None:
                add("SCRIPT", f"{base} Likely chasing: passes {_pct(pt)} down {BIG_LEAD}+ vs {_pct(pn)} neutral "
                              f"→ 2H pass volume {'up' if pt > pn else 'flat'}, RB carries at risk.", "up" if pt > pn else None)
        else:
            add("SCRIPT", f"{base} Close spread → expect neutral script most of the game (neutral pass rate {_pct(pn)}).")

    # 2. First half: team 1H scoring vs defense 1H allowed
    o1, d1 = _v(off, "pts_1h"), _v(dfn, "pts_1h")
    if o1 is not None and d1 is not None:
        lg1 = _lg(off, "pts_1h") or 0
        exp = (o1 + d1) / 2
        add("1H", f"1H: scores {o1:.1f} / game, {dfn_team} allows {d1:.1f} (lg {lg1:.1f}) → ~{exp:.1f} 1H pts blended. "
                  f"Opening-script pass rate {_pct(_v(off, 'pass_script15'))}.",
            "up" if exp > lg1 + 1.5 else "down" if exp < lg1 - 1.5 else None)

    # 3. Drive killers: 3-and-outs
    o3, d3, lg3 = _v(off, "three_out"), _v(dfn, "three_out"), _lg(off, "three_out")
    if None not in (o3, d3, lg3):
        blended = (o3 + d3) / 2
        lean = "down" if blended > lg3 * 1.15 else "up" if blended < lg3 * 0.85 else None
        add("DRIVES", f"3-and-out: {off_team} {_pct(o3)} of drives, {dfn_team} forces {_pct(d3)} (lg {_pct(lg3)}). "
                      + ("Short drives → fewer snaps for skill players." if lean == "down"
                         else "Sustained drives → more snaps/volume." if lean == "up" else "About league average."), lean)

    # 4. Red zone: TD vs FG
    orz, drz, lgrz = _v(off, "rz_td"), _v(dfn, "rz_td"), _lg(off, "rz_td")
    otr, dtr = _v(off, "rz_trips_pg"), _v(dfn, "rz_trips_pg")
    if None not in (orz, drz, lgrz):
        blended = (orz + drz) / 2
        lean = "up" if blended > lgrz + 0.05 else "down" if blended < lgrz - 0.05 else None
        add("RED ZONE", f"RZ TD%: {off_team} {_pct(orz)} ({otr or 0:.1f} trips/g), {dfn_team} allows {_pct(drz)} "
                        f"({dtr or 0:.1f} trips/g) vs lg {_pct(lgrz)} → "
                        + ("TD-friendly (anytime TD props)." if lean == "up" else "FG-leaning (kicker props, TD unders)."
                           if lean == "down" else "neutral."), lean)

    # 5. Where the defense forces the ball
    shares = {p: (_v(dfn, f"tgt_{p}"), _lg(dfn, f"tgt_{p}"), _v(dfn, f"epa_tgt_{p}")) for p in ("wr", "te", "rb")}
    tilt = [(p, v - lg, e) for p, (v, lg, e) in shares.items() if v is not None and lg is not None]
    if tilt:
        p, diff, epa = max(tilt, key=lambda x: x[1])
        if diff > 0.02:
            add("TARGETS", f"{dfn_team} funnels targets to {p.upper()}s: {_pct(shares[p][0])} of targets vs lg "
                           f"{_pct(shares[p][1])} (EPA/tgt allowed {epa:+.2f}) → {p.upper()} reception volume up.", "up")
        low = min(tilt, key=lambda x: x[1])
        if low[1] < -0.02:
            add("TARGETS", f"{dfn_team} suppresses {low[0].upper()} targets: {_pct(shares[low[0]][0])} vs lg "
                           f"{_pct(shares[low[0]][1])}.", "down")
    dd, lgdd = _v(dfn, "deep_rate"), _lg(dfn, "deep_rate")
    if dd is not None and lgdd:
        if dd < lgdd * 0.8:
            add("TARGETS", f"{dfn_team} takes away deep: {_pct(dd)} of targets 20+ air yds (lg {_pct(lgdd)}), "
                           f"aDOT allowed {_v(dfn, 'adot') or 0:.1f} → underneath/YAC over air-yards; longest-rec unders.", "down")
        elif dd > lgdd * 1.2:
            add("TARGETS", f"{dfn_team} gives up deep shots: {_pct(dd)} of targets 20+ air yds (lg {_pct(lgdd)}) "
                           f"→ longest-reception / big-play WR overs.", "up")

    # 6. Run game direction vs defense
    lanes = [(loc, _v(off, f"run_{loc}"), _v(dfn, f"ypc_{loc}"), _lg(dfn, f"ypc_{loc}")) for loc in ("left", "middle", "right")]
    lanes = [x for x in lanes if None not in x[1:]]
    if lanes:
        best = max(lanes, key=lambda x: x[2] - x[3])
        worst = min(lanes, key=lambda x: x[2] - x[3])
        if best[2] - best[3] > 0.7:
            add("RUSHING", f"{dfn_team} leaks {best[2]:.1f} YPC to the {best[0]} (lg {best[3]:.1f}); "
                           f"{off_team} runs {best[0]} {_pct(best[1])} of the time.", "up")
        if worst[2] - worst[3] < -0.7 and worst[1] > 0.3:
            add("RUSHING", f"{off_team}'s favorite lane ({worst[0]}, {_pct(worst[1])} of runs) is {dfn_team}'s strength "
                           f"({worst[2]:.1f} YPC allowed vs lg {worst[3]:.1f}).", "down")

    # 7. Pass funnel vs run funnel (do offenses throw more than expected vs this D?)
    proe_f = _v(dfn, "proe")
    if proe_f is not None and abs(proe_f) >= 3:
        add("SCRIPT", f"Opponents pass {proe_f:+.1f} pts over expected vs {dfn_team} → "
                      + ("pass funnel: QB/WR volume up, RB rush volume down." if proe_f > 0
                         else "run funnel: offenses lean on the ground vs this D (RB carries up, pass attempts down)."),
            "up" if proe_f > 0 else None)
    return reads


def warm(season: int | None = None) -> None:
    """Build this season's and last season's tables so game pages don't wait."""
    season = season or nd.current_season()
    for s in (season, season - 1):
        offense_table(s)
        defense_table(s)
    coverage_table(season - 1)


def matchup_tendencies(away: str, home: str, spread: dict | None = None, total: dict | None = None,
                       season: int | None = None) -> dict:
    season = season or nd.current_season()
    a, h = team_tendencies(away, season), team_tendencies(home, season)
    implied = _implied_points(spread, total, away, home)
    return {
        "season": season,
        "prior_season": season - 1,
        "metrics": [{"group": g, "key": k, "label": l, "fmt": f, "off_better": ob, "def_better": db}
                    for g, k, l, f, ob, db in METRICS],
        "implied": implied,
        "teams": {away: a, home: h},
        "sides": [
            {"offense": away, "defense": home, "reads": _script_read(away, a["off"], home, h["def"], implied)},
            {"offense": home, "defense": away, "reads": _script_read(home, h["off"], away, a["def"], implied)},
        ],
    }
