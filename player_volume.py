"""
Trailing 4-hour Kalshi volume per player across open weekly player props,
used to rank search-box autocomplete.

Kalshi markets only carry lifetime and 24h volume, so the 4h figure is summed
from 1-minute candlesticks (batch endpoint, max 100 tickers and 10k candles
per call -> 40 tickers x 240 minutes). Markets with no 24h volume are skipped
since they can't have traded in the last 4h.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from kalshi_markets import MarketIndex, WEEKLY_PLAYER_PROP_SERIES

WINDOW_SECONDS = 4 * 3600
CACHE_SECONDS = 300
BATCH = 40

_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "data": None}


def _open_player_markets(idx: MarketIndex) -> dict[str, tuple[str, str | None]]:
    """ticker -> (Kalshi player target id, team target id), for open weekly
    props that traded in the last 24h."""
    def fetch(series):
        return idx.client.get_events(series_ticker=series, status="open")

    with ThreadPoolExecutor(max_workers=4) as pool:
        event_lists = list(pool.map(fetch, WEEKLY_PLAYER_PROP_SERIES))
    out = {}
    for events in event_lists:
        for ev in events:
            for m in ev.get("markets", []):
                custom = m.get("custom_strike") or {}
                pid = custom.get("football_player")
                if pid and float(m.get("volume_24h_fp") or 0) > 0:
                    out[m["ticker"]] = (pid, custom.get("football_team"))
    return out


def _volume_since(idx: MarketIndex, tickers: list[str], start_ts: int, end_ts: int) -> dict[str, float]:
    def fetch(chunk):
        data = idx.client.get("/trade-api/v2/markets/candlesticks", params={
            "market_tickers": ",".join(chunk), "start_ts": start_ts, "end_ts": end_ts, "period_interval": 1,
        })
        return {
            m["market_ticker"]: sum(float(c.get("volume_fp") or 0) for c in m.get("candlesticks", []))
            for m in data.get("markets", [])
        }

    chunks = [tickers[i:i + BATCH] for i in range(0, len(tickers), BATCH)]
    vols: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for part in pool.map(fetch, chunks):
            vols.update(part)
    return vols


def build_player_volume() -> dict:
    idx = MarketIndex()
    ticker_to_player = _open_player_markets(idx)
    end_ts = int(time.time())
    vols = _volume_since(idx, list(ticker_to_player), end_ts - WINDOW_SECONDS, end_ts)

    by_player: dict[tuple[str, str | None], float] = {}
    for ticker, v in vols.items():
        key = ticker_to_player.get(ticker)
        if key and v > 0:
            by_player[key] = by_player.get(key, 0.0) + v
    names = idx.resolve_targets(list({pid for pid, _ in by_player}))
    # Team is included so the client can fall back to last name + team when
    # Kalshi's name differs from the roster's (e.g. "Joshua" vs "Josh").
    players = []
    for (pid, team_id), v in by_player.items():
        t = names.get(pid)
        if t and t.get("name"):
            players.append({"name": t["name"], "team": idx.team_abbr(team_id) if team_id else None, "vol": round(v)})
    return {"as_of": end_ts, "window_hours": WINDOW_SECONDS // 3600, "players": players}


def player_volume_cached() -> dict:
    with _lock:
        if _cache["data"] is None or time.time() - _cache["ts"] > CACHE_SECONDS:
            _cache["data"] = build_player_volume()
            _cache["ts"] = time.time()
        return _cache["data"]
