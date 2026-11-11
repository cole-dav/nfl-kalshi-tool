"""
Post-game review and a scouting notebook on every team.

After a game is final and its play-by-play is in nflverse, Claude reviews it:
it gets the final score, both offenses' play-by-play facts (splits, run
direction, drive results, players, the biggest plays), each team's season
game log, the current notebook for both teams, and, when the feed previewed
the game, every model's preview with the assumptions its bets depended on, its
reasoning summary and tool calls (week_feed audits) and the graded bets. It
checks each assumption against the play-by-play (query_plays pulls specific
plays and splits), reviews the reasoning (process vs luck, over-reliance on
the engine), judges whether the engine's edge was real, writes 0-3 general
lessons, and rewrites both teams' notebooks. The preview writer reads the
notebooks and recent lessons, so the feed learns week to week.

Every game is reviewed, previewed or not, so every team's notebook updates
after each game it plays. Stores (data/feed/, host-only, backed up nightly):
  reviews.json     game_id -> review
  team_notes.json  Kalshi team code -> {notes, week, history: [...]}
The server reviews finished games on its own when FEED_AUTO_REVIEW=1 (VPS);
otherwise: week_feed.py review [--game 2026_10_NE_DET | --week N] [--force].
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

import pandas as pd

import engine_agent as ea
import nflverse_data as nd

MODEL = "claude-opus-5-5"
EFFORT = "medium"
MAX_TOKENS = 16000
MAX_TOOL_ITERS = 12
FEED_DIR = os.path.join(os.path.dirname(__file__), "data", "feed")
REVIEWS_PATH = os.path.join(FEED_DIR, "reviews.json")
NOTES_PATH = os.path.join(FEED_DIR, "team_notes.json")
AUDIT_DIR = os.path.join(FEED_DIR, "audits")
NOTES_HISTORY = 12          # earlier versions kept per team
READY_AFTER = timedelta(hours=16)       # kickoff + game + the ledger's 12h grading delay
GIVE_UP_WAITING_ON_BETS = timedelta(hours=40)
LOOKBACK_DAYS = 10
ET = "America/New_York"
_lock = threading.Lock()


class ReviewNotReady(RuntimeError):
    pass


# -- stores ---------------------------------------------------------------------------------

def _load(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def _save(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, default=str)
    os.replace(tmp, path)


def reviews() -> dict:
    return _load(REVIEWS_PATH)


def all_notes() -> dict:
    return _load(NOTES_PATH)


def team_notes(team: str) -> dict | None:
    """Notebook for one team (Kalshi code), without its history."""
    n = all_notes().get(team)
    return {k: v for k, v in n.items() if k != "history"} if n else None


def _write_notes(updates: list[dict], week: int, season: int, game_id: str) -> None:
    with _lock:
        notes = all_notes()
        for u in updates:
            team, text = u.get("team"), (u.get("notes") or "").strip()
            if not team or not text:
                continue
            prev = notes.get(team)
            history = (prev or {}).get("history") or []
            if prev:
                history = ([{k: prev.get(k) for k in ("notes", "week", "season", "game_id", "updated_at")}]
                           + history)[:NOTES_HISTORY]
            notes[team] = {"team": team, "notes": text, "week": week, "season": season, "game_id": game_id,
                           "updated_at": ea._iso(time.time()), "history": history}
        _save(NOTES_PATH, notes)


def recent_lessons(limit: int = 8) -> list[dict]:
    """Newest lessons across all reviews, for the preview writer."""
    out = []
    for r in reviews().values():
        for l in r.get("lessons") or []:
            out.append({"week": r.get("week"), "game": r.get("matchup"), "lesson": l, "at": r.get("created_at") or ""})
    out.sort(key=lambda x: x["at"], reverse=True)
    return [{k: v for k, v in x.items() if k != "at"} for x in out[:limit]]


def save_audit(post_id: str, audit: dict) -> None:
    os.makedirs(AUDIT_DIR, exist_ok=True)
    _save(os.path.join(AUDIT_DIR, f"{post_id}.json"), audit)


def load_audit(post_id: str) -> dict | None:
    if not post_id or not post_id.isalnum():
        return None
    a = _load(os.path.join(AUDIT_DIR, f"{post_id}.json"))
    return a or None


# -- play-by-play facts -----------------------------------------------------------------------

def _r(x, nd_=2):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) else round(x, nd_)


def _k(team: str) -> str:
    return nd.nflverse_to_kalshi_team(team)


def _scrimmage(g: pd.DataFrame) -> pd.DataFrame:
    return g[g["play_type"].isin(["pass", "run"])]


def _split(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"plays": 0}
    return {"plays": len(df), "yards": int(df["yards_gained"].fillna(0).sum()),
            "ypp": _r(df["yards_gained"].mean()), "epa_per_play": _r(df["epa"].mean(), 3),
            "success_rate": _r(df["success"].mean(), 3)}


def _offense(g: pd.DataFrame, team: str) -> dict:
    o = g[g["posteam"] == team]
    s = _scrimmage(o)
    db = s[s["pass"] == 1]
    att = s[s["pass_attempt"] == 1]
    runs = s[(s["rush"] == 1) & (s["qb_scramble"] != 1)]
    neutral = s[s["down"].isin([1, 2]) & s["wp"].between(0.2, 0.8) & (s["qtr"] <= 3)]
    drives = o.dropna(subset=["fixed_drive"]).groupby("fixed_drive").agg(
        result=("fixed_drive_result", "last"), min_yl=("yardline_100", "min"))
    rz = drives[drives["min_yl"] <= 20]
    out = {
        "overall": _split(s),
        "passing": {**_split(db), "dropbacks": len(db), "att": len(att), "cmp": int(att["complete_pass"].sum()),
                    "sacks": int(db["sack"].sum()), "ints": int(db["interception"].sum()),
                    "cpoe": _r(att["cpoe"].mean(), 1), "adot": _r(att["air_yards"].mean(), 1),
                    "explosive_20plus": int((db["yards_gained"] >= 20).sum()),
                    "scrambles": int(db["qb_scramble"].sum())},
        "rushing": {**_split(runs), "explosive_10plus": int((runs["yards_gained"] >= 10).sum()),
                    "by_direction": {loc: _split(runs[runs["run_location"] == loc])
                                     for loc in ("left", "middle", "right")}},
        "neutral_early_down_pass_rate": _r(neutral["pass"].mean(), 3) if len(neutral) else None,
        "neutral_pass_oe": _r(neutral["pass_oe"].mean(), 1) if len(neutral) else None,
        "third_down": f"{int(o['third_down_converted'].sum())}/"
                      f"{int(o['third_down_converted'].sum() + o['third_down_failed'].sum())}",
        "turnovers": int(o["interception"].sum() + o["fumble_lost"].sum()),
        "drives": len(drives), "drive_results": drives["result"].value_counts().to_dict(),
        "red_zone": f"{int((rz['result'] == 'Touchdown').sum())} TD on {len(rz)} trips",
        "penalties": int(((g["penalty"] == 1) & (g["penalty_team"] == team)).sum()),
    }
    passers = att.groupby("passer_player_name").agg(att=("pass_attempt", "sum"), cmp=("complete_pass", "sum"),
                                                     yds=("passing_yards", "sum"), td=("pass_touchdown", "sum"),
                                                     int_=("interception", "sum"))
    rushers = s[s["rush"] == 1].groupby("rusher_player_name").agg(att=("rush", "sum"), yds=("rushing_yards", "sum"),
                                                                  td=("rush_touchdown", "sum"))
    targets = att.dropna(subset=["receiver_player_name"]).groupby("receiver_player_name").agg(
        tgt=("pass_attempt", "sum"), rec=("complete_pass", "sum"), yds=("receiving_yards", "sum"),
        td=("pass_touchdown", "sum"))
    fmt = lambda df, n: [{"player": i, **{c.rstrip("_"): int(v) if not pd.isna(v) else 0 for c, v in r.items()}}
                         for i, r in df.head(n).iterrows()]
    out["players"] = {"passing": fmt(passers.sort_values("att", ascending=False), 2),
                      "rushing": fmt(rushers.sort_values("att", ascending=False), 4),
                      "receiving": fmt(targets.sort_values("tgt", ascending=False), 6)}
    return out


def _play_row(r) -> dict:
    return {"qtr": int(r["qtr"]) if not pd.isna(r["qtr"]) else None, "clock": r.get("time"),
            "offense": _k(r["posteam"]) if isinstance(r["posteam"], str) else None,
            "down": f"{int(r['down'])}&{int(r['ydstogo'])}" if not pd.isna(r["down"]) else None,
            "yardline": r.get("yrdln"), "type": r.get("play_type"), "yards": _r(r["yards_gained"], 0),
            "epa": _r(r["epa"]), "wpa": _r(r["wpa"], 3), "desc": str(r.get("desc") or "")[:220]}


def _by_quarter(g: pd.DataFrame, home: str, away: str) -> dict:
    out = {home: [], away: []}
    prev_h = prev_a = 0
    for q in sorted(g["qtr"].dropna().unique()):
        qq = g[g["qtr"] == q]
        h, a = int(qq["total_home_score"].max()), int(qq["total_away_score"].max())
        out[home].append(h - prev_h)
        out[away].append(a - prev_a)
        prev_h, prev_a = h, a
    return {_k(t): v for t, v in out.items()}


def game_facts(pbp: pd.DataFrame, row: pd.Series) -> dict:
    gid, home, away = row["game_id"], row["home_team"], row["away_team"]
    g = pbp[pbp["game_id"] == gid]
    if g.empty:
        raise ReviewNotReady(f"no play-by-play for {gid} yet")
    big = g.dropna(subset=["wpa"]).assign(aw=lambda d: d["wpa"].abs()).nlargest(14, "aw").sort_index()
    return {
        "final": {_k(away): int(row["away_score"]), _k(home): int(row["home_score"])},
        "closing_line": {"favorite": _k(home if (row.get("spread_line") or 0) >= 0 else away),
                         "favored_by": _r(abs(row.get("spread_line") or 0), 1), "total": _r(row.get("total_line"), 1)},
        "points_by_quarter": _by_quarter(g, home, away),
        "venue": {k: row.get(k) for k in ("roof", "surface", "temp", "wind")
                  if row.get(k) is not None and row.get(k) != "" and not pd.isna(row.get(k))},
        "offense": {_k(away): _offense(g, away), _k(home): _offense(g, home)},
        "biggest_plays_by_wpa": [_play_row(r) for _, r in big.iterrows()],
    }


def season_log(pbp: pd.DataFrame, sched: pd.DataFrame, team: str, before_week: int) -> list[dict]:
    """Per-game results for `team` (nflverse code) before `before_week`."""
    rows = sched[((sched["home_team"] == team) | (sched["away_team"] == team)) & sched["result"].notna()
                 & (sched["week"] < before_week)].sort_values("week")
    out = []
    for _, r in rows.iterrows():
        home = r["home_team"] == team
        g = _scrimmage(pbp[pbp["game_id"] == r["game_id"]])
        off, dfn = g[g["posteam"] == team], g[g["defteam"] == team]
        out.append({"week": int(r["week"]), "opp": ("vs " if home else "@ ") + _k(r["away_team"] if home else r["home_team"]),
                    "score": f"{int(r['home_score'] if home else r['away_score'])}-{int(r['away_score'] if home else r['home_score'])}",
                    "favored_by": _r((r["spread_line"] if home else -r["spread_line"]), 1),
                    "off_epa_play": _r(off["epa"].mean(), 3), "def_epa_play_allowed": _r(dfn["epa"].mean(), 3),
                    "rush_ypc": _r(off[off["rush"] == 1]["yards_gained"].mean(), 1),
                    "ypa": _r(off[off["pass"] == 1]["yards_gained"].mean(), 1)})
    return out


def query_plays(pbp: pd.DataFrame, game_id: str, inp: dict) -> dict:
    g = pbp[pbp["game_id"] == game_id]
    off = (inp.get("offense") or "").upper()
    if off and off != "ANY":
        g = g[g["posteam"] == nd.kalshi_to_nflverse_team(off)]
    pt = inp.get("play_type") or "any"
    g = g[g["play_type"] == pt] if pt in ("pass", "run") else g[g["play_type"].isin(["pass", "run", "field_goal", "punt"])]
    if inp.get("quarter"):
        g = g[g["qtr"] == int(inp["quarter"])]
    if inp.get("down"):
        g = g[g["down"] == int(inp["down"])]
    loc = inp.get("run_location") or "any"
    if loc != "any":
        g = g[g["run_location"] == loc]
    player = (inp.get("player") or "").strip()
    if player:
        cols = ["passer_player_name", "rusher_player_name", "receiver_player_name"]
        m = pd.Series(False, index=g.index)
        for c in cols:
            m |= g[c].fillna("").str.contains(player, case=False, regex=False)
        m |= g["desc"].fillna("").str.contains(player, case=False, regex=False)
        g = g[m]
    if inp.get("min_yards") is not None and inp["min_yards"] > -99:
        g = g[g["yards_gained"] >= inp["min_yards"]]
    oc = inp.get("outcome") or "any"
    if oc == "touchdown":
        g = g[g["touchdown"] == 1]
    elif oc == "turnover":
        g = g[(g["interception"] == 1) | (g["fumble_lost"] == 1)]
    elif oc == "sack":
        g = g[g["sack"] == 1]
    elif oc == "explosive":
        g = g[((g["pass"] == 1) & (g["yards_gained"] >= 20)) | ((g["rush"] == 1) & (g["yards_gained"] >= 10))]
    elif oc == "first_down":
        g = g[g["first_down"] == 1]
    elif oc == "failed":
        g = g[g["success"] == 0]
    sort = inp.get("sort") or "chrono"
    if sort in ("epa", "wpa", "yards"):
        col = {"epa": "epa", "wpa": "wpa", "yards": "yards_gained"}[sort]
        g2 = g.assign(_s=g[col].abs()).sort_values("_s", ascending=False)
    else:
        g2 = g
    limit = max(1, min(40, int(inp.get("limit") or 20)))
    return {"matching": len(g), "split": _split(_scrimmage(g)), "plays": [_play_row(r) for _, r in g2.head(limit).iterrows()]}


# -- the reviewer ------------------------------------------------------------------------------

QUERY_TOOL = ea._tool(
    "query_plays",
    "Plays from this game's play-by-play matching every filter, with a split (plays, yards, ypp, EPA/play, "
    "success rate) over all matches. Use 'any' / 0 / -99 / '' to leave a filter off.",
    {"offense": {"type": "string", "description": "Team on offense (Kalshi code) or 'any'"},
     "play_type": {"type": "string", "enum": ["pass", "run", "any"]},
     "quarter": {"type": "integer", "description": "1-5, 0 = any"},
     "down": {"type": "integer", "description": "1-4, 0 = any"},
     "run_location": {"type": "string", "enum": ["left", "middle", "right", "any"]},
     "player": {"type": "string", "description": "Name fragment as in play text, e.g. 'Gibbs', or ''"},
     "min_yards": {"type": "number", "description": "-99 = any"},
     "outcome": {"type": "string", "enum": ["any", "touchdown", "turnover", "sack", "explosive", "first_down", "failed"]},
     "sort": {"type": "string", "enum": ["chrono", "epa", "wpa", "yards"]},
     "limit": {"type": "integer"}},
    ["offense", "play_type", "quarter", "down", "run_location", "player", "min_yards", "outcome", "sort", "limit"],
)

PUBLISH_REVIEW = ea._tool(
    "publish_review",
    "Save the review and both teams' rewritten notebooks. Call exactly once, at the end.",
    {"summary": {"type": "string", "description": "Markdown, 80-160 words: what decided the game, in numbers."},
     "assumption_checks": {"type": "array", "items": {
         "type": "object",
         "properties": {"post_id": {"type": "string"}, "index": {"type": "integer"},
                        "verdict": {"type": "string", "enum": ["held", "failed", "mixed", "untestable"]},
                        "evidence": {"type": "string", "description": "One or two sentences with the numbers."}},
         "required": ["post_id", "index", "verdict", "evidence"], "additionalProperties": False}},
     "reasoning_review": {"type": "array", "items": {
         "type": "object",
         "properties": {"post_id": {"type": "string"},
                        "grade": {"type": "string", "enum": ["sound", "mixed", "flawed"]},
                        "note": {"type": "string", "description": "2-4 sentences: process vs outcome."}},
         "required": ["post_id", "grade", "note"], "additionalProperties": False}},
     "engine_review": {"type": "string", "description": "Was the engine's edge real? Empty if no preview."},
     "lessons": {"type": "array", "items": {"type": "string"}, "description": "0-3 general lessons for future previews."},
     "team_notes": {"type": "array", "items": {
         "type": "object",
         "properties": {"team": {"type": "string"}, "notes": {"type": "string", "description": "The full rewritten notebook, markdown."}},
         "required": ["team", "notes"], "additionalProperties": False}}},
    ["summary", "assumption_checks", "reasoning_review", "engine_review", "lessons", "team_notes"],
)

REVIEW_TOOLS = [QUERY_TOOL, PUBLISH_REVIEW]

REVIEW_PROMPT = """You review finished NFL games for the weekly feed of an NFL research tool for Kalshi prediction markets, and you keep a scouting notebook on every team. The previews you review were written before kickoff by models using the tool's quant engine; your reviews and notebooks are what they read before writing next week's previews, so the point is to make them smarter over time.

