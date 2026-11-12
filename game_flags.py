"""
Outlier flags: games whose box score shouldn't be read as a trend.

A rain game where a team doesn't complete a pass, a backup QB playing three
quarters, a 38-point blowout spent in garbage time, a five-turnover day: the
play-by-play of games like these says more about the conditions than about the
teams, and averaging them into a season read misleads. This module flags them
from the play-by-play alone, so the post-game reviewer can tag what it writes
into the notebooks and the preview writer knows which games skew each team's
season numbers.

Three kinds of evidence:
  conditions  rain / snow / strong wind / cold from nflverse's weather text
              (outdoor and open-roof games only)
  personnel   the team's usual QB threw few of its passes (injury, benching)
  game state  garbage time, turnover avalanches
  stat        a team-game far outside both the league's range this season and
              the team's own other games (pass rate, attempts, completion %,
              yards per dropback, total plays)

Every flag says which team it's about and why; a game with a conditions flag
and an extreme stat is "major" (the stat is probably the weather, not the team).
"""

from __future__ import annotations

import re

import pandas as pd

WIND_MPH = 18           # sustained wind at which deep passing and kicking suffer
COLD_F = 20
GARBAGE_SHARE = 0.40    # share of a team's snaps with win probability < 5% or > 95%
TURNOVERS = 4
LEAGUE_TAIL = 0.02      # bottom/top 2% of all team-games this season
OWN_Z = 2.0             # and this many SDs from the team's other games
MIN_OWN_GAMES = 3

# metric -> (label, which tails are notable, sd floor so a steady team isn't flagged for noise)
METRICS = {
    "dropback_rate": ("dropback rate", "both", 0.05),
    "pass_att": ("pass attempts", "both", 4.0),
    "cmp_pct": ("completion %", "low", 0.06),
    "yds_per_dropback": ("yards per dropback", "both", 1.0),
    "plays": ("offensive plays", "low", 5.0),
}

_PRECIP = re.compile(r"\b(rain|showers?|snow|sleet|storm|thunder|downpour|drizzle|flurr|hurricane)", re.I)
_HEAVY = re.compile(r"\b(heavy|steady|downpour|storm|thunder|hurricane|snow|sleet)", re.I)
_WIND = re.compile(r"wind:?\s*(?:[NSEW]{1,3}\s*)?(\d+)\s*mph", re.I)
_TEMP = re.compile(r"temp:?\s*(-?\d+)", re.I)


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else f


def conditions(g: pd.DataFrame, row: pd.Series | None = None) -> dict:
    """Weather facts for one game's plays (dome/closed games come back empty)."""
    roof = str(g["roof"].dropna().iloc[0]) if "roof" in g and g["roof"].notna().any() else \
        str((row.get("roof") if row is not None else "") or "")
    if roof in ("dome", "closed"):
        return {"roof": roof}
    text = str(g["weather"].dropna().iloc[0]) if "weather" in g and g["weather"].notna().any() else ""
    head = re.split(r"temp", text, flags=re.I)[0]
    wind = _num(row.get("wind")) if row is not None else None
    temp = _num(row.get("temp")) if row is not None else None
    if wind is None and (m := _WIND.search(text)):
        wind = float(m.group(1))
    if temp is None and (m := _TEMP.search(text)):
        temp = float(m.group(1))
    precip = _PRECIP.search(head)
    return {"roof": roof or "outdoors", "weather": text.strip() or None, "wind_mph": wind, "temp_f": temp,
            "precip": precip.group(1).lower() if precip else None, "heavy": bool(precip and _HEAVY.search(head))}


def team_games(pbp: pd.DataFrame) -> pd.DataFrame:
    """One row per (game_id, posteam) with the metrics in METRICS."""
    s = pbp[pbp["play_type"].isin(["pass", "run"])]
    if s.empty:
        return pd.DataFrame(columns=["game_id", "posteam", *METRICS])
    db = s["pass"] == 1
    att = s["pass_attempt"] == 1
    s = s.assign(db=db.astype(int), att=att.astype(int), cmp=(att & (s["complete_pass"] == 1)).astype(int),
                 db_yds=s["yards_gained"].fillna(0).where(db, 0))
    t = s.groupby(["game_id", "posteam"]).agg(plays=("play_type", "size"), dbs=("db", "sum"), pass_att=("att", "sum"),
                                               cmp=("cmp", "sum"), db_yds=("db_yds", "sum")).reset_index()
    t["dropback_rate"] = t["dbs"] / t["plays"]
    t["cmp_pct"] = (t["cmp"] / t["pass_att"]).where(t["pass_att"] >= 5)
    t["yds_per_dropback"] = (t["db_yds"] / t["dbs"]).where(t["dbs"] >= 5)
    return t


def _stat_flags(tg: pd.DataFrame, game_id: str, team: str) -> list[dict]:
    me = tg[(tg["game_id"] == game_id) & (tg["posteam"] == team)]
    if me.empty:
        return []
    me = me.iloc[0]
    own = tg[(tg["posteam"] == team) & (tg["game_id"] != game_id)]
    out = []
    for key, (label, tails, floor) in METRICS.items():
        v = me[key]
        col = tg[key].dropna()
        if pd.isna(v) or len(col) < 40 or own[key].notna().sum() < MIN_OWN_GAMES:
            continue
        pct = (col < v).mean() + 0.5 * (col == v).mean()
        mu, sd = own[key].mean(), max(own[key].std(ddof=1) or 0, floor)
        z = (v - mu) / sd
        low = pct <= LEAGUE_TAIL and z <= -OWN_Z
        high = pct >= 1 - LEAGUE_TAIL and z >= OWN_Z and tails == "both"
        if low or high:
            fmt = (lambda x: f"{x:.0%}") if key in ("dropback_rate", "cmp_pct") else \
                (lambda x: f"{x:.1f}") if key == "yds_per_dropback" else (lambda x: f"{x:.0f}")
            tail = 100 * (pct if low else 1 - pct)
            out.append({"team": team, "kind": "stat", "metric": key,
                        "detail": f"{label} {fmt(v)} vs {fmt(mu)} in its other games "
                                  f"({'lowest' if low else 'highest'} {tail:.1f}% of team-games this season)"})
    return out


