"""
Ties Kalshi markets + nflverse data + weather into one player research payload.

resolve_and_build(name) is the main entry point used by server.py.
"""

from __future__ import annotations

from dataclasses import asdict

import nflverse_data as nd
import weather as wx
from kalshi_book import KalshiClient, position_from_kalshi
from kalshi_markets import MarketIndex, WEEKLY_PLAYER_PROP_SERIES, SEASON_PLAYER_PROP_SERIES

SKILL_POSITIONS = {"WR", "TE", "RB"}


class PlayerNotFound(Exception):
    pass


def _quote_dict(pm) -> dict:
    q = pm.quote
    return {
        "ticker": pm.ticker,
        "series": pm.series,
        "stat": pm.stat_label,
        "title": q.title,
        "threshold": pm.threshold,
        "yes_bid": q.yes_bid,
        "yes_ask": q.yes_ask,
        "last_price": q.last_price,
        "implied_prob_yes": q.implied_prob_yes,
        "volume": q.volume,
        "open_interest": q.open_interest,
        "kind": pm.kind,
        "team_code": pm.team_code,
        "player_id": pm.player_id,
    }


def _attach_positions(markets: list[dict], my_positions: dict[str, dict]) -> None:
    for m in markets:
        pos = my_positions.get(m["ticker"])
        m["my_position"] = pos


def build_my_positions() -> dict[str, dict]:
    try:
        client = KalshiClient()
        raw_positions = client.get_positions()
    except Exception as e:
        return {}
    out = {}
    for raw in raw_positions:
        pos = position_from_kalshi(raw)
        if pos.contracts == 0:
            continue
        out[pos.ticker] = {
            "side": pos.side,
            "contracts": pos.contracts,
            "avg_price_cents": round(pos.avg_price_cents, 2),
        }
    return out


def resolve_and_build(name: str) -> dict:
    roster_row = nd.find_player_in_roster(name=name)
    if roster_row is None:
        raise PlayerNotFound(f"No active NFL player matching '{name}' found in current roster.")

    team_nflverse = roster_row["team"]
    team_kalshi = nd.nflverse_to_kalshi_team(team_nflverse)
    gsis_id = roster_row["gsis_id"]
    sportradar_id = roster_row.get("sportradar_id")
    position = roster_row["position"]

    my_positions = build_my_positions()

    idx = MarketIndex()
    game = idx.find_game_for_team(team_kalshi)

    markets_section = _build_markets_section(idx, game, team_kalshi, sportradar_id, roster_row, my_positions)

    next_game = nd.get_team_next_game(team_nflverse)
    opponent = next_game["opponent"] if next_game else None

    defense_section = _build_defense_section(opponent) if opponent else None
    team_context_section = _build_team_context_section(team_nflverse, gsis_id, position)
    environment_section = _build_environment_section(next_game) if next_game else None
    history_section = _build_history_section(gsis_id, opponent) if opponent else None
    correlation_section = _build_correlation_section(markets_section)
    try:
        season_pace = nd.player_season_pace(gsis_id, team_nflverse)
    except Exception:
        season_pace = None

    return {
        "player": {
            "name": roster_row["full_name"],
            "position": position,
            "team": team_nflverse,
            "team_kalshi": team_kalshi,
            "jersey_number": roster_row.get("jersey_number"),
            "gsis_id": gsis_id,
            "sportradar_id": sportradar_id,
            "headshot_url": roster_row.get("headshot_url"),
        },
        "opponent": opponent,
        "markets": markets_section,
        "opponent_defense": defense_section,
        "team_context": team_context_section,
        "game_environment": environment_section,
        "history_vs_opponent": history_section,
        "correlations": correlation_section,
        "season_pace": season_pace,
    }