You get: the final score and closing line, play-by-play facts for both offenses (splits, run direction, neutral-script pass rate, drive results, red zone, players, the biggest plays by win probability), each team's season game log, the current notebook for both teams, and, if the feed previewed this game, each preview: its write-up, the numbered assumptions it said its bets depended on, a summary of its reasoning, the engine tool calls it made, and its bets with graded results.

About the engine: its game-line fair values are fitted only to Kalshi's own moneyline/spread/total prices (plus a small blend toward the sportsbook line). It knows nothing about injuries or matchups. An engine "edge" means one Kalshi market disagrees with the others, often a thin one. Judge it that way.

What to do
1. Check every numbered assumption against the play-by-play. Use query_plays when the facts given don't settle it (a split, a player, a down, a quarter). Verdict held / failed / mixed / untestable, with the evidence in numbers.
2. Review each preview's reasoning. Separate process from outcome: a sound bet that lost to a tipped interception is not a mistake; a bet that won for reasons the preview never considered is not skill. Name over-reliance on the engine's edge, narratives that argued against the bet, ignored injuries, or reads that were right.
3. Engine review: was the edge real, given the result and what the play-by-play shows about why?
4. Lessons: 0-3 short lessons that generalize to future games (not recaps). Skip if nothing generalizes.
5. Rewrite both teams' notebooks, merging the current notes with this game. Keep each under ~250 words with these sections: **Identity**, **Offense**, **Defense**, **Personnel**, **Trends to watch**, **Betting notes**. Tag new observations with the week, e.g. "(W10)". One game is a data point, not a trend: say how established each item is, and confirm, weaken or drop older items as the evidence says. If a team has no notebook yet, start one from this game and its season log.

