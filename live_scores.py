"""
Live game state from ESPN's public scoreboard: score, clock, possession,
down & distance, last play, and ESPN's win probability. Keyed by Kalshi team
codes so the slate and game detail can attach it next to the market prices.
"""

from __future__ import annotations

import threading
import time

import requests

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
TTL = 20  # seconds; callers' snapshots refresh every ~30s anyway

# ESPN abbreviations that differ from Kalshi's team codes.
ESPN_TO_KALSHI = {"JAX": "JAC", "WSH": "WAS"}

_lock = threading.Lock()
_cache: tuple[float, dict] = (0.0, {})


def _team(c: dict) -> str:
    abbr = c["team"]["abbreviation"]
    return ESPN_TO_KALSHI.get(abbr, abbr)


def _parse_event(e: dict) -> tuple[tuple[str, str], dict] | None:
    comp = e["competitions"][0]
    sides = {c["homeAway"]: c for c in comp["competitors"]}
    if "home" not in sides or "away" not in sides:
        return None
    away, home = _team(sides["away"]), _team(sides["home"])
    st = e["status"]["type"]
    sit = comp.get("situation") or {}
    last = sit.get("lastPlay") or {}
    prob = last.get("probability") or {}
    ids = {sides["away"]["team"]["id"]: away, sides["home"]["team"]["id"]: home}
    live = {
        "state": st["state"],  # pre | in | post
        "detail": st.get("shortDetail") or st.get("detail"),
        "period": e["status"].get("period"),
        "clock": e["status"].get("displayClock"),
        "score": {"away": int(sides["away"].get("score") or 0), "home": int(sides["home"].get("score") or 0)},
        "possession": ids.get(sit.get("possession")),
        "down_distance": sit.get("shortDownDistanceText"),
        "yard_line": sit.get("possessionText"),
        "red_zone": bool(sit.get("isRedZone")),
        "last_play": last.get("text"),
        "win_prob": {"away": prob["awayWinPercentage"], "home": prob["homeWinPercentage"]}
                    if "homeWinPercentage" in prob else None,
    }
    return (away, home), live


def scoreboard() -> dict[tuple[str, str], dict]:
    """(away, home) Kalshi codes -> live state for this ESPN scoreboard week.
    Serves the last good copy if ESPN errors."""
    global _cache
    with _lock:
        fetched_at, data = _cache
        if time.time() - fetched_at < TTL:
            return data
        try:
            resp = requests.get(SCOREBOARD_URL, timeout=8)
            resp.raise_for_status()
            data = dict(filter(None, (_parse_event(e) for e in resp.json().get("events", []))))
        except Exception:
            pass
        _cache = (time.time(), data)
        return data


def game_live(away: str, home: str) -> dict | None:
    """Live state for one game, or None before ESPN's scoreboard lists it
    (e.g. next week's slate)."""
    try:
        return scoreboard().get((away, home))
    except Exception:
        return None