def _build_markets_section(idx: MarketIndex, game, team_kalshi, sportradar_id, roster_row, my_positions) -> dict:
    if not game:
        return {"note": "No Kalshi game/props found for this team's upcoming matchup.", "player_props": [],
                "teammate_props": [], "team_game_markets": [], "season_props": []}

    suffix = game["event_ticker"].split("-", 1)[1]
    all_props = idx.get_all_prop_markets_for_game(suffix)

    player_ids = list({p.player_id for p in all_props if p.player_id})
    resolved = idx.resolve_targets(player_ids)

    def target_matches_our_player(target: dict | None) -> bool:
        if not target:
            return False
        sids = target.get("source_ids", {})
        return sportradar_id and (
            target.get("source_id") == sportradar_id or sids.get("source_3_id") == sportradar_id
        )

    own_player_id = None
    for pid, t in resolved.items():
        if target_matches_our_player(t):
            own_player_id = pid
            break

    player_props, teammate_props, team_game_markets = [], [], []
    for p in all_props:
        if p.kind == "player" and p.player_id:
            target = resolved.get(p.player_id)
            pname = target["name"] if target else None
            entry = _quote_dict(p)
            entry["player_name"] = pname
            if p.player_id == own_player_id:
                player_props.append(entry)
            elif p.team_code == team_kalshi:
                teammate_props.append(entry)
        else:
            team_game_markets.append(_quote_dict(p))

    for group in (player_props, teammate_props, team_game_markets):
        _attach_positions(group, my_positions)

    season_props = []
    if own_player_id:
        season_markets = idx.get_season_prop_markets_for_player(own_player_id)
        season_props = [_quote_dict(p) for p in season_markets]
        _attach_positions(season_props, my_positions)

    return {
        "event_ticker": game["event_ticker"],
        "matchup": game["sub_title"],
        "player_props": player_props,
        "teammate_props": teammate_props,
        "team_game_markets": team_game_markets,
        "season_props": season_props,
    }


def _build_defense_section(opponent: str) -> dict:
    coord = nd.load_coordinators().get(opponent, {})
    dropback = nd.defense_epa_per_dropback()
    rush = nd.defense_rush_epa_allowed()
    pressure = nd.defense_pressure_generated()
    blitz = nd.defense_blitz_rate_ftn()

    def row_for(df, team_col="team"):
        r = df[df[team_col] == opponent]
        return r.iloc[0].to_dict() if not r.empty else {}

    return {
        "team": opponent,
        "dc_name": coord.get("dc"),
        # lower EPA allowed = better defense -> ascending=True
        "epa_per_dropback_allowed": row_for(dropback).get("epa_per_dropback"),
        "epa_per_dropback_allowed_rank": nd.rank_of(dropback, "epa_per_dropback", opponent, ascending=True),
        "epa_per_rush_allowed": row_for(rush).get("epa_per_rush"),
        "epa_per_rush_allowed_rank": nd.rank_of(rush, "epa_per_rush", opponent, ascending=True),
        # more pressure generated = better defense -> ascending=False
        "pressure_pct_generated": row_for(pressure).get("pressure_pct"),
        "pressure_pct_generated_rank": nd.rank_of(pressure, "pressure_pct", opponent, ascending=False),
        "sacks": row_for(pressure).get("sacks"),
        "blitz_rate": row_for(blitz).get("blitz_rate"),
        "avg_pass_rushers": row_for(blitz).get("avg_pass_rushers"),
        "coverage_scheme_note": (
            "nflverse's free FTN charting feed does not include man/zone coverage rate "
            "(that's part of FTN's paid product). Blitz rate and pass-rush count above are "
            "the closest public proxy for defensive scheme aggressiveness."
        ),
    }


def _build_team_context_section(team: str, gsis_id: str, position: str) -> dict:
    oline = nd.oline_pressure_allowed()
    pace = nd.team_pace()

    def row_for(df):
        r = df[df["team"] == team]
        return r.iloc[0].to_dict() if not r.empty else {}

    result = {
        "team": team,
        # lower pressure/sacks allowed = better O-line -> ascending=True
        "pressure_pct_allowed": row_for(oline).get("pressure_pct_allowed"),
        "pressure_pct_allowed_rank": nd.rank_of(oline, "pressure_pct_allowed", team, ascending=True),
        "sacks_allowed": row_for(oline).get("sacks_allowed"),
        "sacks_allowed_rank": nd.rank_of(oline, "sacks_allowed", team, ascending=True),
        "plays_per_game": row_for(pace).get("plays_per_game"),
    }
    if position in SKILL_POSITIONS:
        ts = nd.target_share(gsis_id)
        result["target_share_by_week"] = ts.to_dict(orient="records")
        if len(ts) > 1:
            result["target_share_stdev"] = {
                "targets": round(float(ts["targets"].std()), 2),
                "target_share": round(float(ts["target_share"].std()), 4),
                "air_yards_share": round(float(ts["air_yards_share"].std()), 4),
                "wopr": round(float(ts["wopr"].std()), 4),
            }
    return result


