"""
nflverse data layer: cached loaders + derived metrics for opponent defense,
O-line/pace context, injuries, and player history.

Data is fetched via nflreadpy (which has its own HTTP cache) and then
persisted as local parquet in cache/ (and held in memory) until new games
finish -- see the cache section below.

Team code note: Kalshi uses JAC / LAR; nflverse uses JAX / LA. All functions
here take/return *nflverse* codes; convert at the Kalshi boundary with
kalshi_to_nflverse_team() / nflverse_to_kalshi_team().
"""

from __future__ import annotations

import bisect
import functools
import json
import os
import threading
import time
from datetime import date, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

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


# -- cache ---------------------------------------------------------------------
#
# Every dataset lives on disk as cache/<name>_<season>.parquet and in memory
# once read. Staleness is driven by the schedule rather than a clock: game
# data (pbp, box scores, PFR/FTN charting, snaps) only changes when games
# finish, so it's refetched once after each game day's last kickoff +
# GAME_DONE_DELAY (results in) and once more at +GAME_SETTLED_DELAY (nflverse's
# overnight rebuilds and stat corrections). Finished seasons never refetch.
# Datasets that move mid-week (betting lines, rosters, injury reports, expert
# rankings) also carry a TTL.

GAME_DONE_DELAY = 4 * 3600
GAME_SETTLED_DELAY = 28 * 3600
GAME_DATASETS = {"schedules", "pbp", "player_stats", "pfr_pass_adv", "ftn", "pfr_def_week", "snap_counts",
                 "ff_rankings_week"}
TTL_SECONDS = {"schedules": 6 * 3600, "rosters": 12 * 3600, "injuries": 6 * 3600, "ff_rankings_week": 24 * 3600}
DEFAULT_TTL = 6 * 3600
ET = ZoneInfo("America/New_York")

_cache_lock = threading.Lock()
_key_locks: dict[tuple[str, int], threading.Lock] = {}
_frames: dict[tuple[str, int], tuple[float, pd.DataFrame]] = {}  # (name, season) -> (written_ts, df)
_checkpoints: dict[int, tuple[float, list[float]]] = {}  # season -> (schedule written_ts, refresh times)


def _path(name: str, season: int) -> str:
    return os.path.join(CACHE_DIR, f"{name}_{season}.parquet")


def _peek(name: str, season: int) -> tuple[float, pd.DataFrame] | None:
    """Cached (written_ts, frame) without any freshness check: memory, then disk."""
    hit = _frames.get((name, season))
    if hit:
        return hit
    path = _path(name, season)
    if os.path.exists(path):
        hit = (os.path.getmtime(path), pd.read_parquet(path))
        _frames[(name, season)] = hit
        return hit
    return None


def _game_refresh_times(season: int) -> list[float]:
    """Sorted timestamps after which `season`'s game data goes stale: each
    game day's last kickoff + the done/settled delays."""
    sched = _peek("schedules", season)
    if sched is None:
        return []
    cached = _checkpoints.get(season)
    if cached and cached[0] == sched[0]:
        return cached[1]
    last_kick: dict[str, float] = {}
    for gameday, gametime in sched[1][["gameday", "gametime"]].itertuples(index=False):
        if not isinstance(gameday, str):
            continue
        clock = gametime if isinstance(gametime, str) and gametime else "20:15"
        try:
            kick = datetime.strptime(f"{gameday} {clock}", "%Y-%m-%d %H:%M").replace(tzinfo=ET).timestamp()
        except ValueError:
            continue
        last_kick[gameday] = max(last_kick.get(gameday, 0.0), kick)
    times = sorted(k + d for k in last_kick.values() for d in (GAME_DONE_DELAY, GAME_SETTLED_DELAY))
    _checkpoints[season] = (sched[0], times)
    return times


def _is_fresh(name: str, season: int, written: float, now: float) -> bool:
    if season < current_season():
        return True
    ttl = TTL_SECONDS.get(name, None if name in GAME_DATASETS else DEFAULT_TTL)
    if ttl is not None and now - written > ttl:
        return False
    if name in GAME_DATASETS:
        # stale if a refresh point fell between the write and now
        times = _game_refresh_times(season)
        i = bisect.bisect_right(times, written)
        return i >= len(times) or times[i] > now
    return True


