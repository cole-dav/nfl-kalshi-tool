"""
All-22 play replays from NFL Big Data Bowl player-tracking data (Viz Lab only:
historical seasons, never used on betting pages).

Two sources, normalized to one shape:

  "2021" -- Big Data Bowl 2023 (Kaggle; `kaggle competitions download
            nfl-big-data-bowl-2023 -p data/bdb/2023`). 2021 season Weeks 1-8,
            every dropback, all 22 players + the ball, from the snap until the
            throw or sack (that contest was about the pass rush). Comes with
            PFF scouting: who rushed / blocked, and every hit, hurry and sack.
  "2017" -- Big Data Bowl 2019 (public, no login). 2017 Weeks 1-6, all plays,
            all 22 + ball, whole play. Only one game's tracking is still on the
            NFL repo's master branch; the other 90 are fetched from the repo's
            history (commit before they were dropped) the first time a game is
            opened (~30 MB each).

Raw CSVs are converted once per game to cache/bdb/<source>/<gameId>.parquet
(the 2021 weekly files are split all at once on first touch of a week).

Per play this returns positions for every player and the ball, plus metrics
from the dots:

  * pressure   -- "Continuous Pocket Pressure"-style index after the 2023
                  winners: each defender within 7 yd of the QB adds (1 - d/7),
                  scaled by up to 2x for closing speed. On 2021 plays it's
                  shown next to PFF's actual hits/hurries/sacks, and
                  tools/bdb_precompute.py measures how well it separates
                  pressured from clean dropbacks.
  * separation -- per eligible receiver, distance to the nearest coverage
                  defender.
  * catchProb  -- per receiver per frame, "if thrown now" completion
                  probability from completion_model.py (trained on BDB 2026).

Coordinates are the raw tracking frame: x 0-120 yd along the field
(end zones included), y 0-53.3 yd across.
"""

from __future__ import annotations

import glob
import json
import os
import re
import threading
from functools import lru_cache

import numpy as np
import pandas as pd
import requests

import completion_model as cm

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data", "bdb")
CACHE_DIR = os.path.join(ROOT, "cache", "bdb")
DIR_2021 = os.path.join(DATA_DIR, "2023")

GH = "https://raw.githubusercontent.com/nfl-football-ops/Big-Data-Bowl/"
GH_MASTER = GH + "master/Data/"
GH_HISTORY = GH + "c38c1aaadcc2fb50f3adad6466a6ad4641e902d8/Data/"  # last commit with all 91 games
META_2017 = ("games.csv", "plays.csv", "players.csv")

PRESSURE_RADIUS = 7.0     # yd: defenders farther than this add nothing
CLOSING_FULL = 8.0        # yd/s of closing speed that doubles a defender's weight
ELIGIBLE = {"WR", "TE", "RB", "FB", "HB"}
PASS_RESULTS = {"C", "I", "S", "IN", "R"}  # complete, incomplete, sack, int, scramble
FPS = 10

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _nan(v):
    if v is None or (isinstance(v, float) and np.isnan(v)) or v is pd.NA or v is pd.NaT:
        return None
    return v.item() if isinstance(v, np.generic) else v


def _download(url: str, path: str) -> None:
    r = requests.get(url, timeout=300)
    r.raise_for_status()
    with open(path + ".part", "wb") as f:
        f.write(r.content)
    os.replace(path + ".part", path)


# -- sources -------------------------------------------------------------------


def _has_2021() -> bool:
    return all(os.path.exists(os.path.join(DIR_2021, f)) for f in ("games.csv", "plays.csv", "players.csv"))


@lru_cache(maxsize=1)
def _meta_2017() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    with _lock("meta2017"):
        os.makedirs(DATA_DIR, exist_ok=True)
        for name in META_2017:
            path = os.path.join(DATA_DIR, name)
            if not os.path.exists(path):
                _download(GH_MASTER + name, path)
    g = pd.read_csv(os.path.join(DATA_DIR, "games.csv"))
    p = pd.read_csv(os.path.join(DATA_DIR, "plays.csv"))
    pl = pd.read_csv(os.path.join(DATA_DIR, "players.csv"))
    return g, p, pl


