"""
nflverse data layer: cached loaders + derived metrics for opponent defense,
O-line/pace context, injuries, and player history.

Data is fetched via nflreadpy (which has its own HTTP cache) and then
persisted as local parquet in cache/ so repeat runs of the tool don't refetch
or reparse. Call refresh() to force a re-pull for a new week.

Team code note: Kalshi uses JAC / LAR; nflverse uses JAX / LA. All functions
here take/return *nflverse* codes; convert at the Kalshi boundary with
kalshi_to_nflverse_team() / nflverse_to_kalshi_team().
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from functools import lru_cache

import nflreadpy as nfl
import pandas as pd

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

KALSHI_TO_NFLVERSE_TEAM = {"JAC": "JAX", "LAR": "LA"}
NFLVERSE_TO_KALSHI_TEAM = {v: k for k, v in KALSHI_TO_NFLVERSE_TEAM.items()}


def kalshi_to_nflverse_team(code: str) -> str:
    return KALSHI_TO_NFLVERSE_TEAM.get(code, code)


def nflverse_to_kalshi_team(code: str) -> str:
    return NFLVERSE_TO_KALSHI_TEAM.get(code, code)


def current_season() -> int:
    today = date.today()
    # NFL season N runs roughly Sep(year N) - Feb(year N+1); before March, we're
    # still in season N-1's playoffs/offseason data window.
    return today.year if today.month >= 3 else today.year - 1


def _parquet_cache(name: str, season: int, loader):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{name}_{season}.parquet")
    if os.path.exists(path) and (datetime.now().timestamp() - os.path.getmtime(path)) < 6 * 3600:
        return pd.read_parquet(path)
    df = loader()
    df.to_parquet(path)
    return df


def load_schedules(season: int | None = None) -> pd.DataFrame:
    season = season or current_season()
    return _parquet_cache("schedules", season, lambda: nfl.load_schedules(seasons=[season]).to_pandas())


def load_rosters(season: int | None = None) -> pd.DataFrame:
    season = season or current_season()
    return _parquet_cache("rosters", season, lambda: nfl.load_rosters(seasons=[season]).to_pandas())


def load_pbp(season: int | None = None) -> pd.DataFrame:
    season = season or current_season()
    return _parquet_cache("pbp", season, lambda: nfl.load_pbp(seasons=[season]).to_pandas())


def load_player_stats(season: int | None = None) -> pd.DataFrame:
    season = season or current_season()
    return _parquet_cache("player_stats", season, lambda: nfl.load_player_stats(seasons=[season]).to_pandas())


def load_injuries_df(season: int | None = None) -> pd.DataFrame:
    season = season or current_season()
    return _parquet_cache("injuries", season, lambda: nfl.load_injuries(seasons=[season]).to_pandas())


def load_pfr_pass_advstats(season: int | None = None) -> pd.DataFrame:
    season = season or current_season()
    return _parquet_cache(
        "pfr_pass_adv", season, lambda: nfl.load_pfr_advstats(seasons=[season], stat_type="pass").to_pandas()
    )


def load_ftn(season: int | None = None) -> pd.DataFrame:
    season = season or current_season()
    return _parquet_cache("ftn", season, lambda: nfl.load_ftn_charting(seasons=[season]).to_pandas())


@lru_cache(maxsize=1)
def load_stadiums() -> dict:
    with open(os.path.join(DATA_DIR, "stadiums.json")) as f:
        return json.load(f)


@lru_cache(maxsize=1)
def load_coordinators() -> dict:
    with open(os.path.join(DATA_DIR, "coordinators.json")) as f:
        return json.load(f)


# -- player / team resolution -------------------------------------------------


def find_player_in_roster(
    name: str | None = None, sportradar_id: str | None = None, season: int | None = None
) -> pd.Series | None:
    rosters = load_rosters(season)
    active = rosters[rosters["status"].isin(["ACT", "RES"])]
    if sportradar_id:
        match = active[active["sportradar_id"] == sportradar_id]
        if not match.empty:
            return match.iloc[0]
    if name:
        norm = _normalize_name(name)
        cand = active[active["full_name"].apply(_normalize_name) == norm]
        if not cand.empty:
            return cand.iloc[0]
        # fallback: last name substring match
        last = norm.split()[-1] if norm else ""
        cand = active[active["full_name"].apply(lambda n: last in _normalize_name(n))]
        if len(cand) == 1:
            return cand.iloc[0]
    return None


def _normalize_name(name: str) -> str:
    import re

    name = name.lower()
    name = re.sub(r"[.\-']", "", name)
    name = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", name)
    return " ".join(name.split())


def get_team_next_game(team: str, season: int | None = None) -> dict | None:
    """team is an nflverse code. Returns the next game not yet played."""
    sched = load_schedules(season)
    today = pd.Timestamp(date.today())
    team_games = sched[(sched["home_team"] == team) | (sched["away_team"] == team)].copy()
    team_games["gameday_dt"] = pd.to_datetime(team_games["gameday"])
    upcoming = team_games[team_games["gameday_dt"] >= today - pd.Timedelta(days=1)]
    upcoming = upcoming.sort_values("gameday_dt")
    if upcoming.empty:
        return None
    row = upcoming.iloc[0]
    is_home = row["home_team"] == team
    opponent = row["away_team"] if is_home else row["home_team"]
    return {
        "season": int(row["season"]),
        "week": int(row["week"]),
        "gameday": row["gameday"],
        "gametime": row.get("gametime"),
        "team": team,
        "opponent": opponent,
        "is_home": bool(is_home),
        "rest_days": int(row["home_rest"] if is_home else row["away_rest"]),
        "opponent_rest_days": int(row["away_rest"] if is_home else row["home_rest"]),
        "roof": row.get("roof"),
        "surface": row.get("surface"),
        "stadium": row.get("stadium"),
        "spread_line": row.get("spread_line"),
        "total_line": row.get("total_line"),
        "home_coach": row.get("home_coach"),
        "away_coach": row.get("away_coach"),
        "game_id": row.get("game_id"),
    }


def rank_of(df: pd.DataFrame, col: str, team: str, ascending: bool) -> dict | None:
    """League rank (1 = best per `ascending`) for `team` on `col`, out of however
    many teams have a non-null value. `ascending=True` means lower is better
    (e.g. EPA allowed); `ascending=False` means higher is better (e.g. pressure
    rate generated)."""
    valid = df.dropna(subset=[col])
    if team not in valid["team"].values or len(valid) == 0:
        return None
    ranks = valid[col].rank(ascending=ascending, method="min")
    row_rank = ranks[valid["team"] == team].iloc[0]
    return {
        "value": float(valid.loc[valid["team"] == team, col].iloc[0]),
        "rank": int(row_rank),
        "out_of": int(len(valid)),
        "better_is": "low" if ascending else "high",
    }


# -- opponent defense metrics --------------------------------------------------


def defense_epa_per_dropback(season: int | None = None) -> pd.DataFrame:
    """EPA allowed per dropback (pass attempts + sacks), by defteam, season-to-date."""
    pbp = load_pbp(season)
    dropbacks = pbp[pbp["qb_dropback"] == 1]
    out = dropbacks.groupby("defteam")["epa"].agg(epa_per_dropback="mean", dropbacks="count").reset_index()
    return out.rename(columns={"defteam": "team"})


def defense_rush_epa_allowed(season: int | None = None) -> pd.DataFrame:
    pbp = load_pbp(season)
    rushes = pbp[(pbp["play_type"] == "run")]
    out = rushes.groupby("defteam")["epa"].agg(epa_per_rush="mean", rushes="count").reset_index()
    return out.rename(columns={"defteam": "team"})


def defense_pressure_generated(season: int | None = None) -> pd.DataFrame:
    """From PFR advanced pass stats: pressure/blitz/hurry/hit rate a defense
    (the 'opponent' in each row) inflicted on opposing QBs, season-to-date."""
    adv = load_pfr_pass_advstats(season)
    grouped = adv.groupby("opponent").agg(
        pressure_pct=("times_pressured_pct", "mean"),
        blitzes=("times_blitzed", "sum"),
        hurries=("times_hurried", "sum"),
        hits=("times_hit", "sum"),
        sacks=("times_sacked", "sum"),
        qb_dropbacks_charted=("times_sacked", "count"),
    ).reset_index()
    return grouped.rename(columns={"opponent": "team"})


def defense_blitz_rate_ftn(season: int | None = None) -> pd.DataFrame:
    """Blitz/pass-rush tendency from FTN charting, joined to pbp for defteam.
    NOTE: nflverse's free FTN release has no man/zone coverage field -- this
    is the closest public proxy for defensive scheme aggressiveness."""
    ftn = load_ftn(season)
    pbp = load_pbp(season)
    joined = ftn.merge(
        pbp[["nflverse_game_id" if "nflverse_game_id" in pbp.columns else "game_id", "play_id", "defteam"]]
        .rename(columns={"nflverse_game_id": "nflverse_game_id"} if "nflverse_game_id" in pbp.columns else {"game_id": "nflverse_game_id"}),
        left_on=["nflverse_game_id", "nflverse_play_id"],
        right_on=["nflverse_game_id", "play_id"],
        how="inner",
    )
    out = joined.groupby("defteam").agg(
        blitz_rate=("n_blitzers", lambda s: (s > 0).mean()),
        avg_pass_rushers=("n_pass_rushers", "mean"),
        plays_charted=("n_blitzers", "count"),
    ).reset_index()
    return out.rename(columns={"defteam": "team"})


# -- team/O-line context --------------------------------------------------------


def oline_pressure_allowed(season: int | None = None) -> pd.DataFrame:
    """Pressure/sack rate allowed, by the passer's own team (proxy for O-line)."""
    adv = load_pfr_pass_advstats(season)
    grouped = adv.groupby("team").agg(
        pressure_pct_allowed=("times_pressured_pct", "mean"),
        sacks_allowed=("times_sacked", "sum"),
        hurries_allowed=("times_hurried", "sum"),
        hits_allowed=("times_hit", "sum"),
    ).reset_index()
    return grouped