def _parquet_cache(name: str, season: int, loader):
    with _cache_lock:
        lock = _key_locks.setdefault((name, season), threading.Lock())
    with lock:
        now = time.time()
        hit = _peek(name, season)
        if hit and _is_fresh(name, season, hit[0], now):
            return hit[1]
        try:
            df = loader()
        except Exception:
            if hit:  # nflverse unreachable: serve stale rather than fail
                return hit[1]
            raise
        os.makedirs(CACHE_DIR, exist_ok=True)
        df.to_parquet(_path(name, season))
        _frames[(name, season)] = (now, df)
        return df


def memo_on_data(*deps: str):
    """Memoize a derived `fn(season=None)` table in memory until one of the
    `deps` datasets it's built from is refetched."""
    def deco(fn):
        memo: dict[int, tuple[tuple, object]] = {}
        lock = threading.Lock()

        @functools.wraps(fn)
        def wrapper(season: int | None = None):
            season = season or current_season()
            with lock:
                for d in deps:
                    _LOADERS[d](season)  # refetch if stale
                version = tuple(_frames[(d, season)][0] for d in deps)
                hit = memo.get(season)
                if hit and hit[0] == version:
                    return hit[1]
                result = fn(season)
                memo[season] = (version, result)
                return result
        return wrapper
    return deco


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


def load_pfr_def_week(season: int | None = None) -> pd.DataFrame:
    season = season or current_season()
    return _parquet_cache("pfr_def_week", season, lambda: nfl.load_pfr_advstats(
        seasons=[season], stat_type="def", summary_level="week").to_pandas())


def load_snap_counts(season: int | None = None) -> pd.DataFrame:
    season = season or current_season()
    return _parquet_cache("snap_counts", season, lambda: nfl.load_snap_counts(seasons=[season]).to_pandas())


def load_ftn(season: int | None = None) -> pd.DataFrame:
    season = season or current_season()
    return _parquet_cache("ftn", season, lambda: nfl.load_ftn_charting(seasons=[season]).to_pandas())


def load_participation(season: int | None = None) -> pd.DataFrame:
    """NGS/FTN participation (formation, personnel, coverage shell, man/zone).
    nflverse only publishes it after a season ends, so the current season
    raises upstream -- callers use the prior season."""
    season = season or current_season()
    return _parquet_cache("participation", season, lambda: nfl.load_participation(seasons=[season]).to_pandas())


def load_ff_rankings_week(season: int | None = None) -> pd.DataFrame:
    """FantasyPros weekly ECR. Not season-scoped upstream; keyed by the
    current season so it expires alongside the rest of the game data."""
    season = season or current_season()
    return _parquet_cache("ff_rankings_week", season, lambda: nfl.load_ff_rankings(type="week").to_pandas())


_LOADERS = {
    "schedules": load_schedules, "rosters": load_rosters, "pbp": load_pbp, "player_stats": load_player_stats,
    "injuries": load_injuries_df, "pfr_pass_adv": load_pfr_pass_advstats, "pfr_def_week": load_pfr_def_week,
    "snap_counts": load_snap_counts, "ftn": load_ftn, "ff_rankings_week": load_ff_rankings_week,
}


def warm(season: int | None = None) -> None:
    """Load (refetching if stale) every current-season dataset and derived
    table, so page requests are served from memory."""
    season = season or current_season()
    for name, loader in _LOADERS.items():
        try:
            loader(season)
        except Exception as e:
            print(f"cache warm: {name}_{season} failed: {e}")
    for fn in (defense_epa_per_dropback, defense_rush_epa_allowed, defense_pressure_generated,
               defense_blitz_rate_ftn, oline_pressure_allowed, team_pace, team_records):
        try:
            fn(season)
        except Exception as e:
            print(f"cache warm: {fn.__name__} failed: {e}")


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


@memo_on_data("pbp")
def defense_epa_per_dropback(season: int | None = None) -> pd.DataFrame:
    """EPA allowed per dropback (pass attempts + sacks), by defteam, season-to-date."""
    pbp = load_pbp(season)
    dropbacks = pbp[pbp["qb_dropback"] == 1]
    out = dropbacks.groupby("defteam")["epa"].agg(epa_per_dropback="mean", dropbacks="count").reset_index()
    return out.rename(columns={"defteam": "team"})


@memo_on_data("pbp")
def defense_rush_epa_allowed(season: int | None = None) -> pd.DataFrame:
    pbp = load_pbp(season)
    rushes = pbp[(pbp["play_type"] == "run")]
    out = rushes.groupby("defteam")["epa"].agg(epa_per_rush="mean", rushes="count").reset_index()
    return out.rename(columns={"defteam": "team"})


