"""
Bet recommendation engine: slate-wide edges, plus a Claude chat layer that
drives the quant engine through tools and proposes bets the user can accept.

Chat design:
  * Official `anthropic` SDK, manual tool loop (capped at MAX_TOOL_ITERS),
    `claude-opus-5` with adaptive thinking, effort medium, server-side refusal
    fallback (`fallbacks="default"` + beta `server-side-fallback-2026-07-01`).
  * The system prompt and tool list are frozen (prompt caching); the user's
    slip (locks / excludes / scenarios / odds mode) rides in each user turn.
  * Conversations live in memory keyed by conversation_id, storing the full
    `response.content` of every assistant turn so thinking and tool blocks
    replay unchanged.
  * propose_bets is enforced server-side too: excluded legs are dropped and
    locked legs added before pricing, and the model is told what changed.
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import fair_value as fv
import prop_model as pm
import scenario_sim as ss

MODEL = "claude-opus-5"
MAX_TOKENS = 16000
MAX_TOOL_ITERS = 8
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_CONVERSATIONS = 200


class EngineError(ValueError):
    """Bad input -> HTTP 400."""


class ChatUnavailable(RuntimeError):
    """No Anthropic credentials -> HTTP 503."""


# -- slate-wide edges -------------------------------------------------------------------


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def event_edges(ev, min_edge: float, kind: str) -> tuple[list[dict], list[dict]]:
    list_all = min_edge <= -0.5
    if ev.in_play and not list_all:
        return [], []
    rows: list[dict] = []
    if kind in ("game", "all"):
        rows += fv.game_edges(ev.markets, ev.model, ev.matchup, min_edge)
    if kind in ("prop", "all"):
        try:
            rows += pm.prop_edges(ev, min_edge)
        except Exception:
            traceback.print_exc()
    if list_all:
        # every market must appear (the UI derives strike lists from this)
        seen = {r["ticker"] for r in rows}
        for m in ev.markets:
            if m["ticker"] in seen:
                continue
            if (kind == "game" and m["kind"] == "player") or (kind == "prop" and m["kind"] != "player"):
                continue
            rows.append({
                "ticker": m["ticker"], "side": "yes", "event_ticker": m["event_ticker"], "matchup": ev.matchup,
                "title": m["title"], "kind": m["kind"], "family": m["family"], "team_code": m.get("team_code"),
                "player_name": m.get("player_name"), "threshold": m.get("threshold"),
                "fair": None, "cost": fv.side_cost(m, "yes"), "edge": None, "ev_per_dollar": None,
                "american_cost": fv.american(fv.side_cost(m, "yes")), "american_fair": None,
                "confidence": "low", "reasons": ["no quote or no model price"],
                "yes_bid": m.get("yes_bid"), "yes_ask": m.get("yes_ask"), "volume": m.get("volume"),
            })
    if ev.in_play:
        for r in rows:
            r["confidence"] = "low"
            r["reasons"] = ["game in progress: pregame model, ignore"] + r["reasons"]
    arbs = [] if ev.in_play else fv.ladder_arbs(ev.markets, ev.away, ev.home, ev.event_ticker)[0]
    return rows, arbs


def edges(event: str | None = None, min_edge: float = 0.03, kind: str = "all") -> dict:
    kind = (kind or "all").lower()
    if kind not in ("game", "prop", "all"):
        raise EngineError("kind must be game, prop or all")
    if event:
        suffixes = [fv.game_suffix(event)]
    else:
        suffixes = [fv.game_suffix(g["event_ticker"]) for g in fv.current_games()]
    with_props = kind != "game"

    def one(sfx):
        try:
            ev = fv.load_event(sfx, with_props=with_props)
            return event_edges(ev, min_edge, kind) + (ev.as_of, None)
        except ValueError as e:
            return [], [], time.time(), str(e)
        except Exception as e:
            traceback.print_exc()
            return [], [], time.time(), f"{sfx}: {e}"

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(suffixes)))) as pool:
        results = list(pool.map(one, suffixes))
    if event and results and results[0][3] and not results[0][0]:
        raise EngineError(results[0][3])
    rows = [r for res in results for r in res[0]]
    arbs = [a for res in results for a in res[1]]
    rows.sort(key=lambda r: (r["ev_per_dollar"] is None, -(r["ev_per_dollar"] or 0)))
    out = {"as_of": _iso(min((r[2] for r in results), default=time.time())), "edges": rows, "arbs": arbs}
    errs = [r[3] for r in results if r[3]]
    if errs:
        out["errors"] = errs
    return out


# -- tools ------------------------------------------------------------------------------

_CONSTRAINT = {
    "type": "object",
    "description": "One condition. Use exactly one of: {team, margin_gte} | {team, margin_lte} | {total_gte} | "
                   "{total_lte} | {team, points_gte} | {team, points_lte} | {player, series, gte} | {player, series, lte}. "
                   "margin is that team's final margin (points for - against). series is a prop family like RECYDS.",
    "properties": {
        "team": {"type": "string"}, "margin_gte": {"type": "number"}, "margin_lte": {"type": "number"},
        "total_gte": {"type": "number"}, "total_lte": {"type": "number"},
        "points_gte": {"type": "number"}, "points_lte": {"type": "number"},
        "player": {"type": "string"}, "series": {"type": "string"},
        "gte": {"type": "number"}, "lte": {"type": "number"},
    },
    "additionalProperties": False,
}
_SCENARIO = {
    "type": "object",
    "properties": {"event": {"type": "string", "description": "KXNFLGAME-<suffix>"},
                   "constraints": {"type": "array", "items": _CONSTRAINT}},
    "required": ["event", "constraints"],
    "additionalProperties": False,
}
_LEG = {
    "type": "object",
    "properties": {"market_ticker": {"type": "string"}, "side": {"type": "string", "enum": ["yes", "no"]}},
    "required": ["market_ticker", "side"],
    "additionalProperties": False,
}


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"name": name, "description": description, "strict": True,
            "input_schema": {"type": "object", "properties": properties, "required": required,
                             "additionalProperties": False}}


TOOLS = [
    _tool("list_edges",
          "Ranked mispriced Kalshi markets (fair - cost >= min_edge) and hard ladder arbs. Omit event for the whole "
          "slate. kind: game (ML/spread/total/team totals), prop (player props) or all.",
          {"event": {"type": "string"}, "min_edge": {"type": "number"},
           "kind": {"type": "string", "enum": ["game", "prop", "all"]}, "limit": {"type": "integer"}}, []),
    _tool("price_market", "Fair value vs cost for one side of one market, with American odds.",
          {"market_ticker": {"type": "string"}, "side": {"type": "string", "enum": ["yes", "no"]}},
          ["market_ticker", "side"]),
    _tool("price_parlay",
          "Correlated joint fair price for several legs (same-game legs priced jointly from the simulation), vs the "
          "independent product and the product of costs. Optional scenario conditions one game on a user view.",
          {"legs": {"type": "array", "items": _LEG}, "scenario": _SCENARIO}, ["legs"]),
    _tool("simulate_scenario",
          "Re-price every market in one game assuming the constraints hold. Returns P(scenario) and the markets "
          "whose fair value moves most, with conditional edge vs cost.",
          {"event": {"type": "string"}, "constraints": {"type": "array", "items": _CONSTRAINT},
           "limit": {"type": "integer"}}, ["event", "constraints"]),
    _tool("build_ladder",
          "Same-team stack of singles (e.g. SEA ML / -3.5 / -6.5), priced jointly, with P&L by final-margin bucket. "
          "family SPREAD with strike 0 = moneyline; TEAMTOTAL strikes are points thresholds.",
          {"event": {"type": "string"}, "team": {"type": "string"},
           "family": {"type": "string", "enum": ["ML", "SPREAD", "TEAMTOTAL"]},
           "strikes": {"type": "array", "items": {"type": "number"}},
           "stakes": {"type": "array", "items": {"type": "number"}}, "scenario": _SCENARIO},
          ["event", "team", "family", "strikes"]),
    _tool("get_game_context",
          "Model line/total/implied points, nflverse lines, injuries, weather, records and ladder arbs for one game.",
          {"event": {"type": "string"}}, ["event"]),
    _tool("propose_bets",
          "Show concrete bet proposals to the user as cards they can accept. kind: single, parlay (legs combined) or "
          "ladder (same-team stack of singles). The server prices every leg and enforces the slip's locks/excludes.",
          {"proposals": {"type": "array", "items": {
              "type": "object",
              "properties": {
                  "title": {"type": "string"},
                  "kind": {"type": "string", "enum": ["single", "parlay", "ladder"]},
                  "rationale": {"type": "string"},
                  "legs": {"type": "array", "items": _LEG},
                  "scenario": _SCENARIO,
              },
              "required": ["title", "kind", "rationale", "legs"],
              "additionalProperties": False,
          }}}, ["proposals"]),
]

SYSTEM_PROMPT = """You are the betting assistant inside an NFL research tool for Kalshi prediction markets. Your job is to find mispriced Kalshi markets and help the user build bets: singles, same-game parlays, and same-team ladders (e.g. SEA ML / -3.5 / -6.5).

