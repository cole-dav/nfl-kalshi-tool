"""
Week/Slate overview: every game this week with moneyline/spread/total, and a
per-game detail view (combo markets + both teams' rosters) for the
click-team-then-player drill-down.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import depth_matchups as dm
import nflverse_data as nd
from kalshi_markets import MarketIndex


def build_week_overview() -> dict:
    idx = MarketIndex()
    games = idx.get_current_week_games()
    games.sort(key=lambda g: g["close_time"] or "")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(idx.get_game_core_markets, games))

    # Kalshi close_time is a market-close window, not kickoff (Sunday games
    # carry a Tuesday date), so take the real kickoff from the nflverse schedule.
    records = nd.team_records()
    for g in results:
        away_nv = nd.kalshi_to_nflverse_team(g["away"])
        home_nv = nd.kalshi_to_nflverse_team(g["home"])
        g["records"] = {"away": records.get(away_nv, "0-0"), "home": records.get(home_nv, "0-0")}
        g["schedule"] = nd.find_upcoming_game(away_nv, home_nv)
    results.sort(key=lambda g: (g["schedule"]["gameday"], g["schedule"]["gametime"]) if g["schedule"] else ("9999", g["kickoff"] or ""))
    return {"games": results}


def build_game_detail(event_ticker: str) -> dict:
    idx = MarketIndex()
    games = {g["event_ticker"]: g for g in idx.get_current_week_games()}
    game = games.get(event_ticker)
    if game is None:
        raise ValueError(f"Unknown or non-current-week event_ticker: {event_ticker}")

    core = idx.get_game_core_markets(game)
    combos = idx.get_game_combos(event_ticker)

    away_nflverse = nd.kalshi_to_nflverse_team(game["away"])
    home_nflverse = nd.kalshi_to_nflverse_team(game["home"])

    records = nd.team_records()
    return {
        **core,
        "records": {"away": records.get(away_nflverse, "0-0"), "home": records.get(home_nflverse, "0-0")},
        "combos": combos,
        "matchups": dm.game_matchups(away_nflverse, home_nflverse),
    }