@lru_cache(maxsize=1)
def _meta_2021() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    g = pd.read_csv(os.path.join(DIR_2021, "games.csv"))
    p = pd.read_csv(os.path.join(DIR_2021, "plays.csv"))
    pl = pd.read_csv(os.path.join(DIR_2021, "players.csv"))
    pff = pd.read_csv(os.path.join(DIR_2021, "pffScoutingData.csv"))
    return g, p, pl, pff


def sources() -> list[dict]:
    out = []
    if _has_2021():
        out.append({"id": "2021", "season": 2021, "label": "2021 · Wk 1-8",
                    "note": "BDB 2023 · 2021 Wk 1-8 · every dropback · all 22 + ball · snap to throw/sack · PFF pressure charting"})
    out.append({"id": "2017", "season": 2017, "label": "2017 · Wk 1-6",
                "note": "BDB 2019 · 2017 Wk 1-6 · every play · all 22 + ball · whole play"})
    return out


def _src(source: str) -> str:
    if source == "2021" and _has_2021():
        return "2021"
    if source == "2017":
        return "2017"
    raise ValueError(f"unknown tracking source {source!r}")


def games(source: str) -> list[dict]:
    source = _src(source)
    if source == "2017":
        g, plays, _ = _meta_2017()
        n = plays.groupby("gameId").size()
        return [{"gameId": int(r.gameId), "season": int(r.season), "week": int(r.week), "date": r.gameDate,
                 "home": r.homeTeamAbbr, "away": r.visitorTeamAbbr, "homeScore": int(r.HomeScore),
                 "awayScore": int(r.VisitorScore), "plays": int(n.get(r.gameId, 0))}
                for r in g.sort_values(["week", "gameId"]).itertuples()]
    g, plays, _, _ = _meta_2021()
    n = plays.groupby("gameId").size()
    return [{"gameId": int(r.gameId), "season": int(r.season), "week": int(r.week), "date": r.gameDate,
             "home": r.homeTeamAbbr, "away": r.visitorTeamAbbr, "homeScore": None, "awayScore": None,
             "plays": int(n.get(r.gameId, 0))}
            for r in g.sort_values(["week", "gameId"]).itertuples()]


def _game_row(source: str, game_id: int) -> dict:
    row = next((x for x in games(source) if x["gameId"] == game_id), None)
    if row is None:
        raise ValueError(f"game {game_id} not in the {source} sample")
    return row


# -- tracking, normalized + cached per game -------------------------------------


def _cache_path(source: str, game_id: int) -> str:
    return os.path.join(CACHE_DIR, source, f"{game_id}.parquet")


def _norm_events(ev: pd.Series) -> pd.Series:
    ev = ev.where(ev.notna() & (ev != "None"))
    return ev.str.replace("autoevent_ballsnap", "ball_snap").str.replace("autoevent_passforward", "pass_forward")


def _split_2021_week(week: int) -> None:
    g, _, players, _ = _meta_2021()
    t = pd.read_csv(os.path.join(DIR_2021, f"week{week}.csv"))
    names = dict(zip(players["nflId"], players["displayName"]))
    teams = g.set_index("gameId")[["homeTeamAbbr", "visitorTeamAbbr"]]
    os.makedirs(os.path.join(CACHE_DIR, "2021"), exist_ok=True)
    for gid, d in t.groupby("gameId"):
        home, away = teams.loc[gid]
        side = np.where(d["team"] == "football", "ball", np.where(d["team"] == home, "home", "away"))
        out = pd.DataFrame({
            "playId": d["playId"].to_numpy(), "nflId": d["nflId"].to_numpy(), "frameId": d["frameId"].to_numpy(),
            "x": d["x"].to_numpy(), "y": d["y"].to_numpy(), "s": d["s"].to_numpy(), "side": side,
            "name": d["nflId"].map(names).to_numpy(), "jersey": d["jerseyNumber"].to_numpy(),
            "event": _norm_events(d["event"]).to_numpy(),
        })
        out.to_parquet(_cache_path("2021", int(gid)))