How the engine prices things
- Fair value comes from a quant model: a key-number-aware margin/total distribution fitted to the Kalshi ladder (lightly blended with the sportsbook line), player prop distributions blended between a projection model and the Kalshi ladder, and a joint simulation that correlates legs through game script (e.g. a QB's passing yards and his WR's receiving yards move together; a team that leads runs more).
- Cost to buy YES is the ask; cost to buy NO is 1 - yes_bid. Edge = fair - cost. EV per $1 = fair / cost - 1. Prices are dollars per $1 contract.
- Every market resolves YES only if the stat is strictly greater than its strike ("N+" props mean at least N). Spreads: "SEA -6.5" = SEA wins by 7 or more.
- Kalshi combos allow at most one game-level leg (ML, spread, total, team total) per game, and can't span games. A same-team ladder is therefore a set of singles priced jointly by the model, not a combo. Cross-game parlays get a model price but can't be quoted as one Kalshi combo.
- Kalshi taker fees (~0.07*p*(1-p) per contract) are not in the edges; treat edges under ~2 cents as noise. Low-confidence rows (thin volume, wide spreads, little player history) deserve skepticism. Games already in progress are priced with the pregame model and should not be bet from it.

Tools
- Always get numbers from the tools; never invent prices, tickers or probabilities.
- list_edges to scan, get_game_context for a game's line/injuries/weather, price_market / price_parlay to check specific legs, build_ladder for ML/spread/team-total stacks, simulate_scenario to re-price a game under a view.
- When you recommend specific bets, call propose_bets so they appear as cards. Keep it to the best 1-4 proposals.