Only cite numbers from the data or your query results. Finish by calling publish_review exactly once (both teams in team_notes, by Kalshi code); don't write the review as a normal message."""


def _schedule_row(game_id: str) -> pd.Series:
    season = int(game_id.split("_")[0])
    sched = nd.load_schedules(season)
    rows = sched[sched["game_id"] == game_id]
    if rows.empty:
        raise ValueError(f"unknown game {game_id}")
    return rows.iloc[0]


def _kickoff(row) -> datetime | None:
    from zoneinfo import ZoneInfo
    try:
        return datetime.strptime(f"{row['gameday']} {row['gametime'] or '13:00'}", "%Y-%m-%d %H:%M") \
            .replace(tzinfo=ZoneInfo(ET)).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def posts_for_game(away: str, home: str, week: int) -> list[dict]:
    """Every preview of this game (live + A/B), one per post_id. Kalshi codes."""
    import week_feed as wf
    out = {}
    cands = list(wf._load().values()) + [v for e in wf._load(wf.AB_PATH).values() for v in e["variants"].values()]
    for p in cands:
        if p.get("error") or p.get("away") != away or p.get("home") != home:
            continue
        if p.get("week") not in (None, week):
            continue
        out.setdefault(p.get("post_id") or p.get("created_at"), p)
    return list(out.values())


