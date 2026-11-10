"""
Week overview tab: an engine-written preview feed (one post per game) and the
week's notable injuries league-wide, best players (Madden OVR) first.

Posts are written by Claude driving the quant engine: the game's lines,
injuries (with OVR), depth-chart matchups, tendency reads and the engine's top
edges are gathered here and handed over up front; the model can price more
legs with the engine tools, then must call publish_preview. Every recommended
bet is re-priced server-side (engine_agent._price_proposal) before it's saved,
so the numbers on a post always come from the engine, never the model.

Posts live in data/feed/posts.json keyed by event ticker (one per game;
rewriting replaces). A/B runs (generate --ab) write the same game with several
models independently into data/feed/ab.json for side-by-side comparison; the
Week tab shows them and can promote one to the live feed. Generating costs API calls, so the server only allows it
from the operator's own machine; on the VPS use the CLI:

    .venv/bin/python week_feed.py generate [--week N] [--event KXNFLGAME-...] [--missing]
    .venv/bin/python week_feed.py generate --ab claude-sonnet-5-5,claude-opus-5-5   # A/B, effort low
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor

import bet_ledger as bl
import engine_agent as ea
import madden_ratings as mr
import nflverse_data as nd

MODEL = "claude-opus-5-5"
EFFORT = "low"
MAX_TOKENS = 16000
MAX_TOOL_ITERS = 10
FEED_PATH = os.path.join(os.path.dirname(__file__), "data", "feed", "posts.json")
AB_PATH = os.path.join(os.path.dirname(__file__), "data", "feed", "ab.json")
# $/MTok: input, output, cache read (cache writes bill at 1.25x input)
PRICES = {"claude-opus-5-5": (4.0, 20.0, 0.20), "claude-sonnet-5-5": (2.0, 10.0, 0.20),
          "claude-opus-5": (5.0, 25.0, 0.50), "claude-haiku-4-5": (1.0, 5.0, 0.10)}

_lock = threading.Lock()


# -- top injuries -------------------------------------------------------------------------

def _injury_kind(r: dict) -> str | None:
    """'report' (game status or limited/DNP practice), 'reserve' (IR/PUP/NFI),
    or None for players practicing fully with no game status."""
    status = (r.get("report_status") or "").strip()
    practice = (r.get("practice_status") or "").lower()
    if status in ("IR", "PUP", "NFI") or "reserve" in practice or "pup" in practice:
        return "reserve"
    if status or "did not" in practice or "limited" in practice:
        return "report"
    return None


def week_injuries(games: list[dict]) -> dict:
    """Notable injuries for every team playing in `games` (week-overview game
    dicts, Kalshi team codes), each tagged with its opponent and game."""
    teams = []
    for g in games:
        teams += [(g["away"], g["home"], g), (g["home"], g["away"], g)]

    def one(entry):
        team, opp, g = entry
        try:
            return team, opp, g, mr.injuries_by_ovr(nd.kalshi_to_nflverse_team(team))
        except Exception:
            traceback.print_exc()
            return team, opp, g, []

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(one, teams))
    rows = []
    for team, opp, g, inj in results:
        for r in inj:
            kind = _injury_kind(r)
            if kind:
                rows.append({**r, "team": team, "opp": opp, "home": team == g["home"],
                             "event_ticker": g["event_ticker"], "kind": kind})
    rows.sort(key=lambda r: (-(r["ovr"] or 0), r["full_name"]))
    return {"injuries": rows, "teams": len(teams)}


# -- post store ---------------------------------------------------------------------------

def _load(path: str | None = None) -> dict:
    try:
        with open(path or FEED_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def _save(posts: dict, path: str | None = None) -> None:
    path = path or FEED_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(posts, f, default=str)
    os.replace(tmp, path)


def save_post(post: dict) -> None:
    with _lock:
        posts = _load()
        posts[post["event_ticker"]] = post
        _save(posts)


def delete_post(event_ticker: str) -> bool:
    with _lock:
        posts = _load()
        if posts.pop(event_ticker, None) is None:
            return False
        _save(posts)
        return True


def feed(overview: dict) -> dict:
    """Posts for the week in `overview` (build_week_overview output), in
    kickoff order, plus the games that don't have one yet."""
    posts = _load()
    games, out = [], []
    for g in overview["games"]:
        games.append({"event_ticker": g["event_ticker"], "away": g["away"], "home": g["home"],
                      "kickoff": g.get("kickoff"), "schedule": g.get("schedule"), "records": g.get("records"),
                      "has_post": g["event_ticker"] in posts})
        if g["event_ticker"] in posts:
            out.append(posts[g["event_ticker"]])
    ab = _load(AB_PATH)
    week_events = {g["event_ticker"] for g in games}
    ab = {ev: v for ev, v in ab.items() if ev in week_events}
    bets = bl.week_bets(week_events)
    by_id = {b["id"]: b for b in bets}
    for p in out + [v for e in ab.values() for v in e["variants"].values()]:
        for i, b in enumerate(p.get("bets") or []):
            g = by_id.get(f"{p.get('post_id')}:{i}")
            if g:
                b["grade"] = {k: g.get(k) for k in ("outcome", "pnl", "record", "due_at", "graded_at")}
                b["grade"]["legs"] = [l.get("result") for l in g["legs"]]
    return {"week": overview["week"], "weeks": overview["weeks"], "games": games, "posts": out,
            "ab": ab, "scorecard": bl.scorecard(bets),
            "chat_enabled": ea.chat_enabled(), "model": MODEL, "effort": EFFORT}