@memo_on_data("pfr_pass_adv")
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


@memo_on_data("ftn", "pbp")
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


@memo_on_data("pfr_pass_adv")
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


@memo_on_data("pbp")
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


GAME_LOG_STAT_COLS = [
    "completions", "attempts", "passing_yards", "passing_tds", "passing_interceptions",
    "carries", "rushing_yards", "rushing_tds",
    "targets", "receptions", "receiving_yards", "receiving_tds",
    "fantasy_points_ppr",
]


def player_log_seasons(rookie_year, max_back: int = 10) -> list[int]:
    """Seasons worth offering in a player's game log, newest first."""
    cur = current_season()
    try:
        first = int(rookie_year)
    except (TypeError, ValueError):
        first = cur - 3
    return list(range(cur, max(first, cur - max_back + 1) - 1, -1))


def _game_links(game: dict) -> dict:
    espn = game.get("espn")
    pfr = game.get("pfr")
    if espn and not pd.isna(espn):
        return {"url": f"https://www.espn.com/nfl/game/_/gameId/{int(float(espn))}", "url_source": "ESPN"}
    if pfr and not pd.isna(pfr):
        return {"url": f"https://www.pro-football-reference.com/boxscores/{pfr}.htm", "url_source": "PFR"}
    return {"url": None, "url_source": None}


def _with_game_context(rows: pd.DataFrame, seasons: list[int]) -> list[dict]:
    """Attach date, home/away, final score, result and a box-score link from
    the schedule to player-stat rows (joined on game_id)."""
    games = {}
    for s in seasons:
        try:
            sched = load_schedules(s)
        except Exception:
            continue
        for g in sched.to_dict(orient="records"):
            games[g["game_id"]] = g
    out = []
    for r in rows.to_dict(orient="records"):
        g = games.get(r.get("game_id"), {})
        team = r.get("team")
        is_home = g.get("home_team") == team if g else None
        rec = {k: r.get(k) for k in ["season", "week", "season_type", "game_id", "team", "opponent_team"] + GAME_LOG_STAT_COLS}
        rec["gameday"] = g.get("gameday")
        rec["is_home"] = is_home
        pts, opp_pts = (g.get("home_score"), g.get("away_score")) if is_home else (g.get("away_score"), g.get("home_score"))
        if pts is not None and opp_pts is not None and not pd.isna(pts) and not pd.isna(opp_pts):
            pts, opp_pts = int(pts), int(opp_pts)
            rec["score"] = f"{pts}-{opp_pts}"
            rec["result"] = "W" if pts > opp_pts else "L" if pts < opp_pts else "T"
        else:
            rec["score"] = rec["result"] = None
        rec.update(_game_links(g))
        out.append(rec)
    return out


def _player_stat_rows(gsis_id: str, seasons: list[int]) -> pd.DataFrame:
    frames = []
    for s in seasons:
        try:
            ps = load_player_stats(s)
        except Exception:
            continue
        frames.append(ps[ps["player_id"] == gsis_id])
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).sort_values(["season", "week"])


def player_game_log(gsis_id: str, season: int | None = None) -> list[dict]:
    """Every game (REG + POST) the player logged in one season, oldest first."""
    season = season or current_season()
    rows = _player_stat_rows(gsis_id, [season])
    return _with_game_context(rows, [season]) if not rows.empty else []


def player_vs_opponent_history(gsis_id: str, opponent: str, seasons: list[int] | None = None) -> list[dict]:
    """All career games (within loaded seasons) a player has played against
    `opponent`, oldest first, with date, score and a box-score link."""
    seasons = seasons or list(range(current_season() - 3, current_season() + 1))
    rows = _player_stat_rows(gsis_id, seasons)
    if rows.empty:
        return []
    hist = rows[rows["opponent_team"] == opponent]
    return _with_game_context(hist, sorted(set(int(x) for x in hist["season"]))) if not hist.empty else []


def team_injuries(team: str, season: int | None = None, week: int | None = None) -> pd.DataFrame:
    inj = load_injuries_df(season)
    rows = inj[inj["team"] == team]
    if week:
        rows = rows[rows["week"] == week]
    else:
        rows = rows[rows["week"] == rows["week"].max()]
    return rows[["full_name", "position", "report_status", "report_primary_injury",
                 "report_secondary_injury", "practice_primary_injury", "practice_status"]]