def _graded_bets(post: dict) -> list[dict]:
    import bet_ledger as bl
    ledger = bl._load()
    out = []
    for i, b in enumerate(post.get("bets") or []):
        g = ledger.get(f"{post.get('post_id')}:{i}") or {}
        graded = g.get("legs") or []
        out.append({"title": b.get("title"), "kind": b.get("kind"), "rationale": b.get("rationale"),
                    "legs": [{"title": l.get("title"), "side": l.get("side"), "fair_at_post": l.get("fair"),
                              "cost": l.get("cost"), "result": graded[li].get("result") if li < len(graded) else None}
                             for li, l in enumerate(b.get("legs") or [])],
                    "outcome": g.get("outcome"), "pnl": g.get("pnl")})
    return out


def _preview_for_review(p: dict) -> dict:
    audit = load_audit(p.get("post_id") or "") or {}
    thinking = "\n\n".join(audit.get("thinking") or [])
    return {"post_id": p.get("post_id"), "model": p.get("model"), "effort": p.get("effort"),
            "headline": p.get("headline"), "lean": p.get("lean"), "confidence": p.get("confidence"),
            "writeup": p.get("writeup"), "key_matchups": p.get("key_matchups"),
            "assumptions": [{"index": i, **a} for i, a in enumerate(p.get("assumptions") or [])],
            "pass_reason": p.get("pass_reason") or None,
            "reasoning_summary": thinking[:8000] or "(not recorded for this preview)",
            "engine_calls": audit.get("trace") or p.get("trace") or [],
            "bets": _graded_bets(p)}