# -- game context handed to the model -------------------------------------------------------

def _quote(q: dict | None) -> dict | None:
    if not q:
        return None
    return {k: q.get(k) for k in ("ticker", "team", "line", "yes_bid", "yes_ask", "implied_prob_yes") if q.get(k) is not None}


def _player(slot: list[dict]) -> str | None:
    if not slot:
        return None
    p = slot[0]
    m = p.get("madden") or {}
    return f"{p.get('full_name')} ({m.get('ovr') or '?'})"


def game_context(detail: dict) -> dict:
    """Compact, model-facing summary of build_game_detail output."""
    away, home = detail["away"], detail["home"]
    ctx = {
        "event": detail.get("event_ticker"), "matchup": f"{away} @ {home}", "kickoff": detail.get("kickoff"),
        "records": detail.get("records"),
        "lines": {"moneyline": {s: _quote(q) for s, q in (detail.get("moneyline") or {}).items()},
                  "spread": _quote(detail.get("spread")), "total": _quote(detail.get("total")),
                  "team_totals": {s: _quote(q) for s, q in (detail.get("team_totals") or {}).items()}},
        "injuries": {}, "matchups": [], "tendency_reads": [],
    }
    for side, team in (("away", away), ("home", home)):
        rows = [r for r in (detail.get("injuries") or {}).get(side) or [] if _injury_kind(r)]
        ctx["injuries"][team] = [
            {"player": r["full_name"], "pos": r.get("position"), "ovr": r.get("ovr"),
             "game_status": r.get("report_status"), "practice": r.get("practice_status"),
             "injury": r.get("report_primary_injury") or r.get("practice_primary_injury")}
            for r in rows[:14]]
    for m in detail.get("matchups") or []:
        rows = [r for r in m.get("rows") or [] if r.get("edge") is not None]
        rows.sort(key=lambda r: -abs(r["edge"]))
        ctx["matchups"].append({
            "offense": m["offense_team"], "defense": m["defense_team"], "scheme": m.get("scheme"),
            "unit_ovr_edges": [{k: s.get(k) for k in ("unit", "off_avg", "def_avg", "edge")} for s in m.get("summary") or []],
            "biggest_ovr_gaps": [{"slot": f"{r['off_pos']} vs {r['def_pos']}", "offense": _player(r["offense"]),
                                  "defense": _player(r["defense"]), "ovr_edge": r["edge"]} for r in rows[:6]],
        })
    td = detail.get("tendencies") or {}
    for s in td.get("sides") or []:
        ctx["tendency_reads"] += [r.get("text") for r in s.get("reads") or [] if r.get("text")][:6]
    return ctx


def _edges(event_ticker: str) -> list[dict]:
    try:
        res = ea.edges(event_ticker, 0.02, "all")
    except Exception as e:
        return [{"error": str(e)}]
    return [ea._compact_edge(r) for r in res["edges"] if r.get("confidence") != "low"][:15]


# -- the writer ---------------------------------------------------------------------------

_BET = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "kind": {"type": "string", "enum": ["single", "parlay", "ladder"]},
        "rationale": {"type": "string", "description": "One or two sentences tying the bet to the write-up."},
        "legs": {"type": "array", "items": ea._LEG},
    },
    "required": ["title", "kind", "rationale", "legs"],
    "additionalProperties": False,
}