POSITION_GROUP_ORDER = {"QB": 0, "RB": 1, "WR": 2, "TE": 3, "OL": 4, "T": 4, "G": 4, "C": 4,
                         "DL": 5, "DE": 5, "DT": 5, "LB": 6, "DB": 7, "CB": 7, "S": 7,
                         "K": 8, "P": 9, "LS": 10}


def team_roster(team: str, season: int | None = None) -> list[dict]:
    """Active players for `team` (nflverse code), sorted offense-skill-position
    first (QB/RB/WR/TE) since those are what a bettor usually wants to drill
    into, then the rest of the depth chart."""
    rosters = load_rosters(season)
    rows = rosters[(rosters["team"] == team) & (rosters["status"] == "ACT")].copy()
    rows["sort_key"] = rows["position"].map(lambda p: POSITION_GROUP_ORDER.get(p, 11))
    rows = rows.sort_values(["sort_key", "jersey_number"])
    return rows[["full_name", "position", "jersey_number", "sportradar_id", "gsis_id", "pfr_id"]].to_dict(orient="records")


def all_player_names(season: int | None = None) -> list[dict]:
    """Active players league-wide for search-box autocomplete, skill positions
    first so the likely prop targets surface ahead of linemen."""
    rosters = load_rosters(season)
    rows = rosters[rosters["status"] == "ACT"].copy()
    rows["sort_key"] = rows["position"].map(lambda p: POSITION_GROUP_ORDER.get(p, 11))
    rows = rows.sort_values(["sort_key", "full_name"]).drop_duplicates("full_name")
    return [
        {"name": r.full_name, "team": nflverse_to_kalshi_team(r.team), "pos": r.position}
        for r in rows.itertuples()
    ]


@memo_on_data("schedules")
def team_records(season: int | None = None) -> dict[str, str]:
    """W-L(-T) record per nflverse team code from completed regular-season games."""
    sched = load_schedules(season)
    done = sched[(sched["game_type"] == "REG") & sched["result"].notna()]
    rec: dict[str, list[int]] = {}
    for _, g in done.iterrows():
        # result = home_score - away_score
        for team, margin in ((g["home_team"], g["result"]), (g["away_team"], -g["result"])):
            w_l_t = rec.setdefault(team, [0, 0, 0])
            w_l_t[0 if margin > 0 else 1 if margin < 0 else 2] += 1
    return {t: f"{w}-{l}-{t_}" if t_ else f"{w}-{l}" for t, (w, l, t_) in rec.items()}


def find_upcoming_game(away: str, home: str, season: int | None = None) -> dict | None:
    """Next unplayed schedule row for away@home (nflverse codes): week, gameday,
    and gametime (ET, HH:MM)."""
    sched = load_schedules(season)
    rows = sched[(sched["away_team"] == away) & (sched["home_team"] == home) & sched["result"].isna()]
    if rows.empty:
        return None
    row = rows.sort_values("gameday").iloc[0]
    return {"week": int(row["week"]), "gameday": row["gameday"], "gametime": row["gametime"]}


SEASON_PACE_STATS = [
    "passing_yards", "passing_tds", "rushing_yards", "rushing_tds",
    "receptions", "receiving_yards", "receiving_tds",
]


def player_season_pace(gsis_id: str, team: str, season: int | None = None) -> dict:
    """Regular-season-to-date totals for a player plus how many team games are
    left. Remaining games come from the team's unplayed REG schedule rows, so
    the bye week (which has no row) is excluded automatically."""
    season = season or current_season()
    sched = load_schedules(season)
    reg = sched[(sched["game_type"] == "REG") & ((sched["home_team"] == team) | (sched["away_team"] == team))]
    played = reg[reg["result"].notna()]
    remaining = reg[reg["result"].isna()]
    team_weeks = set(int(w) for w in reg["week"])
    all_weeks = set(int(w) for w in sched.loc[sched["game_type"] == "REG", "week"])
    bye_weeks = sorted(all_weeks - team_weeks)
    last_played_week = int(played["week"].max()) if not played.empty else 0

    ps = load_player_stats(season)
    rows = ps[(ps["player_id"] == gsis_id) & (ps["season_type"] == "REG")]
    totals = {c: float(rows[c].fillna(0).sum()) for c in SEASON_PACE_STATS if c in rows.columns}
    return {
        "season": season,
        "totals": totals,
        "games_played": int(len(rows)),
        "team_games_played": int(len(played)),
        "team_games_remaining": int(len(remaining)),
        "bye_week": bye_weeks[0] if bye_weeks else None,
        "bye_upcoming": bool(bye_weeks and bye_weeks[0] > last_played_week),
    }
