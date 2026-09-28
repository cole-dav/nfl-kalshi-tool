"""
Week/Slate overview: every game this week with moneyline/spread/total, and a
per-game detail view (combo markets + both teams' rosters) for the
click-team-then-player drill-down.
"""

from __future__ import annotations

import traceback
from concurrent.futures import ThreadPoolExecutor

import depth_matchups as dm
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
    results.sort(key=lambda g: (g["schedule"]["gameday"], g["schedule"]["gametime"]) if g["schedule"] else ("9999", g["kickoff"] or ""))
    return {"week": week, "weeks": weeks, "games": results}


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
    }