def _bets_settled(posts: list[dict]) -> bool:
    import bet_ledger as bl
    ledger = bl._load()
    for p in posts:
        for i, _ in enumerate(p.get("bets") or []):
            b = ledger.get(f"{p.get('post_id')}:{i}")
            if b and not b.get("graded_at"):
                return False
    return True


def _create(client, messages, model: str, effort: str):
    kwargs = dict(
        model=model, max_tokens=MAX_TOKENS,
        system=[{"type": "text", "text": REVIEW_PROMPT, "cache_control": {"type": "ephemeral"}}],
        tools=REVIEW_TOOLS, messages=messages,
        thinking={"type": "adaptive", "display": "summarized"}, output_config={"effort": effort},
        betas=[ea.FALLBACK_BETA],
    )
    try:
        return client.beta.messages.create(fallbacks="default", **kwargs)
    except TypeError:
        return client.beta.messages.create(extra_body={"fallbacks": "default"}, **kwargs)


def review_game(game_id: str, client=None, model: str = MODEL, effort: str = EFFORT) -> dict:
    """Review one finished game, save it, and update both teams' notebooks."""
    import week_feed as wf
    row = _schedule_row(game_id)
    if pd.isna(row["result"]):
        raise ReviewNotReady(f"{game_id} isn't final yet")
    season, week = int(row["season"]), int(row["week"])
    pbp = nd.load_pbp(season)
    facts = game_facts(pbp, row)
    sched = nd.load_schedules(season)
    away, home = _k(row["away_team"]), _k(row["home_team"])
    posts = posts_for_game(away, home, week)
    if client is None:
        if not ea.chat_enabled():
            raise ea.ChatUnavailable("ANTHROPIC_API_KEY is not set on the server")
        client = ea.get_client()
    ctx = {
        "game_id": game_id, "matchup": f"{away} @ {home}", "week": week, "season": season,
        "facts": facts,
        "season_log": {away: season_log(pbp, sched, row["away_team"], week),
                       home: season_log(pbp, sched, row["home_team"], week)},
        "current_notebooks": {t: (team_notes(t) or {}).get("notes") or "(none yet)" for t in (away, home)},
        "previews": [_preview_for_review(p) for p in posts],
    }
    messages = [{"role": "user", "content": [{"type": "text", "text":
        f"<game>\n{json.dumps(ea.fv_sanitize(ctx), default=str)}\n</game>\n\n"
        f"Review {away} @ {home} (week {week}) and publish it."}]}]
    thinking, trace = [], []
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
            raise ea.EngineError("the model declined to review this game")
        content = list(resp.content or [])
        messages.append({"role": "assistant", "content": content})
        thinking += [b.thinking for b in content if getattr(b, "type", None) == "thinking" and getattr(b, "thinking", "")]
        if stop == "pause_turn":
            continue
        tool_uses = [b for b in content if getattr(b, "type", None) == "tool_use"]
        pub = next((b for b in tool_uses if b.name == "publish_review"), None)
        if pub is not None:
            return _save_review(row, pub.input, posts, served, effort, thinking, trace, usage, t0, wf)
        if stop != "tool_use" or not tool_uses:
            raise ea.EngineError("the reviewer finished without publishing")
        results = []
        for b in tool_uses:
            inp = b.input if isinstance(b.input, dict) else {}
            try:
                if b.name != "query_plays":
                    raise ValueError(f"unknown tool {b.name}")
                res, err = query_plays(pbp, game_id, inp), False
            except Exception as e:
                res, err = {"error": str(e)}, True
            trace.append(f"query_plays {json.dumps({k: v for k, v in inp.items() if v not in ('any', 0, -99, '')})}"
                         f": {res.get('matching', 'error')} plays")
            results.append({"type": "tool_result", "tool_use_id": b.id,
                            "content": json.dumps(ea.fv_sanitize(res), default=str)[:40_000],
                            **({"is_error": True} if err else {})})
        messages.append({"role": "user", "content": results})
    raise ea.EngineError("stopped after the maximum number of tool calls without publishing")