PUBLISH_TOOL = ea._tool(
    "publish_preview",
    "Publish the finished game preview to the week feed. Call exactly once, at the end. The server re-prices "
    "every bet leg with the engine before it's shown.",
    {
        "headline": {"type": "string", "description": "Punchy one-line headline, under 80 characters."},
        "lean": {"type": "string", "description": "The side you like in a few words, e.g. 'NYG -2.5' or 'Under 41.5'."},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "writeup": {"type": "string", "description": "Markdown, 120-220 words: injuries that matter, the key "
                                                     "matchups, how the game script likely goes, and why."},
        "key_injuries": {"type": "array", "items": {
            "type": "object",
            "properties": {"team": {"type": "string"}, "player": {"type": "string"},
                           "impact": {"type": "string", "description": "A few words on why it matters."}},
            "required": ["team", "player", "impact"], "additionalProperties": False}},
        "key_matchups": {"type": "array", "items": {
            "type": "object",
            "properties": {"title": {"type": "string", "description": "e.g. 'NYG edge rush vs WAS RT'"},
                           "note": {"type": "string"}},
            "required": ["title", "note"], "additionalProperties": False}},
        "bets": {"type": "array", "items": _BET},
    },
    ["headline", "lean", "confidence", "writeup", "key_injuries", "key_matchups", "bets"],
)

def _writer_tool(t: dict) -> dict:
    """The engine tool without scenario inputs: strict schemas are capped at 24
    optional parameters in total, and the constraint object alone has 11."""
    schema = t["input_schema"]
    props = {k: v for k, v in schema["properties"].items() if k not in ("scenario", "stakes")}
    return {**t, "input_schema": {**schema, "properties": props}}


WRITER_TOOLS = [_writer_tool(t) for t in ea.TOOLS
                if t["name"] not in ("propose_bets", "simulate_scenario")] + [PUBLISH_TOOL]

SYSTEM_PROMPT = """You write the game previews in the weekly feed of an NFL research tool for Kalshi prediction markets. Readers are bettors who want a fast, sharp take on each game and a few bets backed by the tool's quant engine.

Each request gives you one game's context: Kalshi lines, both injury reports with Madden OVR ratings (higher = better player; an absent 85+ starter matters, a backup's absence usually doesn't), depth-chart matchups with OVR gaps (positive ovr_edge favors the offense), tendency reads, and the engine's top edges (fair value vs cost) for the game.

How to write it
- Lead with what decides the game: the injuries that actually change it (who's out or doubtful, and who replaces them) and the two or three matchups with real leverage. Connect them to game script and to the markets.
- Be specific and grounded in the data given or fetched. Never invent players, injuries, prices or probabilities. If the data is thin, say less.
- Plain, confident sports-writing voice. No hype, no disclaimers, no "as an AI".

Bets
- Recommend 1-3 bets that follow from the write-up: singles, a same-game parlay, or a same-team ladder. Only use market tickers that appear in the context or in tool results.
- Prefer real edges (engine fair above cost by ~2c or more, not low confidence). Use price_market / price_parlay / build_ladder to check legs before recommending them; get_game_context adds weather.
- Kalshi combos allow at most one game-level leg (ML, spread, total, team total) per game. If nothing has an edge, recommend the single best-priced look and set confidence to low.

Finish by calling publish_preview exactly once. Don't write the preview as a normal message."""


def _create(client, messages, model: str, effort: str):
    kwargs = dict(
        model=model, max_tokens=MAX_TOKENS,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        tools=WRITER_TOOLS, messages=messages,
        thinking={"type": "adaptive"}, output_config={"effort": effort},
        betas=[ea.FALLBACK_BETA],
    )
    try:
        return client.beta.messages.create(fallbacks="default", **kwargs)
    except TypeError:
        return client.beta.messages.create(extra_body={"fallbacks": "default"}, **kwargs)


def _price(model: str) -> tuple[float, float, float]:
    """$/MTok (input, output, cache read) for a served model id."""
    for prefix, p in PRICES.items():
        if model.startswith(prefix):
            return p
    return PRICES[MODEL]


def _usage_cost(usage: dict, model: str) -> float:
    inp, out, read = _price(model)
    return round((usage["input_tokens"] * inp + usage["cache_creation_input_tokens"] * inp * 1.25
                  + usage["cache_read_input_tokens"] * read + usage["output_tokens"] * out) / 1e6, 4)


def prepare(event_ticker: str) -> tuple[dict, dict]:
    """(game detail, model-facing context) for one game. Built once and shared
    by every model in an A/B run so they see identical prices."""
    import week_overview as wo
    detail = wo.build_game_detail(event_ticker)
    detail["event_ticker"] = event_ticker
    ctx = game_context(detail)
    ctx["engine_edges"] = _edges(event_ticker)
    ctx["now_utc"] = ea._iso(time.time())
    return detail, ctx


