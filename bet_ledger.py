"""
Ledger of every bet the week feed recommends, graded from Kalshi settlement.

Bets are recorded the moment a post is written (live feed or A/B variant), so
rewriting a game later never loses them. Each bet becomes due for grading 12
hours after its game ends -- taken as the close time of the game's moneyline
market, which Kalshi closes as soon as a winner is declared -- and is graded
from each leg's settled `result` (yes/no). The server grades in the
background; /api/feed/results serves what was graded in a time window, with a
ready-to-send email subject and body (the hourly results email routine reads
it).

P&L is per $1 staked at the price when the bet was posted: a single or parlay
wins 1/price - 1 or loses 1; a ladder is graded as its legs, $1 each. A leg
that settles as neither yes nor no (void) pushes and drops out of a parlay.
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from kalshi_markets import MarketIndex

LEDGER_PATH = os.path.join(os.path.dirname(__file__), "data", "feed", "ledger.json")
DELAY_HOURS = 12            # grade this long after the game ends
GIVE_UP_HOURS = 72          # legs still unsettled this long after the game: graded as void
_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _load() -> dict:
    try:
        with open(LEDGER_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def _save(ledger: dict) -> None:
    os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
    tmp = LEDGER_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ledger, f, default=str)
    os.replace(tmp, LEDGER_PATH)


# -- recording ------------------------------------------------------------------------------

def record(post: dict, source: str) -> int:
    """Add a post's bets (source 'live' or 'ab'). A post already recorded
    (same post_id) only gains the new source, so promoting an A/B variant to
    the feed doesn't count its bets twice. Returns how many bets were added."""
    pid = post.get("post_id")
    if not pid or not post.get("bets"):
        return 0
    added = 0
    with _lock:
        ledger = _load()
        for i, b in enumerate(post["bets"]):
            bid = f"{pid}:{i}"
            if bid in ledger:
                if source not in ledger[bid]["sources"]:
                    ledger[bid]["sources"].append(source)
                continue
            legs = [{k: l.get(k) for k in ("market_ticker", "side", "title", "cost", "fair")}
                    for l in b.get("legs") or [] if l.get("market_ticker")]
            if not legs:
                continue
            ledger[bid] = {
                "id": bid, "post_id": pid, "event_ticker": post["event_ticker"],
                "matchup": f"{post['away']} @ {post['home']}", "week": post.get("week"),
                "kickoff": post.get("kickoff"), "posted_at": post.get("created_at"),
                "model": post.get("model"), "effort": post.get("effort"), "sources": [source],
                "title": b.get("title"), "kind": b.get("kind") or "single", "legs": legs,
                "price": b.get("parlay_cost") if len(legs) > 1 else legs[0].get("cost"),
                "fair": b.get("joint_fair") if len(legs) > 1 else legs[0].get("fair"),
                "game_end": None, "due_at": None, "graded_at": None, "outcome": None, "pnl": None,
            }
            added += 1
        _save(ledger)
    return added


# -- grading --------------------------------------------------------------------------------

def _market(idx: MarketIndex, ticker: str) -> dict:
    return idx.get(f"/trade-api/v2/markets/{ticker}").get("market") or {}


def _game_end(idx: MarketIndex, event_ticker: str) -> datetime | None:
    """When the game's moneyline market closed on a declared winner; None
    while it's still open."""
    for m in idx.get_event_markets(event_ticker):
        if m.get("result") in ("yes", "no"):
            return _parse(m.get("close_time")) or _parse(m.get("settlement_ts"))
    return None


def grade_bet(bet: dict, markets: dict[str, dict], now: datetime) -> bool:
    """Fill outcome/pnl from settled leg markets. False if a leg hasn't
    settled yet (and the give-up window hasn't passed)."""
    give_up = now >= _parse(bet["game_end"]) + timedelta(hours=GIVE_UP_HOURS)
    for leg in bet["legs"]:
        m = markets.get(leg["market_ticker"]) or {}
        res = m.get("result")
        if res in ("yes", "no"):
            leg["result"] = "win" if res == leg["side"] else "loss"
        elif m.get("status") in ("settled", "finalized") or give_up:
            leg["result"] = "void"
        else:
            return False
    live = [l for l in bet["legs"] if l["result"] != "void"]
    if bet["kind"] == "ladder" and len(bet["legs"]) > 1:
        pnl = sum((1 / l["cost"] - 1) if l["result"] == "win" else -1 for l in live if l.get("cost"))
        wins = sum(l["result"] == "win" for l in live)
        bet["outcome"] = "push" if not live else ("win" if pnl > 0 else "loss" if pnl < 0 else "push")
        bet["record"] = f"{wins}-{len(live) - wins}"
    elif not live:
        bet["outcome"], pnl = "push", 0.0
    elif any(l["result"] == "loss" for l in live):
        bet["outcome"], pnl = "loss", -1.0
    else:
        price = 1.0
        for l in live:
            price *= l.get("cost") or 1.0
        bet["outcome"], pnl = "win", (1 / price - 1) if price else 0.0
    bet["pnl"] = round(pnl, 4)
    bet["graded_at"] = _iso(now)
    return True