def _save_review(row, inp, posts, served, effort, thinking, trace, usage, t0, wf) -> dict:
    away, home = _k(row["away_team"]), _k(row["home_team"])
    season, week = int(row["season"]), int(row["week"])
    teams = {away, home}
    review = {
        "game_id": row["game_id"], "season": season, "week": week, "away": away, "home": home,
        "matchup": f"{away} @ {home}",
        "final": {away: int(row["away_score"]), home: int(row["home_score"])},
        "event_tickers": sorted({p["event_ticker"] for p in posts if p.get("event_ticker")}),
        "post_ids": [p.get("post_id") for p in posts],
        "summary": inp.get("summary") or "", "assumption_checks": inp.get("assumption_checks") or [],
        "reasoning_review": inp.get("reasoning_review") or [], "engine_review": inp.get("engine_review") or "",
        "lessons": (inp.get("lessons") or [])[:3],
        "model": served, "effort": effort, "created_at": ea._iso(time.time()),
        "thinking": thinking, "trace": trace,
        "stats": {**usage, "cost_usd": wf._usage_cost(usage, served), "seconds": round(time.time() - t0)},
    }
    with _lock:
        rv = reviews()
        rv[row["game_id"]] = review
        _save(REVIEWS_PATH, rv)
    _write_notes([n for n in inp.get("team_notes") or [] if n.get("team") in teams], week, season, row["game_id"])
    return review


