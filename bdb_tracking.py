"""
All-22 play replays from NFL Big Data Bowl player-tracking data.

Tracking (x, y per player at 10 Hz) is not published week to week -- the only
public copies are the Big Data Bowl sample seasons. The one that needs no
login is the 2019 contest's sample in the NFL's own GitHub repo: games, plays
and players for 2017 Weeks 1-6, plus full tracking for ONE game
(2017090700, KC @ NE, the Thursday opener -- every one of its 177 plays).
It's downloaded into data/bdb/ on first use.

More games: drop more 2019-format `tracking_gameId_<gameId>.csv` files into
data/bdb/ (the full 91-game set was on Kaggle) and they show up in the game
list -- games.csv / plays.csv already cover all 91 games.

Per play this returns positions for every player and the ball, plus two
metrics derived from the dots alone (no trained model):

  * pressure  -- a "Continuous Pocket Pressure"-style index after the 2023
                 winners: each defender within 7 yd of the QB adds
                 (1 - d/7), scaled up by up to 2x for closing speed;
  * separation -- per eligible receiver, distance to the nearest defender.

Coordinates are the raw tracking frame: x 0-120 yd along the field
(end zones included), y 0-53.3 yd across.
"""

from __future__ import annotations

import glob
import os
import re
import threading
from functools import lru_cache

import numpy as np
import pandas as pd
import requests

DATA_DIR = os.path.join(os.path.dirname(__file__), "data", "bdb")
SOURCE = "https://raw.githubusercontent.com/nfl-football-ops/Big-Data-Bowl/master/Data/"
SAMPLE_FILES = ("games.csv", "plays.csv", "players.csv", "tracking_gameId_2017090700.csv")

PRESSURE_RADIUS = 7.0     # yd: defenders farther than this add nothing
CLOSING_FULL = 8.0        # yd/s of closing speed that doubles a defender's weight
ELIGIBLE = {"WR", "TE", "RB", "FB"}
PASS_RESULTS = {"C", "I", "S", "IN", "R"}  # complete, incomplete, sack, int, scramble

_dl_lock = threading.Lock()


def _ensure_sample() -> None:
    with _dl_lock:
        os.makedirs(DATA_DIR, exist_ok=True)
        for name in SAMPLE_FILES:
            path = os.path.join(DATA_DIR, name)
            if os.path.exists(path):
                continue
            r = requests.get(SOURCE + name, timeout=120)
            r.raise_for_status()
            with open(path + ".part", "wb") as f:
                f.write(r.content)
            os.replace(path + ".part", path)


@lru_cache(maxsize=1)
def _meta() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    _ensure_sample()
    games = pd.read_csv(os.path.join(DATA_DIR, "games.csv"))
    plays = pd.read_csv(os.path.join(DATA_DIR, "plays.csv"))
    players = pd.read_csv(os.path.join(DATA_DIR, "players.csv"))
    return games, plays, players


def _tracked_game_ids() -> list[int]:
    _ensure_sample()
    ids = []
    for p in glob.glob(os.path.join(DATA_DIR, "tracking_gameId_*.csv")):
        m = re.search(r"tracking_gameId_(\d+)\.csv$", p)
        if m:
            ids.append(int(m.group(1)))
    return sorted(ids)


@lru_cache(maxsize=4)
def _tracking(game_id: int) -> pd.DataFrame:
    d = pd.read_csv(os.path.join(DATA_DIR, f"tracking_gameId_{game_id}.csv"))
    return d.rename(columns={"frame.id": "frameId"})


def games() -> list[dict]:
    g, plays, _ = _meta()
    out = []
    for gid in _tracked_game_ids():
        row = g[g["gameId"] == gid]
        if row.empty:
            continue
        r = row.iloc[0]
        out.append({
            "gameId": gid, "season": int(r["season"]), "week": int(r["week"]), "date": r["gameDate"],
            "home": r["homeTeamAbbr"], "away": r["visitorTeamAbbr"],
            "homeScore": int(r["HomeScore"]), "awayScore": int(r["VisitorScore"]),
            "plays": int((plays["gameId"] == gid).sum()),
        })
    return out


def _kind(r) -> str:
    if bool(r["isSTPlay"]):
        return "st"
    if r["PassResult"] in PASS_RESULTS:
        return "pass"
    return "run"


def _nan(v):
    if pd.isna(v):
        return None
    return v.item() if isinstance(v, np.generic) else v


def play_list(game_id: int) -> dict:
    g, plays, _ = _meta()
    if game_id not in _tracked_game_ids():
        raise ValueError(f"no tracking for game {game_id}")
    p = plays[plays["gameId"] == game_id].sort_values("playId")
    rows = []
    for _, r in p.iterrows():
        desc = re.sub(r"^\(\d+:\d+\)\s*", "", str(r["playDescription"]))
        rows.append({
            "playId": int(r["playId"]), "qtr": int(r["quarter"]), "clock": str(r["GameClock"])[:5],
            "down": int(r["down"]) or None, "togo": int(r["yardsToGo"]) or None,
            "offense": r["possessionTeam"], "kind": _kind(r),
            "special": _nan(r["SpecialTeamsPlayType"]), "passResult": _nan(r["PassResult"]),
            "yards": _nan(r["PlayResult"]), "desc": desc,
        })
    return {"game": next(x for x in games() if x["gameId"] == game_id), "plays": rows}


def _abbr_match(abbr: str, full: str) -> bool:
    """'T.Kelce' / 'Ty.Montgomery' vs 'Travis Kelce'."""
    if "." not in abbr:
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