def grade_due(now: datetime | None = None, idx: MarketIndex | None = None) -> int:
    """Grade every bet whose game ended at least DELAY_HOURS ago. Returns the
    number graded."""
    now = now or _now()
    with _lock:
        ledger = _load()
    pending = [b for b in ledger.values() if not b["graded_at"]]
    if not pending:
        return 0
    idx = idx or MarketIndex()
    # Only look up games that have kicked off; game end comes from Kalshi.
    events = {b["event_ticker"] for b in pending
              if not b["game_end"] and (_parse(b.get("kickoff")) or now) <= now}
    ends = {}
    for ev in events:
        try:
            ends[ev] = _game_end(idx, ev)
        except Exception:
            traceback.print_exc()
    markets: dict[str, dict] = {}
    graded = 0
    for b in pending:
        if not b["game_end"] and ends.get(b["event_ticker"]):
            b["game_end"] = _iso(ends[b["event_ticker"]])
            b["due_at"] = _iso(ends[b["event_ticker"]] + timedelta(hours=DELAY_HOURS))
        if not b["due_at"] or now < _parse(b["due_at"]):
            continue
        try:
            for l in b["legs"]:
                if l["market_ticker"] not in markets:
                    markets[l["market_ticker"]] = _market(idx, l["market_ticker"])
        except Exception:
            traceback.print_exc()
            continue
        graded += grade_bet(b, markets, now)
    with _lock:
        fresh = _load()
        for b in pending:
            if b["id"] in fresh and not fresh[b["id"]]["graded_at"]:
                fresh[b["id"]] = {**b, "sources": fresh[b["id"]]["sources"]}
        _save(fresh)
    return graded


def grader_loop(interval: int = 600) -> None:
    """Background thread for the server."""
    time.sleep(60)
    while True:
        try:
            n = grade_due()
            if n:
                print(f"bet_ledger: graded {n} bet(s)", flush=True)
        except Exception:
            traceback.print_exc()
        time.sleep(interval)


# -- results ----------------------------------------------------------------------------------

def _label(b: dict) -> str:
    who = (b.get("model") or "?").replace("claude-", "")
    return ("LIVE · " if "live" in b["sources"] else "") + who


def _fmt_money(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):.2f}"


def results(since: datetime, until: datetime) -> dict:
    """Bets graded in [since, until), grouped by game, with an email
    subject/body ready to send (empty when nothing was graded)."""
    bets = [b for b in _load().values()
            if b["graded_at"] and since <= _parse(b["graded_at"]) < until]
    bets.sort(key=lambda b: (b.get("kickoff") or "", b["event_ticker"], _label(b), b["id"]))
    out = {"since": _iso(since), "until": _iso(until), "count": len(bets), "bets": bets,
           "subject": "", "body": ""}
    if not bets:
        return out

    def stake(b):
        return len([l for l in b["legs"] if l.get("result") != "void"]) if b["kind"] == "ladder" and len(b["legs"]) > 1 else 1

    totals: dict[str, list] = {}
    for b in bets:
        t = totals.setdefault(_label(b).replace("LIVE · ", ""), [0, 0, 0, 0.0, 0])
        t[{"win": 0, "loss": 1, "push": 2}[b["outcome"]]] += 1
        t[3] += b["pnl"]
        t[4] += stake(b) if b["outcome"] != "push" else 0
    games = list(dict.fromkeys(b["matchup"] for b in bets))
    out["subject"] = f"PropsTerminal bet results: {', '.join(games)}"
    lines = [f"Engine bet results, graded {DELAY_HOURS}h after each game ended.",
             "P&L is per $1 staked at the price when the bet was posted.", ""]
    for game in games:
        lines.append(f"== {game} ==")
        for b in (x for x in bets if x["matchup"] == game):
            price = f"{b['price'] * 100:.0f}c" if b.get("price") else "--"
            rec = f" ({b['record']})" if b.get("record") else ""
            lines.append(f"[{_label(b)}] {b['title']} ({b['kind']}, posted at {price}): "
                         f"{b['outcome'].upper()}{rec} {_fmt_money(b['pnl'])}")
            for l in b["legs"]:
                lines.append(f"    {l['side'].upper()} {l.get('title') or l['market_ticker']} @ "
                             f"{(l.get('cost') or 0) * 100:.0f}c: {l.get('result', '?')}")
        lines.append("")
    lines.append("Totals by model (W-L-P, P&L, ROI):")
    for who, (w, l, p, pnl, staked) in totals.items():
        roi = f"{pnl / staked * 100:+.1f}%" if staked else "--"
        lines.append(f"  {who}: {w}-{l}-{p}, {_fmt_money(pnl)}, ROI {roi}")
    out["body"] = "\n".join(lines)
    return out


def last_hour_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """The previous whole clock hour (UTC), e.g. 14:00-15:00 when called at 15:20."""
    now = now or _now()
    until = now.replace(minute=0, second=0, microsecond=0)
    return until - timedelta(hours=1), until