# -- which games are due ------------------------------------------------------------------------

def due_games(now: datetime | None = None, force: bool = False, week: int | None = None) -> list[str]:
    """Final games from the last LOOKBACK_DAYS (or one week) without a review,
    whose bets have graded (or that ended long enough ago)."""
    now = now or datetime.now(timezone.utc)
    season = nd.current_season()
    sched = nd.load_schedules(season)
    rows = sched[sched["result"].notna()]
    if week is not None:
        rows = rows[rows["week"] == week]
    done = reviews()
    out = []
    for _, r in rows.iterrows():
        if r["game_id"] in done and not force:
            continue
        ko = _kickoff(r)
        if ko is None or (week is None and (now - ko > timedelta(days=LOOKBACK_DAYS))):
            continue
        if now - ko < READY_AFTER and not force:
            continue
        posts = posts_for_game(_k(r["away_team"]), _k(r["home_team"]), int(r["week"]))
        if not force and not _bets_settled(posts) and now - ko < GIVE_UP_WAITING_ON_BETS:
            continue
        out.append(r["game_id"])
    return out


def review_loop() -> None:
    """Server thread (FEED_AUTO_REVIEW=1): review games as they become due."""
    if os.environ.get("FEED_AUTO_REVIEW") != "1":
        return
    time.sleep(120)
    while True:
        try:
            if ea.chat_enabled():
                for gid in due_games():
                    try:
                        r = review_game(gid)
                        print(f"game_review: {gid} reviewed (${r['stats']['cost_usd']:.3f})", flush=True)
                    except ReviewNotReady as e:
                        print(f"game_review: {gid} not ready: {e}", flush=True)
                    except Exception:
                        traceback.print_exc()
        except Exception:
            traceback.print_exc()
        time.sleep(3600)