def run_writer(event_ticker: str, detail: dict, ctx: dict, client=None, model: str = MODEL,
               effort: str = EFFORT) -> dict:
    """One model's preview for one game (not saved). Each call is its own
    conversation, so in an A/B run neither model sees the other's work."""
    if client is None:
        if not ea.chat_enabled():
            raise ea.ChatUnavailable("ANTHROPIC_API_KEY is not set on the server")
        client = ea.get_client()
    messages = [{"role": "user", "content": [{"type": "text", "text":
        f"<game_context>\n{json.dumps(ea.fv_sanitize(ctx), default=str)}\n</game_context>\n\n"
        f"Write the preview for {ctx['matchup']} and publish it."}]}]
    slip = ea.normalize_slip({})
    trace: list[str] = []
    usage = dict.fromkeys(("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"), 0)
    served, t0 = model, time.time()
    for _ in range(MAX_TOOL_ITERS):
        resp = _create(client, messages, model, effort)
        served = getattr(resp, "model", None) or served
        u = getattr(resp, "usage", None)
        for k in usage:
            usage[k] += getattr(u, k, 0) or 0
        stop = getattr(resp, "stop_reason", None)
        if stop == "refusal":
            raise ea.EngineError("the model declined to write this preview")
        content = list(resp.content or [])
        messages.append({"role": "assistant", "content": content})
        if stop == "pause_turn":
            continue
        tool_uses = [b for b in content if getattr(b, "type", None) == "tool_use"]
        publish = next((b for b in tool_uses if b.name == "publish_preview"), None)
        if publish is not None:
            post = _build_post(event_ticker, detail, publish.input, slip)
            post.update(model=served, effort=effort, trace=trace,
                        stats={**usage, "cost_usd": _usage_cost(usage, served), "seconds": round(time.time() - t0)})
            return post
        if stop != "tool_use" or not tool_uses:
            raise ea.EngineError("the engine finished without publishing a preview")
        results = []
        for b in tool_uses:
            inp = b.input if isinstance(b.input, dict) else {}
            try:
                res, is_err = ea.run_tool(b.name, inp, slip, []), False
            except Exception as e:
                if not isinstance(e, (ValueError, KeyError)):
                    traceback.print_exc()
                res, is_err = {"error": str(e)}, True
            trace.append(ea._summary(b.name, inp, res))
            results.append({"type": "tool_result", "tool_use_id": b.id,
                            "content": json.dumps(ea.fv_sanitize(res), default=str)[:60_000],
                            **({"is_error": True} if is_err else {})})
        messages.append({"role": "user", "content": results})
    raise ea.EngineError("stopped after the maximum number of tool calls without publishing")


def write_preview(event_ticker: str, client=None, model: str = MODEL, effort: str = EFFORT) -> dict:
    """Have the engine write, price and save one game's post to the live feed."""
    detail, ctx = prepare(event_ticker)
    post = run_writer(event_ticker, detail, ctx, client, model, effort)
    save_post(post)
    bl.record(post, "live")
    return post


def write_ab(event_ticker: str, models: list[str], client=None, effort: str = EFFORT) -> dict:
    """Same game, same context, one independent run per model, in parallel.
    Saved to the A/B store, not the live feed. Returns {model: post or error}."""
    detail, ctx = prepare(event_ticker)

    def one(m):
        try:
            return m, run_writer(event_ticker, detail, ctx, client, m, effort)
        except Exception as e:
            if not isinstance(e, ea.EngineError):
                traceback.print_exc()
            return m, {"error": str(e), "model": m, "effort": effort}

    with ThreadPoolExecutor(max_workers=len(models)) as pool:
        out = dict(pool.map(one, models))
    with _lock:
        ab = _load(AB_PATH)
        ab[event_ticker] = {"models": models, "variants": out}
        _save(ab, AB_PATH)
    for p in out.values():
        if not p.get("error"):
            bl.record(p, "ab")
    return out


def promote(event_ticker: str, model: str) -> dict:
    """Copy one A/B variant into the live feed."""
    variant = (_load(AB_PATH).get(event_ticker) or {}).get("variants", {}).get(model)
    if not variant or variant.get("error"):
        raise ea.EngineError(f"no {model} preview for {event_ticker}")
    save_post(variant)
    bl.record(variant, "live")
    return variant


def _week_of(away: str, home: str) -> int | None:
    try:
        sched = nd.find_upcoming_game(nd.kalshi_to_nflverse_team(away), nd.kalshi_to_nflverse_team(home))
        return sched["week"] if sched else None
    except Exception:
        return None


