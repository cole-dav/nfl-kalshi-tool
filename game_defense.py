"""
Defense view for one game: every Kalshi defensive market (team sacks, forced
turnovers, the opposing QB's INT ladder, individual defender props, safety)
alongside the season-to-date matchup numbers behind them.

Each ladder rung also gets a simple model probability: a Poisson count whose
rate is the defense's per-game rate times the opposing offense's allowed rate
over the league average (both shrunk toward the league mean while the sample
is small). It's a sanity check on the market, not a fitted model.
"""

from __future__ import annotations

import math
import traceback

import nflverse_data as nd
from kalshi_markets import (
    DEF_GAME_SERIES, DEF_PLAYER_PROP_SERIES, DEF_TEAM_SERIES, MarketIndex, PropMarket,
)

# Games of league-average play blended into each team's rate.
PRIOR_GAMES = 4


def _poisson_at_least(lam: float, n: int) -> float:
    if n <= 0:
        return 1.0
    term, cdf = math.exp(-lam), 0.0
    for k in range(n):
        cdf += term
        term *= lam / (k + 1)
    return max(0.0, 1.0 - cdf)


def _shrunk(rate, games, league):
    if rate is None or not games or math.isnan(rate):
        return league
    return (rate * games + league * PRIOR_GAMES) / (games + PRIOR_GAMES)


def _rung(pm: PropMarket, lam: float | None) -> dict:
    q = pm.quote
    n = round(pm.threshold + 0.5) if pm.threshold is not None else None
    return {
        "ticker": pm.ticker, "event_ticker": pm.event_ticker, "series": pm.series,
        "title": q.title, "threshold": pm.threshold, "line": n,
        "yes_bid": q.yes_bid, "yes_ask": q.yes_ask, "implied_prob_yes": q.implied_prob_yes,
        "volume": q.volume, "team_code": pm.team_code,
        "model_prob": round(_poisson_at_least(lam, n), 4) if lam is not None and n is not None else None,
    }


def _ladder(markets: list[PropMarket], lam: float | None) -> list[dict]:
    return [_rung(m, lam) for m in sorted(markets, key=lambda m: m.threshold or 0)]


def _team_rates():
    df = nd.team_defense_rates()
    league = {c: float(df[c].mean()) for c in ("sacks_pg", "takeaways_pg", "ints_pg")}
    return {r["team"]: r for r in df.to_dict("records")}, league, df


def _side(defense: str, offense: str, markets: dict, names: dict, rates, league, df) -> dict:
    d_nv, o_nv = nd.kalshi_to_nflverse_team(defense), nd.kalshi_to_nflverse_team(offense)
    d, o = rates.get(d_nv, {}), rates.get(o_nv, {})

    def lam(def_col, off_col, league_col):
        dr = _shrunk(d.get(def_col), d.get("def_games"), league[league_col])
        orr = _shrunk(o.get(off_col), o.get("off_games"), league[league_col])
        return dr * orr / league[league_col] if league[league_col] else None

    lam_sacks = lam("sacks_pg", "sacks_allowed_pg", "sacks_pg")
    lam_to = lam("takeaways_pg", "giveaways_pg", "takeaways_pg")
    lam_int = lam("ints_pg", "ints_thrown_pg", "ints_pg")

    # Rank 1 = most favorable to this defense on every row.
    def rank(col, team):
        return nd.rank_of(df, col, team, ascending=False)

    stats = [
        {"label": "SACKS / G", "def": rank("sacks_pg", d_nv), "off": rank("sacks_allowed_pg", o_nv),
         "off_label": "SACKS ALLOWED / G"},
        {"label": "TAKEAWAYS / G", "def": rank("takeaways_pg", d_nv), "off": rank("giveaways_pg", o_nv),
         "off_label": "GIVEAWAYS / G"},
        {"label": "INTS / G", "def": rank("ints_pg", d_nv), "off": rank("ints_thrown_pg", o_nv),
         "off_label": "INTS THROWN / G"},
    ]
    try:
        pg = nd.defense_pressure_generated()
        pa = nd.oline_pressure_allowed()
        stats.append({"label": "PRESSURE %", "pct": True,
                      "def": nd.rank_of(pg, "pressure_pct", d_nv, ascending=False),
                      "off": nd.rank_of(pa, "pressure_pct_allowed", o_nv, ascending=False),
                      "off_label": "PRESSURE % ALLOWED"})
    except Exception:
        traceback.print_exc()

    ladders = []
    for series, label in DEF_TEAM_SERIES.items():
        mine = [m for m in markets.get(series, []) if m.team_code == defense]
        if mine:
            ladders.append({"series": series, "label": label,
                            "rows": _ladder(mine, lam_sacks if series == "KXNFLTEAMSACK" else lam_to)})
    # The opposing QB's interceptions are this defense's picks.
    by_qb: dict[str, list[PropMarket]] = {}
    for m in markets.get("KXNFLPASSINT", []):
        if m.team_code == offense and m.player_id:
            by_qb.setdefault(m.player_id, []).append(m)
    for pid, ms in by_qb.items():
        ladders.append({"series": "KXNFLPASSINT", "label": f"{names.get(pid) or 'QB'} INTs thrown",
                        "player_name": names.get(pid), "rows": _ladder(ms, lam_int)})

    players: dict[str, dict] = {}
    for series, label in DEF_PLAYER_PROP_SERIES.items():
        for m in markets.get(series, []):
            if m.team_code != defense or not m.player_id:
                continue
            p = players.setdefault(m.player_id, {"player_name": names.get(m.player_id), "ladders": {}})
            p["ladders"].setdefault(label, []).append(m)
    player_props = [{"player_name": p["player_name"],
                     "ladders": [{"label": lb, "rows": _ladder(ms, None)} for lb, ms in p["ladders"].items()]}
                    for p in players.values()]
    for p in player_props:
        for lad in p["ladders"]:
            for r in lad["rows"]:
                r["player_name"] = p["player_name"]
    for lad in ladders:
        for r in lad["rows"]:
            r["player_name"] = lad.get("player_name")

    try:
        defenders = nd.team_defenders(d_nv)
    except Exception:
        traceback.print_exc()
        defenders = []

    return {
        "defense_team": defense, "offense_team": offense,
        "stats": stats,
        "expected": {"sacks": round(lam_sacks, 2) if lam_sacks else None,
                     "takeaways": round(lam_to, 2) if lam_to else None,
                     "ints": round(lam_int, 2) if lam_int else None},
        "ladders": ladders,
        "player_props": player_props,
        "defenders": defenders,
    }


def build_game_defense(idx: MarketIndex, event_ticker: str, away: str, home: str) -> dict:
    markets = idx.get_game_defense_markets(event_ticker)
    pids = list({m.player_id for ms in markets.values() for m in ms if m.player_id})
    names = {pid: (t or {}).get("name") for pid, t in idx.resolve_targets(pids).items()} if pids else {}
    rates, league, df = _team_rates()

    game = []
    for series, label in DEF_GAME_SERIES.items():
        ms = markets.get(series, [])
        if ms:
            game.append({"series": series, "label": label, "rows": _ladder(ms, None)})

    return {
        "sides": [_side(away, home, markets, names, rates, league, df),
                  _side(home, away, markets, names, rates, league, df)],
        "game": game,
        "league": {k: round(v, 2) for k, v in league.items()},
    }