def _load_2017(game_id: int) -> pd.DataFrame:
    raw = os.path.join(DATA_DIR, f"tracking_gameId_{game_id}.csv")
    if not os.path.exists(raw):
        _download(GH_HISTORY + f"tracking_gameId_{game_id}.csv", raw)
    d = pd.read_csv(raw).rename(columns={"frame.id": "frameId"})
    return pd.DataFrame({
        "playId": d["playId"], "nflId": d["nflId"], "frameId": d["frameId"], "x": d["x"], "y": d["y"],
        "s": d["s"], "side": d["team"], "name": d["displayName"].where(d["team"] != "ball"),
        "jersey": d["jerseyNumber"], "event": _norm_events(d["event"]),
    })


def tracking(source: str, game_id: int) -> pd.DataFrame:
    source = _src(source)
    path = _cache_path(source, game_id)
    if not os.path.exists(path):
        week = _game_row(source, game_id)["week"]
        with _lock(f"{source}:{week if source == '2021' else game_id}"):
            if not os.path.exists(path):
                if source == "2021":
                    _split_2021_week(week)
                else:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    _load_2017(game_id).to_parquet(path)
    return _read_parquet(path)


@lru_cache(maxsize=6)
def _read_parquet(path: str) -> pd.DataFrame:
    return pd.read_parquet(path)


# -- plays ----------------------------------------------------------------------


def _clean_desc(s) -> str:
    return re.sub(r"^\(\d+:\d+\)\s*", "", str(s))


def _plays(source: str, game_id: int) -> pd.DataFrame:
    """One row per play in a common shape."""
    if source == "2017":
        _, p, _ = _meta_2017()
        p = p[p["gameId"] == game_id]
        kind = np.where(p["isSTPlay"].astype(bool), "st", np.where(p["PassResult"].isin(PASS_RESULTS), "pass", "run"))
        return pd.DataFrame({
            "playId": p["playId"], "qtr": p["quarter"], "clock": p["GameClock"].astype(str).str[:5],
            "down": p["down"], "togo": p["yardsToGo"], "offense": p["possessionTeam"], "kind": kind,
            "special": p["SpecialTeamsPlayType"], "passResult": p["PassResult"], "yards": p["PlayResult"],
            "desc": p["playDescription"], "formation": p["offenseFormation"], "personnelO": p["personnel.offense"],
            "personnelD": p["personnel.defense"], "box": p["defendersInTheBox"], "rushers": p["numberOfPassRushers"],
            "coverage": None,
        }).sort_values("playId")
    _, p, _, pff = _meta_2021()
    p = p[p["gameId"] == game_id]
    rush = pff[(pff["gameId"] == game_id) & (pff["pff_role"] == "Pass Rush")].groupby("playId").size()
    cov = (p["pff_passCoverage"].fillna("") + np.where(p["pff_passCoverageType"].notna(),
                                                      " (" + p["pff_passCoverageType"].fillna("") + ")", ""))
    return pd.DataFrame({
        "playId": p["playId"], "qtr": p["quarter"], "clock": p["gameClock"].astype(str).str[:5],
        "down": p["down"], "togo": p["yardsToGo"], "offense": p["possessionTeam"], "kind": "pass",
        "special": None, "passResult": p["passResult"], "yards": p["playResult"], "desc": p["playDescription"],
        "formation": p["offenseFormation"], "personnelO": p["personnelO"], "personnelD": p["personnelD"],
        "box": p["defendersInBox"], "rushers": p["playId"].map(rush), "coverage": cov.where(cov != ""),
    }).sort_values("playId")