def play_detail(game_id: int, play_id: int) -> dict:
    g, plays, players = _meta()
    if game_id not in _tracked_game_ids():
        raise ValueError(f"no tracking for game {game_id}")
    pr = plays[(plays["gameId"] == game_id) & (plays["playId"] == play_id)]
    if pr.empty:
        raise ValueError(f"play {play_id} not found")
    pr = pr.iloc[0]
    gr = g[g["gameId"] == game_id].iloc[0]
    off_side = "home" if pr["possessionTeam"] == gr["homeTeamAbbr"] else "away"

    t = _tracking(game_id)
    t = t[t["playId"] == play_id].sort_values(["frameId"])
    n = int(t["frameId"].max())
    pos = dict(zip(players["nflId"], players["PositionAbbr"]))

    ents = []
    for key, grp in t.groupby(t["nflId"].fillna(-1), sort=False):
        side = grp["team"].iloc[0]
        x = np.full(n, np.nan); y = np.full(n, np.nan); s = np.full(n, np.nan)
        idx = grp["frameId"].to_numpy() - 1
        x[idx] = grp["x"].to_numpy(); y[idx] = grp["y"].to_numpy(); s[idx] = grp["s"].to_numpy()
        ents.append({
            "id": None if key == -1 else int(key), "side": side,
            "name": None if side == "ball" else grp["displayName"].iloc[0],
            "jersey": None if side == "ball" else _nan(grp["jerseyNumber"].iloc[0]),
            "pos": None if key == -1 else pos.get(int(key)),
            "x": x, "y": y, "s": s,
        })
    ents.sort(key=lambda e: (e["side"] == "ball", e["side"] != off_side))

    ev = (t.dropna(subset=["event"]).drop_duplicates(["frameId", "event"])[["frameId", "event"]])
    events = [{"f": int(r.frameId), "name": r.event} for r in ev.itertuples()]
    snap_f = next((e["f"] for e in events if e["name"] == "ball_snap"), 1)

    ball = next((e for e in ents if e["side"] == "ball"), None)
    offense = [e for e in ents if e["side"] == off_side]
    defense = [e for e in ents if e["side"] not in (off_side, "ball")]
    los = float(ball["x"][snap_f - 1]) if ball is not None and not np.isnan(ball["x"][snap_f - 1]) else None
    # Direction of attack: the offense lines up on its own side of the ball.
    direction = None
    if los is not None and offense:
        mean_x = np.nanmean([e["x"][snap_f - 1] for e in offense])
        direction = 1 if mean_x < los else -1
    first_down = None
    if los is not None and direction and int(pr["down"]) > 0:
        first_down = los + direction * int(pr["yardsToGo"])

    focus = _focus_name(str(pr["playDescription"]))
    focus_id = next((e["id"] for e in offense if focus and _abbr_match(focus, e["name"])), None)

    # Metrics (only meaningful after the snap; earlier frames are null).
    def_xy = np.array([[e["x"], e["y"]] for e in defense]) if defense else np.zeros((0, 2, n))
    pressure = None
    qb = next((e for e in offense if e["pos"] == "QB"), None)
    if _kind(pr) == "pass" and qb is not None and len(def_xy):
        d = np.hypot(def_xy[:, 0] - qb["x"], def_xy[:, 1] - qb["y"])          # (defenders, frames)
        closing = np.zeros_like(d)
        closing[:, 1:] = -(d[:, 1:] - d[:, :-1]) / 0.1
        w = np.clip(1 - d / PRESSURE_RADIUS, 0, 1) * (1 + np.clip(closing, 0, CLOSING_FULL) / CLOSING_FULL)
        pressure = np.nansum(w, axis=0)
        pressure[: snap_f - 1] = np.nan
    separation = {}
    if len(def_xy):
        for e in offense:
            if e["pos"] in ELIGIBLE:
                d = np.nanmin(np.hypot(def_xy[:, 0] - e["x"], def_xy[:, 1] - e["y"]), axis=0)
                d[: snap_f - 1] = np.nan
                separation[str(e["id"])] = d

    def arr(a, nd=2):
        return [None if np.isnan(v) else round(float(v), nd) for v in a]

    return {
        "gameId": game_id, "playId": play_id,
        "home": gr["homeTeamAbbr"], "away": gr["visitorTeamAbbr"], "offenseSide": off_side,
        "offense": pr["possessionTeam"], "qtr": int(pr["quarter"]), "clock": str(pr["GameClock"])[:5],
        "down": int(pr["down"]) or None, "togo": int(pr["yardsToGo"]) or None, "kind": _kind(pr),
        "desc": re.sub(r"^\(\d+:\d+\)\s*", "", str(pr["playDescription"])),
        "formation": _nan(pr["offenseFormation"]), "personnelO": _nan(pr["personnel.offense"]),
        "personnelD": _nan(pr["personnel.defense"]), "box": _nan(pr["defendersInTheBox"]),
        "rushers": _nan(pr["numberOfPassRushers"]),
        "frames": n, "events": events, "snapFrame": snap_f,
        "los": los, "firstDown": first_down, "direction": direction,
        "focusId": focus_id, "qbId": qb["id"] if qb else None,
        "players": [{"id": e["id"], "side": e["side"], "name": e["name"], "jersey": e["jersey"],
                     "pos": e["pos"], "x": arr(e["x"]), "y": arr(e["y"]), "s": arr(e["s"])} for e in ents],
        "pressure": arr(pressure) if pressure is not None else None,
        "separation": {k: arr(v) for k, v in separation.items()},
    }
