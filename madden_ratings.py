"""
Madden overall ratings for the game-view rosters.

EA's public drop-api still serves the previous game's ratings, so this reads
the current-edition data embedded (__NEXT_DATA__) in EA's own ratings page,
one request per team via its `?team=<EA team id>` filter. Results are cached
in memory and on disk for 12h (EA updates ratings weekly).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata

import requests

PAGE_URL = "https://www.ea.com/games/madden-nfl/ratings"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/128 Safari/537.36"}
CACHE_SECONDS = 12 * 3600
CACHE_PATH = os.path.join(os.path.dirname(__file__), "cache", "madden_ratings.json")

# EA team labels ("NY Jets", "Los Angeles Rams") end in the nickname, which is
# unique per team, so key the nflverse code on that.
NICKNAME_TO_NFLVERSE = {
    "Cardinals": "ARI", "Falcons": "ATL", "Ravens": "BAL", "Bills": "BUF", "Panthers": "CAR",
    "Bears": "CHI", "Bengals": "CIN", "Browns": "CLE", "Cowboys": "DAL", "Broncos": "DEN",
    "Lions": "DET", "Packers": "GB", "Texans": "HOU", "Colts": "IND", "Jaguars": "JAX",
    "Chiefs": "KC", "Raiders": "LV", "Chargers": "LAC", "Rams": "LA", "Dolphins": "MIA",
    "Vikings": "MIN", "Patriots": "NE", "Saints": "NO", "Giants": "NYG", "Jets": "NYJ",
    "Eagles": "PHI", "Steelers": "PIT", "49ers": "SF", "Seahawks": "SEA", "Buccaneers": "TB",
    "Titans": "TEN", "Commanders": "WAS",
}

_lock = threading.Lock()
_mem: dict = {}


def _next_data(params: dict | None = None) -> dict:
    resp = requests.get(PAGE_URL, params=params, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', resp.text, re.S)
    if not m:
        raise RuntimeError("EA ratings page layout changed: no __NEXT_DATA__")
    return json.loads(m.group(1))["props"]["pageProps"]


def _ea_team_ids() -> dict[str, int]:
    """nflverse code -> EA team id, from the ratings page's team filter."""
    groups = _next_data()["ratingsFilters"]["teamGroups"]
    out = {}
    for g in groups:
        for t in g.get("teams", []):
            code = NICKNAME_TO_NFLVERSE.get(t["label"].split()[-1])
            if code:
                out[code] = t["id"]
    return out


def _fetch_team(ea_team_id: int) -> list[dict]:
    items = _next_data({"team": ea_team_id})["ratingDetails"]["items"]
    return [{
        "first": it.get("firstName") or "",
        "last": it.get("lastName") or "",
        "jersey": it.get("jerseyNum"),
        "pos": (it.get("position") or {}).get("shortLabel"),
        "ovr": it.get("overallRating"),
        "archetype": (it.get("archetype") or {}).get("label"),
        "speed": ((it.get("stats") or {}).get("speed") or {}).get("value"),
        "iteration": (it.get("iteration") or {}).get("label"),
    } for it in items]


def _load_disk() -> dict:
    try:
        with open(CACHE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def _save_disk(data: dict) -> None:
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(data, f)


def team_ratings(team: str) -> list[dict]:
    """Madden ratings for one team (nflverse code); [] if EA is unreachable."""
    with _lock:
        if not _mem:
            _mem.update(_load_disk())
        entry = _mem.get(team)
        if entry and time.time() - entry["ts"] < CACHE_SECONDS:
            return entry["players"]
        try:
            ids = _mem.get("_ids")
            if not ids or time.time() - ids["ts"] > CACHE_SECONDS:
                ids = {"ts": time.time(), "map": _ea_team_ids()}
                _mem["_ids"] = ids
            ea_id = ids["map"].get(team)
            if ea_id is None:
                return []
            players = _fetch_team(ea_id)
        except Exception:
            # Serve stale data rather than nothing if EA is down.
            return entry["players"] if entry else []
        _mem[team] = {"ts": time.time(), "players": players}
        _save_disk(_mem)
        return players


def _norm(name: str) -> str:
    s = unicodedata.normalize("NFD", name.lower())
    s = re.sub(r"[̀-ͯ.'\-]", "", s)
    s = re.sub(r"\s+(jr|sr|ii|iii|iv|v)$", "", s.strip())
    return re.sub(r"\s+", " ", s)


def attach_ratings(roster: list[dict], team: str) -> list[dict]:
    """Adds a `madden` dict to each roster row, matching on full name, then
    last name + jersey number (EA and nflverse disagree on some first names)."""
    ratings = team_ratings(team)
    by_name = {_norm(f"{r['first']} {r['last']}"): r for r in ratings}
    by_last_jersey = {(_norm(r["last"]), r["jersey"]): r for r in ratings}
    for row in roster:
        name = _norm(row["full_name"])
        jersey = row.get("jersey_number")
        jersey = int(jersey) if jersey is not None and jersey == jersey else None
        r = by_name.get(name) or by_last_jersey.get((name.split(" ")[-1], jersey))
        row["madden"] = {k: r[k] for k in ("ovr", "archetype", "speed", "iteration")} if r else None
    return roster