def play_list(source: str, game_id: int) -> dict:
    source = _src(source)
    game = _game_row(source, game_id)
    rows = []
    for r in _plays(source, game_id).itertuples(index=False):
        down = _nan(r.down)
        rows.append({
            "playId": int(r.playId), "qtr": int(r.qtr), "clock": r.clock, "down": int(down) if down else None,
            "togo": int(r.togo) if down else None, "offense": r.offense, "kind": r.kind,
            "special": _nan(r.special), "passResult": _nan(r.passResult), "yards": _nan(r.yards),
            "desc": _clean_desc(r.desc),
        })
    return {"source": source, "game": game, "plays": rows}


# -- per-play detail ------------------------------------------------------------


def _abbr_match(abbr: str, full: str) -> bool:
    """'T.Kelce' / 'Ty.Montgomery' vs 'Travis Kelce'."""
    if "." not in abbr or not isinstance(full, str):
        return False
    first, last = abbr.split(".", 1)
    parts = full.split(" ", 1)
    return len(parts) == 2 and parts[1].replace(" ", "") == last.replace(" ", "") and parts[0].startswith(first)


def _focus_name(desc: str) -> str | None:
    """Targeted receiver on passes, ball carrier on runs, from the PBP text."""
    m = re.search(r"pass (?:incomplete )?(?:short|deep) (?:left|middle|right) to ([A-Z][A-Za-z]*\.[A-Za-z'\-]+)", desc)
    if m:
        return m.group(1)
    m = re.search(r"(?:^|\) |\.\s+)([A-Z][A-Za-z]*\.[A-Za-z'\-]+) (?:left|right|up|scrambles|kneels)", desc)
    return m.group(1) if m else None


def _positions(source: str) -> dict:
    if source == "2017":
        _, _, pl = _meta_2017()
        return dict(zip(pl["nflId"], pl["PositionAbbr"]))
    _, _, pl, _ = _meta_2021()
    return dict(zip(pl["nflId"], pl["officialPosition"]))


def _pff_play(game_id: int, play_id: int) -> dict[int, dict]:
    _, _, _, pff = _meta_2021()
    rows = pff[(pff["gameId"] == game_id) & (pff["playId"] == play_id)]
    out = {}
    for r in rows.itertuples(index=False):
        ev = [k for k, col in (("hit", "pff_hit"), ("hurry", "pff_hurry"), ("sack", "pff_sack")) if getattr(r, col) == 1]
        allowed = [k for k, col in (("hit", "pff_hitAllowed"), ("hurry", "pff_hurryAllowed"), ("sack", "pff_sackAllowed"))
                   if getattr(r, col) == 1]
        out[int(r.nflId)] = {"role": r.pff_role, "lined": _nan(r.pff_positionLinedUp), "events": ev, "allowed": allowed}
    return out


def _entities(t: pd.DataFrame, n: int, pos: dict) -> list[dict]:
    ents = []
    for key, grp in t.groupby(t["nflId"].fillna(-1), sort=False):
        side = grp["side"].iloc[0]
        x = np.full(n, np.nan); y = np.full(n, np.nan); s = np.full(n, np.nan)
        idx = grp["frameId"].to_numpy().astype(int) - 1
        x[idx] = grp["x"].to_numpy(); y[idx] = grp["y"].to_numpy(); s[idx] = grp["s"].to_numpy()
        vx = np.zeros(n); vy = np.zeros(n)
        vx[1:] = np.nan_to_num(np.diff(x) * FPS); vy[1:] = np.nan_to_num(np.diff(y) * FPS)
        ents.append({
            "id": None if key == -1 else int(key), "side": side,
            "name": None if side == "ball" else _nan(grp["name"].iloc[0]),
            "jersey": None if side == "ball" else (int(grp["jersey"].iloc[0]) if pd.notna(grp["jersey"].iloc[0]) else None),
            "pos": None if key == -1 else pos.get(int(key)),
            "x": x, "y": y, "s": s, "vx": vx, "vy": vy,
        })
    return ents