def _build_post(event_ticker: str, detail: dict, inp: dict, slip: dict) -> dict:
    bets = [ea._price_proposal(b, slip) for b in (inp.get("bets") or [])[:3]]
    return {
        "post_id": uuid.uuid4().hex[:12], "event_ticker": event_ticker, "away": detail["away"], "home": detail["home"],
        "kickoff": detail.get("kickoff"), "week": _week_of(detail["away"], detail["home"]),
        "created_at": ea._iso(time.time()),
        "headline": inp.get("headline") or "", "lean": inp.get("lean") or "",
        "confidence": inp.get("confidence") or "low", "writeup": inp.get("writeup") or "",
        "key_injuries": inp.get("key_injuries") or [], "key_matchups": inp.get("key_matchups") or [],
        "bets": bets,
    }


def backfill_ledger() -> int:
    """Give older posts (live and A/B) a post_id and record their bets."""
    added = 0
    with _lock:
        posts, ab = _load(), _load(AB_PATH)
        for p in posts.values():
            p.setdefault("post_id", uuid.uuid4().hex[:12])
        # a promoted variant is the same post: share its id
        live_keys = {(p["event_ticker"], p.get("created_at"), p.get("model")): p["post_id"] for p in posts.values()}
        for entry in ab.values():
            for v in entry["variants"].values():
                if not v.get("error"):
                    v.setdefault("post_id", live_keys.get((v["event_ticker"], v.get("created_at"), v.get("model")))
                                 or uuid.uuid4().hex[:12])
        _save(posts)
        _save(ab, AB_PATH)
    for entry in ab.values():
        for v in entry["variants"].values():
            if not v.get("error"):
                added += bl.record(v, "ab")
    for p in posts.values():
        added += bl.record(p, "live")
    return added


# -- CLI ------------------------------------------------------------------------------------

def main(argv=None):
    from dotenv import load_dotenv
    load_dotenv()
    import week_overview as wo
    ap = argparse.ArgumentParser(description="Write engine game previews for the week feed.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--week", type=int)
    g.add_argument("--event", help="one game (KXNFLGAME-...) instead of the whole week")
    g.add_argument("--missing", action="store_true", help="skip games that already have a post")
    g.add_argument("--model", default=MODEL)
    g.add_argument("--effort", default=EFFORT, choices=["low", "medium", "high", "xhigh", "max"])
    g.add_argument("--ab", help="comma-separated models to compare side by side (A/B store, not the live feed)")
    g.add_argument("--parallel", type=int, default=3, help="games written at once")
    sub.add_parser("backfill-ledger", help="record bets from posts written before the ledger existed")
    gr = sub.add_parser("grade", help="grade due bets now and print the last 24h of results")
    args = ap.parse_args(argv)

    if args.cmd == "backfill-ledger":
        print(f"recorded {backfill_ledger()} bet(s)")
        return 0
    if args.cmd == "grade":
        print(f"graded {bl.grade_due()} bet(s)")
        now = bl._now()
        print(bl.results(now - bl.timedelta(hours=24), now)["report"] or "nothing graded in the last 24h")
        return 0

    models = [m.strip() for m in args.ab.split(",") if m.strip()] if args.ab else None
    if args.event:
        events = [args.event]
    else:
        ov = wo.build_week_overview(args.week)
        have = _load(AB_PATH if models else FEED_PATH)
        events = [x["event_ticker"] for x in ov["games"] if not (args.missing and x["event_ticker"] in have)]
        print(f"Week {ov['week']}: {len(events)} game(s)")

    def one(ev):
        t0 = time.time()
        try:
            if models:
                out = write_ab(ev, models, effort=args.effort)
                parts = [f"{m}: " + (f"FAILED {p['error']}" if p.get("error") else
                                     f"${p['stats']['cost_usd']:.3f} {p['stats']['seconds']}s {len(p['bets'])} bets")
                         for m, p in out.items()]
                return any(p.get("error") for p in out.values()), f"  {ev} ({time.time() - t0:.0f}s): " + " | ".join(parts)
            post = write_preview(ev, model=args.model, effort=args.effort)
            return False, f"  {ev}: {post['headline']} (${post['stats']['cost_usd']:.3f}, {len(post['bets'])} bets, {time.time() - t0:.0f}s)"
        except Exception as e:
            return True, f"  {ev}: FAILED {e}"

    failed = 0
    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        for bad, line in pool.map(one, events):
            failed += bad
            print(line, flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
