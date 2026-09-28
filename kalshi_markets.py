"""
Kalshi NFL market discovery and player/team resolution.

Ticker grammar (reverse-engineered from live data, confirmed against the API):

  event ticker  = "{SERIES}-{YY}{MON}{DD}{AWAY}{HOME}"
                  e.g. KXNFLPASSYDS-26SEP27CARCLE  (Carolina @ Cleveland, Sep 27)

  player market = "{event_ticker}-{TEAM}{SLUG}{JERSEY}-{THRESHOLD}"
                  e.g. KXNFLPASSYDS-26SEP27CARCLE-CARBYOUNG9-150
                  ("Bryce Young 150+ passing yards")

  team market   = "{event_ticker}-{TEAM}{THRESHOLD}"  (team total, spread)
                  or "{event_ticker}-{TEAM}"           (moneyline)
                  or "{event_ticker}-{THRESHOLD}"      (game total, no team)

  season player = "{SERIES}-{SEASON_CODE}{THRESHOLD}-{SLUG}{JERSEY}"
                  e.g. KXNFLSEASONPASSYDS-27C3000-JALLEN17

Every player/team market also carries `custom_strike.football_player` /
`custom_strike.football_team`, a Kalshi-internal UUID. That UUID resolves via
GET /trade-api/v2/structured_targets/{id} to the canonical name, team, jersey,
and position -- this is what we use to identify players, not the ticker slug,
since slugs are lossy (initials, truncation, apostrophes).
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from kalshi_book import KalshiClient, public_get, public_get_events

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")

# Weekly (per-game) player prop series.
WEEKLY_PLAYER_PROP_SERIES: dict[str, str] = {
    "KXNFLPASSYDS": "Passing Yards",
    "KXNFLPASSTDS": "Passing TDs",
    "KXNFLPASSCOMP": "Pass Completions",
    "KXNFLPASSATT": "Pass Attempts",
    "KXNFLPASSINT": "Pass Interceptions",
    "KXNFLRSHYDS": "Rushing Yards",
    "KXNFLRSHATT": "Rush Attempts",
    "KXNFLREC": "Receptions",
    "KXNFLRECYDS": "Receiving Yards",
    "KXNFLTD": "Anytime TD",
    "KXNFLLONGREC": "Longest Reception",
    "KXNFLLONGRSH": "Longest Rush",
}

# Season-long player prop series (threshold events, not per-game).
SEASON_PLAYER_PROP_SERIES: dict[str, str] = {
    "KXNFLSEASONPASSYDS": "Season Passing Yards",
    "KXNFLSEASONRECYDS": "Season Receiving Yards",
    "KXNFLSEASONRSHYDS": "Season Rushing Yards",
    "KXNFLSEASONREC": "Season Receptions",
    "KXNFLSEASONPASSTDS": "Season Passing TDs",
    "KXNFLSEASONRSHTD": "Season Rushing TDs",
    "KXNFLSEASONRECTD": "Season Receiving TDs",
}

# Game/team-level series (one event per matchup).
TEAM_GAME_SERIES: dict[str, str] = {
    "KXNFLGAME": "Moneyline",
    "KXNFLSPREAD": "Spread",
    "KXNFLTOTAL": "Game Total",
    "KXNFLTEAMTOTAL": "Team Total",
}

# Exotic/combo game-level series -- one event per matchup, same as
# TEAM_GAME_SERIES, EXCEPT KXNFLRACE which has one event per (matchup,
# point-threshold) and is handled separately in get_game_combos().
GAME_COMBO_SERIES: dict[str, str] = {
    "KXNFL1H": "1st Half Winner",
    "KXNFL1HSPREAD": "1st Half Spread",
    "KXNFL1HTOTAL": "1st Half Total",
    "KXNFL1HFT": "1st Half / Final",
    "KXNFL1Q": "1st Quarter Winner",
    "KXNFLBOTH": "Both Teams Score",
    "KXNFLOT": "Overtime?",
    "KXNFLFIRSTTDTEAM": "First TD Scorer",
}
RACE_SERIES = "KXNFLRACE"


def _cache_path(name: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, name)


def _load_json_cache(name: str) -> dict:
    path = _cache_path(name)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def _save_json_cache(name: str, data: dict) -> None:
    with open(_cache_path(name), "w") as f:
        json.dump(data, f)


@dataclass
class MarketQuote:
    ticker: str
    title: str
    yes_bid: float | None
    yes_ask: float | None
    last_price: float | None
    volume: float
    open_interest: float

    @property
    def implied_prob_yes(self) -> float | None:
        """Mid-market implied probability, falling back to last trade."""
        if self.yes_bid is not None and self.yes_ask is not None:
            return round((self.yes_bid + self.yes_ask) / 2, 4)
        return self.last_price

    @staticmethod
    def from_market(m: dict) -> "MarketQuote":
        def dollars(key):
            v = m.get(key)
            return float(v) if v not in (None, "") else None

        return MarketQuote(
            ticker=m["ticker"],
            title=m.get("title", ""),
            yes_bid=dollars("yes_bid_dollars"),
            yes_ask=dollars("yes_ask_dollars"),
            last_price=dollars("last_price_dollars"),
            volume=float(m.get("volume_fp") or 0),
            open_interest=float(m.get("open_interest_fp") or 0),
        )


@dataclass
class PropMarket:
    ticker: str
    series: str
    stat_label: str
    event_ticker: str
    kind: str  # "player" | "team" | "game"
    team_code: str | None
    player_id: str | None
    threshold: float | None
    quote: MarketQuote
    raw: dict = field(repr=False, default_factory=dict)


class MarketIndex:
    """Fetches & caches this week's NFL markets, and resolves player/team UUIDs.

    With no client, every read goes through Kalshi's unauthenticated public
    endpoints -- odds/matchups/injuries browsing needs no Kalshi key at all.
    Pass a signed `client` (e.g. a per-session one) to read as that account
    instead; nothing here requires it."""

    def __init__(self, client: KalshiClient | None = None):
        self.client = client
        self._structured_target_cache = _load_json_cache("structured_targets.json")

    # -- signed-or-public read helpers --------------------------------------
    # Public (no leading underscore): other modules that need a raw read
    # (e.g. player_volume.py's candlestick lookups) should go through these
    # rather than reaching into `.client` directly, since that may be None.

    def get(self, path: str, params: dict | None = None) -> dict:
        if self.client:
            return self.client.get(path, params=params)
        return public_get(path, params=params)

    def get_events(self, series_ticker: str | None = None, status: str | None = None) -> list[dict]:
        if self.client:
            return self.client.get_events(series_ticker=series_ticker, status=status)
        return public_get_events(series_ticker=series_ticker, status=status)

    # -- structured target (player/team) resolution ------------------------

    def resolve_target(self, target_id: str) -> dict | None:
        if target_id in self._structured_target_cache:
            return self._structured_target_cache[target_id]
        try:
            data = self.get(f"/trade-api/v2/structured_targets/{target_id}")
        except Exception:
            return None
        target = data.get("structured_target")
        if target:
            self._structured_target_cache[target_id] = target
        return target

    def resolve_targets(self, target_ids: list[str], max_workers: int = 8) -> dict[str, dict]:
        missing = [t for t in set(target_ids) if t not in self._structured_target_cache]
        if missing:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(self.resolve_target, tid): tid for tid in missing}
                for fut in as_completed(futures):
                    fut.result()
            _save_json_cache("structured_targets.json", self._structured_target_cache)
        return {tid: self._structured_target_cache.get(tid) for tid in target_ids}

    def player_name(self, player_id: str) -> str | None:
        t = self.resolve_target(player_id)
        return t["name"] if t else None

    def team_abbr(self, team_id: str) -> str | None:
        t = self.resolve_target(team_id)
        return t["details"]["abbreviation"] if t and t.get("details") else None

    # -- this week's slate ---------------------------------------------------

    def get_week_games(self) -> list[dict]:
        """Authoritative list of this week's matchups from the moneyline series.

        Returns dicts: event_ticker, away, home, close_time (kickoff-ish), title
        """
        events = self.get_events(series_ticker="KXNFLGAME", status="open")
        games = []
        for ev in events:
            markets = ev.get("markets", [])
            if not markets:
                continue
            # two markets per game: "{event}-{AWAY}" and "{event}-{HOME}"
            teams = [m["ticker"].rsplit("-", 1)[-1] for m in markets]
            sub = ev.get("sub_title", "")
            away, home = None, None
            if " vs " in sub:
                left, right = sub.split(" vs ", 1)
                away = left.strip()
                home = right.split("(")[0].strip()
            games.append(
                {
                    "event_ticker": ev["event_ticker"],
                    "sub_title": sub,
                    "away": away,
                    "home": home,
                    "team_codes": teams,
                    "close_time": min((m.get("close_time") for m in markets), default=None),
                }
            )
        return games

    def get_current_week_games(self) -> list[dict]:
        """Kalshi keeps next week's slate open early too, so narrow to the
        nearest week: every game whose kickoff falls within 6 days of the
        earliest open kickoff."""
        games = [g for g in self.get_week_games() if g["close_time"]]
        if not games:
            return []
        games.sort(key=lambda g: g["close_time"])
        earliest = games[0]["close_time"]
        from datetime import datetime, timedelta

        earliest_dt = datetime.fromisoformat(earliest.replace("Z", "+00:00"))
        cutoff = earliest_dt + timedelta(days=6)
        return [
            g
            for g in games
            if datetime.fromisoformat(g["close_time"].replace("Z", "+00:00")) <= cutoff
        ]

    def find_game_for_team(self, team_code: str, current_week_only: bool = True) -> dict | None:
        games = self.get_current_week_games() if current_week_only else self.get_week_games()
        for g in games:
            if team_code in g["team_codes"]:
                return g
        return None

    # -- per-game props --------------------------------------------------------

    def get_event_markets(self, event_ticker: str) -> list[dict]:
        data = self.get(
            "/trade-api/v2/events/" + event_ticker, params={"with_nested_markets": True}
        )
        return data.get("event", {}).get("markets", [])

    def get_all_prop_markets_for_game(
        self, game_event_suffix: str, series_map: dict[str, str] = WEEKLY_PLAYER_PROP_SERIES
    ) -> list[PropMarket]:
        """game_event_suffix: the part after the series ticker, e.g. '26SEP27CARCLE'."""
        out: list[PropMarket] = []
        for series, label in {**series_map, **TEAM_GAME_SERIES}.items():
            event_ticker = f"{series}-{game_event_suffix}"
            try:
                markets = self.get_event_markets(event_ticker)
            except Exception:
                continue
            for m in markets:
                out.append(self._to_prop_market(m, series, label))
        return out

    def _to_prop_market(self, m: dict, series: str, label: str) -> PropMarket:
        custom = m.get("custom_strike") or {}
        player_id = custom.get("football_player")
        team_id = custom.get("football_team")
        kind = "player" if player_id else ("team" if team_id or series in TEAM_GAME_SERIES else "game")
        threshold = m.get("floor_strike") if m.get("floor_strike") is not None else m.get("cap_strike")
        team_code = None
        if team_id:
            team_code = self.team_abbr(team_id)
        return PropMarket(
            ticker=m["ticker"],
            series=series,
            stat_label=label,
            event_ticker=m.get("event_ticker", ""),
            kind=kind,
            team_code=team_code,
            player_id=player_id,
            threshold=threshold,
            quote=MarketQuote.from_market(m),
            raw=m,
        )

    def get_season_prop_markets_for_player(self, player_id: str) -> list[PropMarket]:
        out = []
        for series, label in SEASON_PLAYER_PROP_SERIES.items():
            events = self.get_events(series_ticker=series, status="open")
            for ev in events:
                for m in ev.get("markets", []):
                    custom = m.get("custom_strike") or {}
                    if custom.get("football_player") == player_id:
                        out.append(self._to_prop_market(m, series, label))
        return out

    # -- week overview / game detail (Slate module) ---------------------------

    def get_game_core_markets(self, game: dict) -> dict:
        """Moneyline/spread/total/team-total ref lines for one game, for the
        week-overview list. `game` is one entry from get_current_week_games()."""
        suffix = game["event_ticker"].split("-", 1)[1]
        out: dict[str, Any] = {
            "event_ticker": game["event_ticker"],
            "away": game["away"],
            "home": game["home"],
            "kickoff": game["close_time"],
            "moneyline": {}, "spread": None, "total": None, "team_totals": {},
        }
        for series, label in TEAM_GAME_SERIES.items():
            event_ticker = f"{series}-{suffix}"
            try:
                markets = [self._to_prop_market(m, series, label) for m in self.get_event_markets(event_ticker)]
            except Exception:
                continue
            if series == "KXNFLGAME":
                for m in markets:
                    if m.team_code == game["away"]:
                        out["moneyline"]["away"] = _quote(m)
                    elif m.team_code == game["home"]:
                        out["moneyline"]["home"] = _quote(m)
            elif series == "KXNFLSPREAD":
                ref = _pick_ref(markets)
                if ref:
                    out["spread"] = {"team": ref.team_code, "line": ref.threshold, **_quote(ref)}
            elif series == "KXNFLTOTAL":
                ref = _pick_ref(markets)
                if ref:
                    out["total"] = {"line": ref.threshold, **_quote(ref)}
            elif series == "KXNFLTEAMTOTAL":
                for code in (game["away"], game["home"]):
                    ref = _pick_ref([m for m in markets if m.team_code == code])
                    if ref:
                        out["team_totals"][code] = {"line": ref.threshold, **_quote(ref)}
        return out

    def get_game_combos(self, event_ticker: str) -> list[dict]:
        """Exotic/combo markets for one game (race-to-N, 1H/FT, 1Q winner, both
        teams score, OT, ...), each reduced to a small representative set."""
        suffix = event_ticker.split("-", 1)[1]

        def fetch_one(item):
            series, label = item
            try:
                markets = [self._to_prop_market(m, series, label) for m in self.get_event_markets(f"{series}-{suffix}")]
            except Exception:
                return None
            if not markets:
                return None
            return {
                "series": series, "label": label,
                "outcomes": [{"title": m.raw.get("title"), **_quote(m)} for m in markets],
            }

        with ThreadPoolExecutor(max_workers=len(GAME_COMBO_SERIES)) as pool:
            combos = [c for c in pool.map(fetch_one, GAME_COMBO_SERIES.items()) if c]
        # KXNFLRACE has one event per (game, point-threshold) instead of one
        # event per game, so it needs its own listing.
        try:
            race_events = [
                ev for ev in self.get_events(series_ticker=RACE_SERIES, status="open")
                if ev["event_ticker"].startswith(f"{RACE_SERIES}-{suffix}-")
            ]
        except Exception:
            race_events = []
        race_thresholds = []
        for ev in sorted(race_events, key=lambda e: int(e["event_ticker"].rsplit("-", 1)[-1])):
            threshold = ev["event_ticker"].rsplit("-", 1)[-1]
            markets = [self._to_prop_market(m, RACE_SERIES, "Race to " + threshold) for m in ev.get("markets", [])]
            race_thresholds.append({
                "threshold": int(threshold),
                "outcomes": [{"title": m.raw.get("title"), **_quote(m)} for m in markets],
            })
        if race_thresholds:
            combos.append({"series": RACE_SERIES, "label": "Race to N Points", "thresholds": race_thresholds})
        return combos


def _quote(pm: PropMarket) -> dict:
    q = pm.quote
    return {
        "ticker": pm.ticker,
        "yes_bid": q.yes_bid, "yes_ask": q.yes_ask,
        "implied_prob_yes": q.implied_prob_yes,
        "volume": q.volume,
        "team_code": pm.team_code,
    }


def _pick_ref(markets: list[PropMarket]) -> PropMarket | None:
    """The market whose implied YES probability is closest to a coinflip."""
    priced = [m for m in markets if m.quote.implied_prob_yes is not None]
    if not priced:
        return None
    return min(priced, key=lambda m: abs(m.quote.implied_prob_yes - 0.5))


if __name__ == "__main__":
    idx = MarketIndex()
    games = idx.get_week_games()
    print(f"{len(games)} games this week:")
    for g in games:
        print(" ", g["event_ticker"], g["sub_title"])