def pressure_index(qb: dict, rushers: list[dict], snap_f: int) -> np.ndarray:
    d = np.hypot(np.array([e["x"] for e in rushers]) - qb["x"], np.array([e["y"] for e in rushers]) - qb["y"])
    closing = np.zeros_like(d)
    closing[:, 1:] = -(d[:, 1:] - d[:, :-1]) * FPS
    w = np.clip(1 - d / PRESSURE_RADIUS, 0, 1) * (1 + np.clip(closing, 0, CLOSING_FULL) / CLOSING_FULL)
    p = np.nansum(w, axis=0)
    p[: snap_f - 1] = np.nan
    return p


def _play_core(source: str, game_id: int, play_id: int):
    """Entities + context for one play; shared by play_detail and the precompute."""
    pr = _plays(source, game_id)
    pr = pr[pr["playId"] == play_id]
    if pr.empty:
        raise ValueError(f"play {play_id} not found")
    pr = pr.iloc[0]
    game = _game_row(source, game_id)
    off_side = "home" if pr["offense"] == game["home"] else "away"
    t = tracking(source, game_id)
    t = t[t["playId"] == play_id].sort_values("frameId")
    if t.empty:
        raise ValueError(f"no tracking for play {play_id}")
    n = int(t["frameId"].max())
    ents = _entities(t, n, _positions(source))
    ents.sort(key=lambda e: (e["side"] == "ball", e["side"] != off_side))
    ev = t.dropna(subset=["event"]).drop_duplicates("event")[["frameId", "event"]].sort_values("frameId")
    events = [{"f": int(r.frameId), "name": r.event} for r in ev.itertuples()]
    snap_f = next((e["f"] for e in events if e["name"] == "ball_snap"), 1)
    pff = _pff_play(game_id, play_id) if source == "2021" else {}
    return pr, game, off_side, n, ents, events, snap_f, pff