The user's slip (sent with every message)
- Locked legs must be included in every proposal you make. Excluded legs must never be proposed. The server enforces this and tells you what it changed; adjust your wording to match.
- User scenarios (e.g. "SEA by 7+", "under 41") override the baseline model for that game: call simulate_scenario or price with the scenario, and quote the conditional fair values.
- odds_mode says how the user reads prices: "american" -> lead with American odds (e.g. fair -150 vs cost -135); "kalshi" -> lead with cents (e.g. fair 60c vs ask 57c). Always show fair next to cost.

Style: concise markdown. Lead with the pick and the numbers (fair vs cost, edge, confidence), then one or two lines of why. No long preambles, no disclaimers beyond a brief note when confidence is low."""


def _compact_edge(r: dict) -> dict:
    return {k: r.get(k) for k in ("ticker", "side", "title", "matchup", "family", "fair", "cost", "edge",
                                  "ev_per_dollar", "american_cost", "american_fair", "confidence")} | \
        {"why": (r.get("reasons") or [])[:3]}


def _t_list_edges(inp: dict) -> dict:
    res = edges(inp.get("event"), float(inp.get("min_edge", 0.03)), inp.get("kind") or "all")
    limit = max(1, min(40, int(inp.get("limit") or 15)))
    return {"as_of": res["as_of"], "count": len(res["edges"]),
            "edges": [_compact_edge(r) for r in res["edges"][:limit]], "arbs": res["arbs"][:5],
            **({"errors": res["errors"]} if res.get("errors") else {})}


def _t_price_market(inp: dict) -> dict:
    out = ss.price_legs([{"market_ticker": inp["market_ticker"], "side": inp["side"]}])
    leg = out["legs"][0]
    return {**leg, "ev_per_dollar": out["ev_per_dollar"]}


def _t_price_parlay(inp: dict) -> dict:
    return ss.price_legs(inp["legs"], inp.get("scenario"))


def _t_simulate(inp: dict) -> dict:
    out = ss.condition(inp["event"], inp["constraints"])
    limit = max(1, min(40, int(inp.get("limit") or 20)))
    best = sorted([r for r in out["markets"] if r["edge_cond"] is not None], key=lambda r: -r["edge_cond"])[:8]
    return {"event": out["event"], "p_scenario": out["p_scenario"],
            "biggest_moves": [{k: r[k] for k in ("ticker", "title", "fair_base", "fair_cond", "cost", "edge_cond")}
                              for r in out["markets"][:limit]],
            "best_conditional_yes_edges": [{k: r[k] for k in ("ticker", "title", "fair_cond", "cost", "edge_cond")}
                                           for r in best]}


def _t_ladder(inp: dict) -> dict:
    return ss.ladder(inp["event"], inp["team"], inp["family"], inp.get("strikes") or [], inp.get("stakes"),
                     inp.get("scenario"))


def _t_context(inp: dict) -> dict:
    ev = fv.load_event(inp["event"])
    out = {"event": ev.event_ticker, "matchup": ev.matchup, "in_progress": ev.in_play, "model": ev.model.summary(),
           "nflverse": {k: v for k, v in ev.nflverse.items() if k in ("spread_line", "total_line", "home_moneyline",
                                                                       "away_moneyline", "gameday", "gametime", "roof")}}
    try:
        import nflverse_data as nd
        rec = nd.team_records()
        out["records"] = {t: rec.get(nd.kalshi_to_nflverse_team(t), "0-0") for t in (ev.away, ev.home)}
        inj = {}
        for t in (ev.away, ev.home):
            df = nd.team_injuries(nd.kalshi_to_nflverse_team(t))
            rows = [f"{r['full_name']} ({r['position']}): {r['report_status']}" for r in df.to_dict(orient="records")
                    if isinstance(r.get("report_status"), str)]
            inj[t] = rows[:15]
        out["injuries"] = inj
    except Exception as e:
        out["injuries_error"] = str(e)
    try:
        out["weather"] = pm._weather(ev)
    except Exception:
        out["weather"] = None
    arbs, soft = fv.ladder_arbs(ev.markets, ev.away, ev.home, ev.event_ticker)
    out["arbs"] = arbs[:5]
    out["ladder_inconsistencies"] = soft[:5]
    return out


# -- slip handling / propose_bets enforcement ---------------------------------------------


def _leg_key(x) -> str | None:
    """'TICKER|side' from either a string or a {market_ticker, side} object."""
    if isinstance(x, str):
        if "|" not in x:
            return None
        t, s = x.split("|", 1)
        return f"{t.strip().upper()}|{(s.strip() or 'yes').lower()}"
    if isinstance(x, dict) and (x.get("market_ticker") or x.get("ticker")):
        return f"{str(x.get('market_ticker') or x.get('ticker')).upper()}|{str(x.get('side') or 'yes').lower()}"
    return None


def normalize_slip(slip: dict | None) -> dict:
    slip = slip if isinstance(slip, dict) else {}
    titles = {}
    for group in ("legs", "bookmarks", "locks", "excludes"):
        for x in slip.get(group) or []:
            k = _leg_key(x)
            if k and isinstance(x, dict) and x.get("title"):
                titles[k] = x["title"]
    locks = [k for k in (_leg_key(x) for x in slip.get("locks") or []) if k]
    excludes = [k for k in (_leg_key(x) for x in slip.get("excludes") or []) if k]
    return {
        "legs": [k for k in (_leg_key(x) for x in slip.get("legs") or []) if k],
        "bookmarks": [k for k in (_leg_key(x) for x in slip.get("bookmarks") or []) if k],
        "locks": list(dict.fromkeys(locks)),
        "excludes": list(dict.fromkeys(excludes)),
        "scenarios": [s for s in slip.get("scenarios") or [] if isinstance(s, dict) and s.get("event")],
        "odds_mode": slip.get("odds_mode") if slip.get("odds_mode") in ("american", "kalshi") else "american",
        "titles": titles,
    }


def _split(k: str) -> dict:
    t, s = k.split("|", 1)
    return {"market_ticker": t, "side": s}


def enforce_slip(legs: list[dict], slip: dict) -> tuple[list[dict], list[str]]:
    """Drop excluded legs, add missing locked legs (a locked side replaces the
    opposite side of the same market), dedupe."""
    notes = []
    excl, locks = set(slip["excludes"]), slip["locks"]
    locked_tickers = {k.split("|")[0]: k for k in locks}
    out: list[str] = []
    for l in legs:
        k = _leg_key(l)
        if not k:
            continue
        if k in excl:
            notes.append(f"dropped excluded leg {slip['titles'].get(k, k)}")
            continue
        t = k.split("|")[0]
        if t in locked_tickers and locked_tickers[t] != k:
            notes.append(f"replaced {k} with locked side {locked_tickers[t]}")
            k = locked_tickers[t]
        if k not in out:
            out.append(k)
    for k in locks:
        if k not in out and k not in excl:
            out.append(k)
            notes.append(f"added locked leg {slip['titles'].get(k, k)}")
    return [_split(k) for k in out], notes


def _price_proposal(p: dict, slip: dict) -> dict:
    legs, notes = enforce_slip(p.get("legs") or [], slip)
    scen = p.get("scenario") or None
    if scen is None:
        # user scenarios for games in this proposal apply by default
        sfx = {fv.game_suffix(l["market_ticker"]) for l in legs}
        matching = [s for s in slip["scenarios"] if fv.game_suffix(s["event"]) in sfx and s.get("constraints")]
        scen = matching or None
    out = {"id": uuid.uuid4().hex[:10], "title": p.get("title") or "Proposal", "kind": p.get("kind") or "single",
           "rationale": p.get("rationale") or "", "legs": [], "joint_fair": None, "parlay_american_cost": None,
           "scenario": scen, "notes": notes}
    if not legs:
        out["notes"].append("no legs left after applying excludes")
        return out
    try:
        priced = ss.price_legs(legs, scen)
    except Exception as e:
        out["notes"].append(f"pricing failed: {e}")
        out["legs"] = [{**l, "title": slip["titles"].get(_leg_key(l))} for l in legs]
        return out
    out["legs"] = [{k: lv.get(k) for k in ("market_ticker", "side", "title", "fair", "cost", "american_cost",
                                            "american_fair", "edge", "event_ticker")} for lv in priced["legs"]]
    out.update(joint_fair=priced["joint_fair"], indep_fair=priced["indep_fair"], parlay_cost=priced["parlay_cost"],
               parlay_american_cost=priced["parlay_american_cost"], joint_american=priced["joint_american"],
               ev_per_dollar=priced["ev_per_dollar"], same_event=priced["same_event"],
               scenario_applied=priced["scenario_applied"])
    return out


# -- chat -----------------------------------------------------------------------------------

_client = None
_client_lock = threading.Lock()


def get_client():
    global _client
    with _client_lock:
        if _client is None:
            import anthropic
            _client = anthropic.Anthropic()
        return _client


def chat_enabled() -> bool:
    try:
        c = get_client()
    except Exception:
        return False
    return bool(getattr(c, "api_key", None) or getattr(c, "auth_token", None) or getattr(c, "credentials", None))


def status() -> dict:
    return {"chat_enabled": chat_enabled(), "model": MODEL}


class _Conversation:
    def __init__(self):
        self.messages: list[dict] = []
        self.lock = threading.Lock()


_conversations: "OrderedDict[str, _Conversation]" = OrderedDict()
_conv_lock = threading.Lock()


def _get_conversation(cid: str | None) -> tuple[str, _Conversation]:
    with _conv_lock:
        if cid and cid in _conversations:
            _conversations.move_to_end(cid)
            return cid, _conversations[cid]
        cid = cid or uuid.uuid4().hex
        conv = _Conversation()
        _conversations[cid] = conv
        while len(_conversations) > MAX_CONVERSATIONS:
            _conversations.popitem(last=False)
        return cid, conv


def _slate_lines() -> list[str]:
    try:
        out = []
        for g in fv.current_games():
            out.append(f"{g['event_ticker']}: {g['away']} @ {g['home']}")
        return out
    except Exception:
        return []


def _turn_text(message: str, slip: dict) -> str:
    ctx = {
        "odds_mode": slip["odds_mode"],
        "locked_legs": [{"leg": k, "title": slip["titles"].get(k)} for k in slip["locks"]],
        "excluded_legs": [{"leg": k, "title": slip["titles"].get(k)} for k in slip["excludes"]],
        "slip_legs": [{"leg": k, "title": slip["titles"].get(k)} for k in slip["legs"]],
        "bookmarks": [{"leg": k, "title": slip["titles"].get(k)} for k in slip["bookmarks"][:30]],
        "scenarios": slip["scenarios"],
        "slate": _slate_lines(),
        "now_utc": _iso(time.time()),
    }
    return (f"<slip_state>\n{json.dumps(ctx, default=str)}\n</slip_state>\n"
            f"(Legs are MARKET_TICKER|side. Locked legs must be in every proposal; excluded legs never.)\n\n"
            f"{message}")


def _summary(name: str, inp: dict, result) -> str:
    if isinstance(result, dict) and result.get("error"):
        return f"{name}: error {result['error']}"
    try:
        if name == "list_edges":
            return f"list_edges {inp.get('event') or 'slate'} ({inp.get('kind') or 'all'}): {result['count']} edges"
        if name == "price_market":
            return f"price {result.get('title')} {inp.get('side')}: fair {result.get('fair')} vs cost {result.get('cost')}"
        if name == "price_parlay":
            return f"parlay {len(inp.get('legs') or [])} legs: joint {result.get('joint_fair')} vs cost {result.get('parlay_cost')}"
        if name == "simulate_scenario":
            return f"scenario {inp.get('constraints')}: p={result.get('p_scenario')}"
        if name == "build_ladder":
            return f"ladder {inp.get('team')} {inp.get('family')} {inp.get('strikes')}: EV {result.get('ev')}"
        if name == "get_game_context":
            return f"context {result.get('matchup')}"
        if name == "propose_bets":
            return f"proposed {len(result.get('proposals', []))} bet(s)"
    except Exception:
        pass
    return name


def run_tool(name: str, inp: dict, slip: dict, proposals_out: list) -> dict:
    if name == "propose_bets":
        props = [_price_proposal(p, slip) for p in (inp.get("proposals") or [])]
        proposals_out.extend(props)
        return {"proposals": [{"id": p["id"], "title": p["title"], "legs": [f"{l['market_ticker']}|{l['side']}"
                                                                            for l in p["legs"]],
                               "joint_fair": p["joint_fair"], "parlay_cost": p.get("parlay_cost"),
                               "notes": p["notes"]} for p in props],
                "enforcement": [n for p in props for n in p["notes"]] or ["no changes needed"],
                "shown_to_user": True}
    fn = {"list_edges": _t_list_edges, "price_market": _t_price_market, "price_parlay": _t_price_parlay,
          "simulate_scenario": _t_simulate, "build_ladder": _t_ladder, "get_game_context": _t_context}.get(name)
    if fn is None:
        return {"error": f"unknown tool {name}"}
    return fn(inp)


def _blocks_text(content) -> str:
    return "\n\n".join(b.text for b in content if getattr(b, "type", None) == "text" and getattr(b, "text", None))


def _create(client, messages):
    kwargs = dict(
        model=MODEL, max_tokens=MAX_TOKENS,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        tools=TOOLS, messages=messages,
        thinking={"type": "adaptive"}, output_config={"effort": "medium"},
        betas=[FALLBACK_BETA],
    )
    try:
        return client.beta.messages.create(fallbacks="default", **kwargs)
    except TypeError:
        # older SDKs without the `fallbacks` kwarg: send it raw
        return client.beta.messages.create(extra_body={"fallbacks": "default"}, **kwargs)


def chat(message: str, slip: dict | None = None, conversation_id: str | None = None, client=None) -> dict:
    if not isinstance(message, str) or not message.strip():
        raise EngineError("message is required")
    if client is None:
        if not chat_enabled():
            raise ChatUnavailable("ANTHROPIC_API_KEY is not set on the server")
        client = get_client()
    slip_n = normalize_slip(slip)
    cid, conv = _get_conversation(conversation_id)
    proposals: list[dict] = []
    trace: list[dict] = []
    with conv.lock:
        messages = conv.messages
        user_block = {"type": "text", "text": _turn_text(message.strip(), slip_n)}
        if messages and messages[-1]["role"] == "user":
            # previous turn ended on tool results (iteration cap): keep roles alternating
            messages[-1]["content"] = list(messages[-1]["content"]) + [user_block]
        else:
            messages.append({"role": "user", "content": [user_block]})
        reply_parts: list[str] = []
        for it in range(MAX_TOOL_ITERS + 1):
            resp = _create(client, messages)
            stop = getattr(resp, "stop_reason", None)
            if stop == "refusal":
                reply_parts.append("_The model declined to answer this request._")
                break
            content = list(resp.content or [])
            tool_uses = [b for b in content if getattr(b, "type", None) == "tool_use"]
            if stop == "max_tokens":
                if not tool_uses:
                    messages.append({"role": "assistant", "content": content})
                reply_parts.append(_blocks_text(content))
                reply_parts.append("_(reply cut off at the length limit)_")
                break
            messages.append({"role": "assistant", "content": content})
            text = _blocks_text(content)
            if stop == "pause_turn":
                continue
            if stop != "tool_use" or not tool_uses:
                reply_parts.append(text)
                break
            if text:
                reply_parts.append(text)
            results = []
            for b in tool_uses:
                inp = b.input if isinstance(b.input, dict) else {}
                try:
                    res = run_tool(b.name, inp, slip_n, proposals)
                    is_err = False
                except Exception as e:
                    if not isinstance(e, (ValueError, KeyError)):
                        traceback.print_exc()
                    res, is_err = {"error": str(e)}, True
                trace.append({"tool": b.name, "summary": _summary(b.name, inp, res)})
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": json.dumps(fv_sanitize(res), default=str)[:60_000],
                                **({"is_error": True} if is_err else {})})
            messages.append({"role": "user", "content": results})
            if it == MAX_TOOL_ITERS - 1:
                reply_parts.append("_(stopped after the maximum number of tool calls)_")
                break
    reply = "\n\n".join(p for p in reply_parts if p).strip()
    return {"conversation_id": cid, "reply": reply, "proposals": proposals, "trace": trace}


def fv_sanitize(obj):
    """NaN/Inf -> None so tool results are valid JSON."""
    import math
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: fv_sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [fv_sanitize(v) for v in obj]
    if hasattr(obj, "item") and callable(obj.item):
        try:
            return obj.item()
        except Exception:
            return obj
    return obj