def _qb_flag(pbp: pd.DataFrame, g: pd.DataFrame, team: str) -> dict | None:
    """The QB who led the team's previous game threw under 60% of this one's
    passes: an injury, a benching, or the first start of a change."""
    gid = g["game_id"].iloc[0]
    earlier = sorted(x for x in pbp.loc[pbp["posteam"] == team, "game_id"].dropna().unique() if x < gid)
    if not earlier:
        return None
    prev = pbp[(pbp["game_id"] == earlier[-1]) & (pbp["posteam"] == team) & (pbp["pass_attempt"] == 1)]
    game_att = g[(g["posteam"] == team) & (g["pass_attempt"] == 1)].groupby("passer_player_name").size()
    if prev.empty or game_att.sum() < 10:
        return None
    usual = prev.groupby("passer_player_name").size().idxmax()
    share = game_att.get(usual, 0) / game_att.sum()
    if share >= 0.6:
        return None
    others = ", ".join(f"{n} {int(a)} att" for n, a in game_att.sort_values(ascending=False).items() if n != usual)
    return {"team": team, "kind": "personnel",
            "detail": f"last game's QB {usual} threw {share:.0%} of passes ({others})"}


def flags_for_game(pbp: pd.DataFrame, game_id: str, row: pd.Series | None = None,
                   tg: pd.DataFrame | None = None) -> dict:
    """{"severity": none|minor|major, "conditions": {...}, "flags": [...]} for one game.
    Teams are nflverse codes."""
    g = pbp[pbp["game_id"] == game_id]
    if g.empty:
        return {"severity": "none", "flags": []}
    tg = team_games(pbp) if tg is None else tg
    cond = conditions(g, row)
    flags: list[dict] = []
    wx = []
    if cond.get("precip"):
        wx.append(("heavy " if cond["heavy"] else "") + cond["precip"])
    if (cond.get("wind_mph") or 0) >= WIND_MPH:
        wx.append(f"wind {cond['wind_mph']:.0f} mph")
    if cond.get("temp_f") is not None and cond["temp_f"] <= COLD_F:
        wx.append(f"{cond['temp_f']:.0f}°F")
    if wx:
        flags.append({"team": None, "kind": "conditions", "detail": ", ".join(wx) + f" ({cond.get('weather') or 'schedule data'})"})
    for team in g["posteam"].dropna().unique():
        flags += _stat_flags(tg, game_id, team)
        if (q := _qb_flag(pbp, g, team)):
            flags.append(q)
        s = g[(g["posteam"] == team) & g["play_type"].isin(["pass", "run"]) & g["wp"].notna()]
        if len(s) >= 30:
            garbage = ((s["wp"] < 0.05) | (s["wp"] > 0.95)).mean()
            if garbage >= GARBAGE_SHARE:
                flags.append({"team": team, "kind": "game_state",
                              "detail": f"{garbage:.0%} of snaps with the game decided (win prob <5% or >95%)"})
        to = int(g.loc[g["posteam"] == team, "interception"].fillna(0).sum()
                 + g.loc[g["posteam"] == team, "fumble_lost"].fillna(0).sum())
        if to >= TURNOVERS:
            flags.append({"team": team, "kind": "game_state", "detail": f"{to} turnovers"})
    return {"severity": severity(flags), "conditions": cond, "flags": flags}


def severity(flags: list[dict]) -> str:
    """major: bad conditions plus a stat extreme (the stat is probably the
    weather), or three or more flags; minor: anything else flagged."""
    wx = any(f["kind"] == "conditions" for f in flags)
    stat = any(f["kind"] == "stat" for f in flags)
    return "major" if (wx and stat) or len(flags) >= 3 else "minor" if flags else "none"


def season_flags(pbp: pd.DataFrame, sched: pd.DataFrame | None = None) -> dict[str, dict]:
    """game_id -> flags for every finished game in pbp."""
    tg = team_games(pbp)
    rows = {r["game_id"]: r for _, r in sched.iterrows()} if sched is not None else {}
    return {gid: flags_for_game(pbp, gid, rows.get(gid), tg) for gid in pbp["game_id"].dropna().unique()}


def team_outliers(pbp: pd.DataFrame, sched: pd.DataFrame, team: str, to_code=lambda t: t) -> list[dict]:
    """This season's flagged games for `team` (nflverse code), for the preview writer:
    [{week, opp, severity, flags: [str]}], flags limited to ones about this team or the conditions."""
    rows = sched[((sched["home_team"] == team) | (sched["away_team"] == team)) & sched["result"].notna()]
    tg = team_games(pbp)
    out = []
    for _, r in rows.sort_values("week").iterrows():
        f = flags_for_game(pbp, r["game_id"], r, tg)
        own = [x for x in f["flags"] if x["team"] in (None, team)]
        mine = [x["detail"] if x["team"] is None else f"{to_code(x['team'])}: {x['detail']}" for x in own]
        if mine:
            home = r["home_team"] == team
            out.append({"week": int(r["week"]), "opp": ("vs " if home else "@ ") + to_code(r["away_team"] if home else r["home_team"]),
                        "severity": severity(own), "flags": mine})
    return out
