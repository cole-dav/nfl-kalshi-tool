"""
Week/Slate overview: every game this week with moneyline/spread/total, and a
per-game detail view (combo markets + both teams' rosters) for the
click-team-then-player drill-down.
"""

from __future__ import annotations

import traceback
from concurrent.futures import ThreadPoolExecutor

import depth_matchups as dm
import live_scores as ls
import madden_ratings as mr
import nflverse_data as nd
import team_tendencies as tt
from kalshi_markets import MarketIndex


def build_week_overview(week: int | None = None) -> dict:
    """One NFL week of open Kalshi games. Kalshi lists future weeks' moneylines
    early, so every open game is bucketed by its nflverse schedule week; `week`
    picks a bucket (default: the earliest). Returns the available weeks too so
    the UI can page forward/back."""
    idx = MarketIndex()
    games = [g for g in idx.get_week_games() if g["close_time"]]
    games.sort(key=lambda g: g["close_time"])

    # Kalshi close_time is a market-close window, not kickoff (Sunday games
    # carry a Tuesday date), so take the real kickoff from the nflverse schedule.
    for g in games:
        g["schedule"] = nd.find_upcoming_game(nd.kalshi_to_nflverse_team(g["away"]), nd.kalshi_to_nflverse_team(g["home"]))
    weeks = sorted({g["schedule"]["week"] for g in games if g["schedule"]})
    if not weeks:
        # No schedule match at all: fall back to Kalshi's nearest-week window.
        current = {g["event_ticker"] for g in idx.get_current_week_games()}
        week_games, week = [g for g in games if g["event_ticker"] in current], None
    else:
        if week not in weeks:
            week = weeks[0]
        # Unscheduled games ride along with the earliest week.
        week_games = [g for g in games if (g["schedule"]["week"] if g["schedule"] else weeks[0]) == week]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(idx.get_game_core_markets, week_games))

    records = nd.team_records()
    for g, src in zip(results, week_games):
        g["records"] = {
            "away": records.get(nd.kalshi_to_nflverse_team(g["away"]), "0-0"),
            "home": records.get(nd.kalshi_to_nflverse_team(g["home"]), "0-0"),
        }
        g["schedule"] = src["schedule"]
        g["live"] = ls.game_live(g["away"], g["home"])
    results.sort(key=lambda g: (g["schedule"]["gameday"], g["schedule"]["gametime"]) if g["schedule"] else ("9999", g["kickoff"] or ""))
    return {"week": week, "weeks": weeks, "games": results}


def find_team_next_game(team: str) -> dict | None:
    """The team's earliest open Kalshi game (Kalshi code), ordered by nflverse
    kickoff the same way build_week_overview buckets weeks, so a bye week
    falls through to the following one. None when nothing is open."""
    games = [g for g in MarketIndex().get_week_games() if team in (g["away"], g["home"])]
    if not games:
        return None
    for g in games:
        g["schedule"] = nd.find_upcoming_game(nd.kalshi_to_nflverse_team(g["away"]), nd.kalshi_to_nflverse_team(g["home"]))
    g = min(games, key=lambda g: ((g["schedule"]["gameday"], g["schedule"]["gametime"]) if g["schedule"] else ("9999", g["close_time"] or "")))
    return {"event_ticker": g["event_ticker"], "away": g["away"], "home": g["home"],
            "week": g["schedule"]["week"] if g["schedule"] else None}

def _injuries_by_madden(team: str) -> list[dict]:
    """Injury report + IR for one team (nflverse code), best players first by
    Madden OVR. The rating only drives the order; it isn't sent to the client."""
    rows = nd.team_injury_list(team)
    rows = rows.astype(object).where(rows.notna(), None).to_dict(orient="records")
    roster = nd.load_rosters()
    jerseys = roster[roster["team"] == team].set_index("gsis_id")["jersey_number"].to_dict()
    for r in rows:
        r["jersey_number"] = jerseys.get(r.get("gsis_id"))
    mr.attach_ratings(rows, team)
    rows.sort(key=lambda r: -(r["madden"]["ovr"] if r["madden"] else 0))
    for r in rows:
        r.pop("madden"); r.pop("jersey_number"); r.pop("gsis_id", None)
    return rows


def build_game_detail(event_ticker: str) -> dict:
    idx = MarketIndex()
    games = {g["event_ticker"]: g for g in idx.get_week_games()}
    game = games.get(event_ticker)
    if game is None:
        raise ValueError(f"Unknown or closed event_ticker: {event_ticker}")

    core = idx.get_game_core_markets(game)
    combos = idx.get_game_combos(event_ticker)

    away_nflverse = nd.kalshi_to_nflverse_team(game["away"])
    home_nflverse = nd.kalshi_to_nflverse_team(game["home"])

    try:
        tendencies = tt.matchup_tendencies(away_nflverse, home_nflverse, core.get("spread"), core.get("total"))
    except Exception as e:
        traceback.print_exc()
        tendencies = {"error": f"tendencies unavailable: {e}"}

    records = nd.team_records()
    return {
        **core,
        "records": {"away": records.get(away_nflverse, "0-0"), "home": records.get(home_nflverse, "0-0")},
        "combos": combos,
        "matchups": dm.game_matchups(away_nflverse, home_nflverse),
        "tendencies": tendencies,
        "injuries": {"away": _injuries_by_madden(away_nflverse), "home": _injuries_by_madden(home_nflverse)},
        "live": ls.game_live(game["away"], game["home"]),
    }
