"""
Positions overview (Book module): every open Kalshi position, grouped by game
for this week's single-game bets, with season-long/award/draft futures
grouped separately and treated as secondary.

Deliberately generic: classification is done structurally from each ticker's
event_ticker (does it start with a date + two team codes, or not?) rather
than from a curated series list, so it covers any market the account holds,
not just the ones this tool has cataloged elsewhere.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from kalshi_book import KalshiClient, position_from_kalshi
from kalshi_markets import MarketIndex

WEEKLY_EVENT_RE = re.compile(r"^\d{2}[A-Z]{3}\d{2}[A-Z]{2,3}[A-Z]{2,3}$")


def build_positions_overview() -> dict:
    client = KalshiClient()
    idx = MarketIndex()

    raw_positions = client.get_positions()
    held = [position_from_kalshi(p) for p in raw_positions]
    held = [p for p in held if p.contracts != 0]

    if not held:
        return {"weekly": [], "futures": [], "count": 0}

    with ThreadPoolExecutor(max_workers=8) as pool:
        markets = list(pool.map(lambda p: client.get_market(p.ticker)["market"], held))

    series_title_cache: dict[str, str] = {}

    def series_title(series_ticker: str) -> str:
        if series_ticker not in series_title_cache:
            try:
                series_title_cache[series_ticker] = client.get_series(series_ticker)["series"]["title"]
            except Exception:
                series_title_cache[series_ticker] = series_ticker
        return series_title_cache[series_ticker]

    event_meta_cache: dict[str, dict] = {}

    def event_meta(event_ticker: str) -> dict:
        if event_ticker not in event_meta_cache:
            try:
                event_meta_cache[event_ticker] = client.get(
                    f"/trade-api/v2/events/{event_ticker}", params={"with_nested_markets": False}
                )["event"]
            except Exception:
                event_meta_cache[event_ticker] = {}
        return event_meta_cache[event_ticker]

    weekly_groups: dict[str, dict] = {}
    futures_groups: dict[str, list] = {}

    for pos, market in zip(held, markets):
        event_ticker = market.get("event_ticker", "")
        series_ticker = event_ticker.split("-", 1)[0] if "-" in event_ticker else pos.ticker.split("-", 1)[0]
        suffix = event_ticker.split("-", 1)[1] if "-" in event_ticker else ""

        custom = market.get("custom_strike") or {}
        player_id = custom.get("football_player")
        team_id = custom.get("football_team")
        player_name = idx.player_name(player_id) if player_id else None
        team_code = idx.team_abbr(team_id) if team_id else None

        entry = {
            "ticker": pos.ticker,
            "title": market.get("title"),
            "series": series_ticker,
            "series_label": series_title(series_ticker),
            "threshold": market.get("floor_strike") if market.get("floor_strike") is not None else market.get("cap_strike"),
            "player_name": player_name,
            "team_code": team_code,
            "my_position": {"side": pos.side, "contracts": pos.contracts, "avg_price_cents": round(pos.avg_price_cents, 2)},
            **_market_quote(market),
        }

        is_weekly = bool(WEEKLY_EVENT_RE.match(suffix.split("-")[0])) if suffix else False
        if is_weekly:
            game_key = suffix[:9]  # "26SEP27CARCLE" -> "26SEP27" + away/home embedded in team part; use date+teams as key
            # Reconstruct a stable per-game key: date prefix + the two team codes
            # embedded right after it (suffix itself, stripped of any trailing
            # -threshold segments some series append after the team code).
            m = re.match(r"^(\d{2}[A-Z]{3}\d{2})([A-Z]{2,3})([A-Z]{2,3})", suffix)
            game_key = m.group(0) if m else suffix
            group = weekly_groups.setdefault(game_key, {"game_key": game_key, "positions": [], "meta": None})
            group["positions"].append(entry)
            if group["meta"] is None:
                ev = event_meta(f"KXNFLGAME-{game_key}")
                group["meta"] = {"sub_title": ev.get("sub_title"), "title": ev.get("title")}
        else:
            futures_groups.setdefault(series_title(series_ticker), []).append(entry)

    weekly = [
        {"game_key": g["game_key"], "matchup": (g["meta"] or {}).get("sub_title") or g["game_key"], "positions": g["positions"]}
        for g in weekly_groups.values()
    ]
    futures = [{"category": cat, "positions": positions} for cat, positions in futures_groups.items()]

    return {"weekly": weekly, "futures": futures, "count": len(held)}


def _market_quote(market: dict) -> dict:
    def dollars(key):
        v = market.get(key)
        return float(v) if v not in (None, "") else None

    return {
        "yes_bid": dollars("yes_bid_dollars"),
        "yes_ask": dollars("yes_ask_dollars"),
        "implied_prob_yes": (
            round((dollars("yes_bid_dollars") + dollars("yes_ask_dollars")) / 2, 4)
            if dollars("yes_bid_dollars") is not None and dollars("yes_ask_dollars") is not None
            else dollars("last_price_dollars")
        ),
        "volume": float(market.get("volume_fp") or 0),
    }