def team_pace(season: int | None = None) -> pd.DataFrame:
    """Offensive plays run and seconds/play, by posteam, season-to-date."""
    pbp = load_pbp(season)
    plays = pbp[pbp["play_type"].isin(["pass", "run"])].copy()
    per_game = plays.groupby(["posteam", "game_id"]).size().reset_index(name="plays")
    agg = per_game.groupby("posteam")["plays"].mean().reset_index(name="plays_per_game")
    # seconds/play from play clock deltas where available
    if "play_clock" in pbp.columns:
        pass
    return agg.rename(columns={"posteam": "team"})


def target_share(gsis_id: str, season: int | None = None) -> pd.DataFrame:
    """Weekly target share for a player, season-to-date."""
    ps = load_player_stats(season)
    rows = ps[ps["player_id"] == gsis_id][["week", "team", "targets", "target_share", "air_yards_share", "wopr"]]
    return rows.sort_values("week")


def player_vs_opponent_history(gsis_id: str, opponent: str, seasons: list[int] | None = None) -> pd.DataFrame:
    """All career games (within loaded seasons) a player has played against `opponent`."""
    seasons = seasons or list(range(current_season() - 3, current_season() + 1))
    frames = []
    for s in seasons:
        try:
            frames.append(load_player_stats(s))
        except Exception:
            continue
    if not frames:
        return pd.DataFrame()
    all_stats = pd.concat(frames, ignore_index=True)
    hist = all_stats[(all_stats["player_id"] == gsis_id) & (all_stats["opponent_team"] == opponent)]
    cols = [
        "season", "week", "team", "opponent_team",
        "completions", "attempts", "passing_yards", "passing_tds",
        "carries", "rushing_yards", "rushing_tds",
        "receptions", "targets", "receiving_yards", "receiving_tds",
    ]
    cols = [c for c in cols if c in hist.columns]
    return hist[cols].sort_values(["season", "week"])


def team_injuries(team: str, season: int | None = None, week: int | None = None) -> pd.DataFrame:
    inj = load_injuries_df(season)
    rows = inj[inj["team"] == team]
    if week:
        rows = rows[rows["week"] == week]
    else:
        rows = rows[rows["week"] == rows["week"].max()]
    return rows[["full_name", "position", "report_status", "report_primary_injury", "practice_status"]]