def _build_environment_section(next_game: dict) -> dict:
    stadiums = nd.load_stadiums()
    home_team = next_game["team"] if next_game["is_home"] else next_game["opponent"]
    away_team = next_game["opponent"] if next_game["is_home"] else next_game["team"]
    home_team_kalshi = nd.nflverse_to_kalshi_team(home_team)
    stadium = stadiums.get(home_team_kalshi, {})

    forecast = None
    if stadium:
        forecast = wx.get_game_weather(
            stadium["lat"], stadium["lon"], next_game["gameday"], next_game.get("gametime"),
            next_game.get("roof") or stadium.get("roof"),
        )

    return {
        "home_team": home_team,
        "away_team": away_team,
        "is_home_for_searched_team": next_game["is_home"],
        "week": next_game.get("week"),
        "gameday": next_game.get("gameday"),
        "gametime": next_game.get("gametime"),
        "rest_days": next_game["rest_days"],
        "opponent_rest_days": next_game["opponent_rest_days"],
        "roof": next_game.get("roof"),
        "surface": next_game.get("surface"),
        "stadium": next_game.get("stadium"),
        "spread_line": next_game.get("spread_line"),
        "total_line": next_game.get("total_line"),
        "forecast": forecast,
        "injuries_home": nd.team_injuries(home_team).to_dict(orient="records"),
        "injuries_away": nd.team_injuries(away_team).to_dict(orient="records"),
    }


def _build_history_section(gsis_id: str, opponent: str) -> list[dict]:
    hist = nd.player_vs_opponent_history(gsis_id, opponent)
    return hist.to_dict(orient="records")


def _closest_to_coinflip(markets: list[dict]) -> dict | None:
    """Pick the single threshold whose implied YES probability is nearest 50%,
    as the one representative line for a player+stat family."""
    priced = [m for m in markets if m.get("implied_prob_yes") is not None]
    if not priced:
        return None
    return min(priced, key=lambda m: abs(m["implied_prob_yes"] - 0.5))


def _build_correlation_section(markets_section: dict) -> dict:
    """Structural correlation flags: WR/TE/RB 'over' props correlate with the
    team's QB passing 'over' props and that team's team-total 'over' market.

    To stay readable, each correlated player+stat family is collapsed to one
    representative threshold (closest to a coinflip) rather than listing
    every strike price."""
    player_props = markets_section.get("player_props", [])
    teammate_props = markets_section.get("teammate_props", [])
    team_game_markets = markets_section.get("team_game_markets", [])

    def family_key(m):
        return (m.get("player_name"), m["series"])

    def representative_by_family(markets: list[dict]) -> list[dict]:
        groups: dict[tuple, list[dict]] = {}
        for m in markets:
            groups.setdefault(family_key(m), []).append(m)
        reps = [_closest_to_coinflip(ms) for ms in groups.values()]
        return [r for r in reps if r]

    # Candidate correlated legs always come from TEAMMATES, never the searched
    # player's own other props (that would just be self-correlation noise).
    team_total_overs = representative_by_family(
        [m for m in team_game_markets if m["series"] == "KXNFLTEAMTOTAL"]
    )
    qb_reps = representative_by_family(
        [m for m in teammate_props if m["series"] in ("KXNFLPASSYDS", "KXNFLPASSTDS")]
    )
    skill_reps = representative_by_family(
        [m for m in teammate_props if m["series"] in ("KXNFLREC", "KXNFLRECYDS")]
    )

    searched_families = representative_by_family(player_props)

    flags = []
    for m in searched_families:
        related = []
        if m["series"] in ("KXNFLREC", "KXNFLRECYDS", "KXNFLTD"):
            related.extend({"reason": "same-team QB passing over", **q} for q in qb_reps if q["ticker"] != m["ticker"])
        if m["series"] in ("KXNFLPASSYDS", "KXNFLPASSTDS"):
            related.extend({"reason": "same-team pass-catcher over", **q} for q in skill_reps if q["ticker"] != m["ticker"])
        related.extend({"reason": "same-team total over", "player_name": None, **q} for q in team_total_overs)
        if related:
            flags.append({"ticker": m["ticker"], "player_name": m.get("player_name"), "stat": m["stat"], "correlated_with": related})
    return {"flags": flags}