def play_detail(source: str, game_id: int, play_id: int) -> dict:
    source = _src(source)
    pr, game, off_side, n, ents, events, snap_f, pff = _play_core(source, game_id, play_id)

    ball = next((e for e in ents if e["side"] == "ball"), None)
    offense = [e for e in ents if e["side"] == off_side]
    defense = [e for e in ents if e["side"] not in (off_side, "ball")]
    los = float(ball["x"][snap_f - 1]) if ball is not None and not np.isnan(ball["x"][snap_f - 1]) else None
    direction = None
    if los is not None and offense:
        direction = 1 if np.nanmean([e["x"][snap_f - 1] for e in offense]) < los else -1
    down = _nan(pr["down"])
    first_down = los + direction * int(pr["togo"]) if los is not None and direction and down else None

    focus = _focus_name(str(pr["desc"]))
    focus_id = next((e["id"] for e in offense if focus and _abbr_match(focus, e["name"])), None)
    qb = next((e for e in offense if pff.get(e["id"], {}).get("role") == "Pass"), None) or \
        next((e for e in offense if e["pos"] == "QB"), None)

    # Who counts as a receiver / coverage defender: PFF roles when we have them,
    # else position (receivers) and alignment at the snap (coverage = not on the line).
    if pff:
        receivers = [e for e in offense if pff.get(e["id"], {}).get("role") == "Pass Route"]
        coverage = [e for e in defense if pff.get(e["id"], {}).get("role") == "Coverage"]
    else:
        receivers = [e for e in offense if e["pos"] in ELIGIBLE]
        coverage = [e for e in defense if los is None or direction is None or
                    np.isnan(e["x"][snap_f - 1]) or (e["x"][snap_f - 1] - los) * direction > 2.0]

    pressure = None
    rushers = [e for e in defense if pff.get(e["id"], {}).get("role") == "Pass Rush"] if pff else defense
    if pr["kind"] == "pass" and qb is not None and rushers:
        pressure = pressure_index(qb, rushers, snap_f)

    separation, catch = {}, {}
    model = cm.load()
    throw_f = next((e["f"] for e in events if e["name"] in ("pass_forward", "pass_shovel")), None)
    end_f = throw_f or next((e["f"] for e in events if e["name"] in ("qb_sack", "run", "handoff")), n)
    if coverage:
        cx = np.array([e["x"] for e in coverage]); cy = np.array([e["y"] for e in coverage])
        cvx = np.array([e["vx"] for e in coverage]); cvy = np.array([e["vy"] for e in coverage])
        for e in receivers:
            d = np.nanmin(np.hypot(cx - e["x"], cy - e["y"]), axis=0)
            d[: snap_f - 1] = np.nan
            separation[str(e["id"])] = d
            if model and qb is not None and pr["kind"] == "pass" and los is not None and direction:
                X = cm.features(e["x"], e["y"], e["vx"], e["vy"], qb["x"], qb["y"], cx, cy, cvx, cvy, los, direction)
                p = cm.predict(model, X)
                p[: snap_f - 1] = np.nan
                p[end_f:] = np.nan
                p[np.isnan(e["x"])] = np.nan
                catch[str(e["id"])] = p

    def arr(a, nd=2):
        return [None if np.isnan(v) else round(float(v), nd) for v in a]

    pff_events = [{"id": pid, "name": next((e["name"] for e in ents if e["id"] == pid), None),
                   "events": v["events"], "allowed": v["allowed"]}
                  for pid, v in pff.items() if v["events"] or v["allowed"]]
    return {
        "source": source, "gameId": game_id, "playId": play_id, "season": game["season"], "week": game["week"],
        "home": game["home"], "away": game["away"], "offenseSide": off_side,
        "offense": pr["offense"], "qtr": int(pr["qtr"]), "clock": pr["clock"],
        "down": int(down) if down else None, "togo": int(pr["togo"]) if down else None, "kind": pr["kind"],
        "desc": _clean_desc(pr["desc"]), "formation": _nan(pr["formation"]), "personnelO": _nan(pr["personnelO"]),
        "personnelD": _nan(pr["personnelD"]), "box": _nan(pr["box"]), "rushers": _nan(pr["rushers"]),
        "coverage": _nan(pr["coverage"]), "passResult": _nan(pr["passResult"]),
        "frames": n, "events": events, "snapFrame": snap_f, "endsAtThrow": source == "2021",
        "los": los, "firstDown": first_down, "direction": direction,
        "focusId": focus_id, "qbId": qb["id"] if qb else None,
        "players": [{"id": e["id"], "side": e["side"], "name": e["name"], "jersey": e["jersey"], "pos": e["pos"],
                     "role": pff.get(e["id"], {}).get("role"), "x": arr(e["x"]), "y": arr(e["y"]), "s": arr(e["s"])}
                    for e in ents],
        "pressure": arr(pressure) if pressure is not None else None,
        "pff": pff_events,
        "pressureCheck": pressure_validation() if source == "2021" else None,
        "separation": {k: arr(v) for k, v in separation.items()},
        "catchProb": {k: arr(v, 3) for k, v in catch.items()},
        "model": model_card() if catch else None,
    }


# -- precomputed summaries -------------------------------------------------------


VALIDATION_PATH = os.path.join(CACHE_DIR, "2021", "pressure_validation.json")


@lru_cache(maxsize=1)
def pressure_validation() -> dict | None:
    if not os.path.exists(VALIDATION_PATH):
        return None
    with open(VALIDATION_PATH) as f:
        return json.load(f)


def model_card() -> dict | None:
    m = cm.load()
    if not m:
        return None
    return {"source": m.get("source"), **{k: m["metrics"][k] for k in
                                           ("auc", "log_loss", "baseline_log_loss", "holdout_passes", "holdout_weeks")}}
