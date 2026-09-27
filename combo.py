"""
Combo (Kalshi "multivariate event" market; a parlay in sportsbook terms)
builder: leg validation, independent pricing, and RFQ quotes.

How Kalshi combos work (confirmed against the live API + docs.kalshi.com):

  * A combo is a market inside a multivariate event *collection*
    (e.g. KXMVECROSSCATEGORY-R). The collection lists which events may be
    used as legs (`associated_events`), with a per-event leg cap
    (`size_max`, None = uncapped) and an `is_yes_only` flag, plus overall
    `size_min`/`size_max` leg counts (size_max 0 = no cap).
      - NFL game-level events (moneyline, spread, totals, team total, 1H/1Q)
        cap at 1 leg per event.
      - Player-prop events (pass yds, rec, ATD, ...) are uncapped, so several
        players' lines from the same game can be combined.
      - Pass attempts/completions, rush attempts and longest-play props are
        NOT in any collection -- they can't be combo legs at all.
  * POST /multivariate_event_collections/{collection} with the selected legs
    creates (or returns the existing) combo market ticker.
  * Combo markets have no resting book; pricing comes from RFQs:
    POST /communications/rfqs -> makers reply with quotes carrying
    yes_bid/no_bid (maker bids; yes_bid + no_bid <= $1), then the requester
    accepts one side and the maker confirms (3s window for combos).
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

from kalshi_book import KalshiClient, public_get

# Most NFL combo volume sits in CROSSCATEGORY-R; the others are overflow
# shards / alternates with the same NFL event list.
PREFERRED_COLLECTIONS = (
    "KXMVECROSSCATEGORY-R",
    "KXMVESPORTSMULTIGAMEEXTENDED-R",
    "KXMVECROSSCATEGORY-SHARD1-R",
)

# Placing a combo is real money and the RFQ accept-side semantics are only
# loosely documented, so accepting quotes is opt-in.
ACCEPT_ENABLED = os.environ.get("KALSHI_ENABLE_COMBO_ACCEPT") == "1"


class ComboError(Exception):
    pass


def event_ticker_of(market_ticker: str) -> str:
    """Every NFL weekly market ticker is "{SERIES}-{GAME}-..." and its event
    is the first two segments."""
    return "-".join(market_ticker.split("-")[:2])


# -- collections ------------------------------------------------------------


def open_collections() -> list[dict]:
    data = public_get("/trade-api/v2/multivariate_event_collections", params={"status": "open", "limit": 200})
    cols = data.get("multivariate_contracts", [])
    rank = {c: i for i, c in enumerate(PREFERRED_COLLECTIONS)}
    return sorted(cols, key=lambda c: rank.get(c["collection_ticker"], len(rank)))


def _event_rules(collection: dict) -> dict[str, dict]:
    return {e["ticker"]: e for e in collection.get("associated_events") or []}


def eligible_events_for_game(game_suffix: str) -> dict:
    """Which of one game's events can be combo legs, per the preferred open
    collection. Used by the player payload to grey out ineligible rows."""
    try:
        cols = open_collections()
    except Exception:
        return {"collection": None, "events": []}
    for col in cols:
        evs = [t for t in _event_rules(col) if t.endswith("-" + game_suffix)]
        if evs:
            return {"collection": col["collection_ticker"], "events": sorted(evs)}
    return {"collection": None, "events": []}


# -- pricing ----------------------------------------------------------------


def _dollars(m: dict, key: str) -> float | None:
    v = m.get(key)
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v


def leg_prices(market: dict, side: str) -> dict:
    """Implied probability (mid) and cost to buy `side` as a single."""
    bid, ask, last = _dollars(market, "yes_bid_dollars"), _dollars(market, "yes_ask_dollars"), _dollars(market, "last_price_dollars")
    if bid and ask:
        mid_yes = (bid + ask) / 2
    else:
        mid_yes = last or ask or bid
    if side == "yes":
        prob = mid_yes
        cost = ask or None
    else:
        prob = None if mid_yes is None else 1 - mid_yes
        cost = (1 - bid) if bid else None
    r4 = lambda x: None if x is None else round(x, 4)
    return {"prob": r4(prob), "cost": r4(cost)}


def american_odds(p: float | None) -> int | None:
    if not p or p <= 0 or p >= 1:
        return None
    return round(-100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)


def parlay_math(probs: list[float | None]) -> dict:
    """Running product of leg probabilities, as if independent. `units` is
    profit per 1 unit staked at that price (1/p - 1)."""
    running = []
    p = 1.0
    for q in probs:
        if q is None:
            p = None
        elif p is not None:
            p *= q
        running.append({"prob": p, "units": (1 / p - 1) if p else None, "american": american_odds(p)})
    return {"running": running, "final": running[-1] if running else None}


# -- validation ---------------------------------------------------------------


def _family_and_strike(ticker: str) -> tuple[str, float] | None:
    """Player prop tickers are {SERIES}-{GAME}-{PLAYER}-{STRIKE}; the strike
    is the "N+" line. Returns ((series, player), strike) or None for
    team/game markets (those are capped at one leg per event anyway)."""
    parts = ticker.split("-")
    if len(parts) != 4:
        return None
    try:
        return (parts[0] + "|" + parts[2], float(parts[3]))
    except ValueError:
        return None


def validate(legs: list[dict]) -> dict:
    """legs: [{market_ticker, side}]. Returns per-leg checks, combo-level
    issues, the collection to use, fresh prices, and independent parlay math.

    Uses only public market data, so it works without Kalshi credentials."""
    legs = [
        {"market_ticker": l["market_ticker"], "side": (l.get("side") or "yes").lower()}
        for l in legs if l.get("market_ticker")
    ]
    issues: list[dict] = []

    def fetch(t):
        try:
            return public_get(f"/trade-api/v2/markets/{t}").get("market")
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=8) as ex:
        markets = list(ex.map(fetch, [l["market_ticker"] for l in legs]))

    out_legs = []
    for leg, m in zip(legs, markets):
        row = {**leg, "event_ticker": (m or {}).get("event_ticker") or event_ticker_of(leg["market_ticker"]),
               "title": (m or {}).get("title"), "status": (m or {}).get("status"),
               "yes_bid": _dollars(m or {}, "yes_bid_dollars"), "yes_ask": _dollars(m or {}, "yes_ask_dollars"),
               "errors": [], "warnings": []}
        if leg["side"] not in ("yes", "no"):
            row["errors"].append("side must be yes or no")
        if m is None:
            row["errors"].append("market not found on Kalshi")
        elif m.get("status") not in ("active", "open"):
            row["errors"].append(f"market is {m.get('status')}, not tradable")
        row.update(leg_prices(m or {}, leg["side"]))
        out_legs.append(row)

    # Pick the first open collection that accepts every leg's event.
    try:
        cols = open_collections()
    except Exception as e:
        cols = []
        issues.append({"level": "error", "msg": f"couldn't load Kalshi combo collections: {e}"})
    chosen = None
    for col in cols:
        rules = _event_rules(col)
        if all(l["event_ticker"] in rules for l in out_legs):
            chosen = col
            break
    if chosen is None and cols:
        # Report against the preferred collection so the reasons are specific.
        rules = _event_rules(cols[0])
        for l in out_legs:
            if l["event_ticker"] not in rules:
                l["errors"].append("this stat isn't offered in Kalshi combos")

    if chosen:
        rules = _event_rules(chosen)
        per_event: dict[str, int] = {}
        for l in out_legs:
            per_event[l["event_ticker"]] = per_event.get(l["event_ticker"], 0) + 1
        for l in out_legs:
            r = rules[l["event_ticker"]]
            cap = r.get("size_max")
            if cap and per_event[l["event_ticker"]] > cap:
                l["errors"].append(f"Kalshi allows only {cap} leg{'s' if cap > 1 else ''} from this market group")
            if r.get("is_yes_only") and l["side"] != "yes":
                l["errors"].append("only YES is allowed on this market in combos")
        n = len(out_legs)
        if n < (chosen.get("size_min") or 2):
            issues.append({"level": "error", "msg": f"pick at least {chosen.get('size_min') or 2} legs"})
        if chosen.get("size_max") and n > chosen["size_max"]:
            issues.append({"level": "error", "msg": f"at most {chosen['size_max']} legs"})
    elif len(out_legs) < 2:
        issues.append({"level": "error", "msg": "pick at least 2 legs"})

    # Duplicate tickers, and contradictory / redundant lines on the same
    # player+stat. A YES on strike a needs stat >= a; a NO on strike b needs
    # stat < b; both can only hold when a < b.
    seen: dict[str, int] = {}
    by_family: dict[str, list[tuple[int, float, str]]] = {}
    for i, l in enumerate(out_legs):
        if l["market_ticker"] in seen:
            l["errors"].append("same market picked twice")
        seen[l["market_ticker"]] = i
        fs = _family_and_strike(l["market_ticker"])
        if fs:
            by_family.setdefault(fs[0], []).append((i, fs[1], l["side"]))
    for entries in by_family.values():
        for a in range(len(entries)):
            for b in range(a + 1, len(entries)):
                (i, si, di), (j, sj, dj) = entries[a], entries[b]
                if di == dj:
                    msg = "overlaps another line on the same stat (the stricter line already implies it)"
                    out_legs[i]["warnings"].append(msg)
                    out_legs[j]["warnings"].append(msg)
                else:
                    yes_strike, no_strike = (si, sj) if di == "yes" else (sj, si)
                    if yes_strike >= no_strike:
                        msg = "contradicts another leg on the same stat -- both can't hit"
                        out_legs[i]["errors"].append(msg)
                        out_legs[j]["errors"].append(msg)

    for l in out_legs:
        l["ok"] = not l["errors"]
    pricing = parlay_math([l["prob"] for l in out_legs])
    cost = parlay_math([l["cost"] for l in out_legs])["final"]
    ok = bool(out_legs) and all(l["ok"] for l in out_legs) and not any(x["level"] == "error" for x in issues)
    return {
        "ok": ok,
        "collection": chosen["collection_ticker"] if chosen else None,
        "legs": out_legs,
        "issues": issues,
        "pricing": pricing,
        "singles_cost": cost,
        "accept_enabled": ACCEPT_ENABLED,
    }


# -- quoting (authenticated) ----------------------------------------------------


def _selected(legs: list[dict]) -> list[dict]:
    return [{"market_ticker": l["market_ticker"], "event_ticker": l["event_ticker"], "side": l["side"]} for l in legs]


def request_quote(legs: list[dict], target_cost_dollars: float) -> dict:
    """Validate, create/lookup the combo market, and open an RFQ sized by
    dollars to spend. Returns the combo ticker and RFQ id to poll."""
    v = validate(legs)
    if not v["ok"]:
        raise ComboError("combo isn't valid yet: " + "; ".join(
            [x["msg"] for x in v["issues"]] + [f"{l['market_ticker']}: {e}" for l in v["legs"] for e in l["errors"]]))
    if target_cost_dollars <= 0:
        raise ComboError("stake must be positive")
    client = KalshiClient()
    created = client.post(
        f"/trade-api/v2/multivariate_event_collections/{v['collection']}",
        {"selected_markets": _selected(v["legs"]), "with_market_payload": True},
    )
    ticker = created["market_ticker"]
    rfq = client.post("/trade-api/v2/communications/rfqs", {
        "market_ticker": ticker,
        "target_cost_dollars": f"{target_cost_dollars:.2f}",
        "rest_remainder": False,
        # 409s if we already have an open RFQ on this exact combo; replace it.
        "replace_existing": True,
    })
    market = created.get("market") or {}
    return {
        "collection": v["collection"],
        "market_ticker": ticker,
        "event_ticker": created.get("event_ticker"),
        "title": market.get("title"),
        "rfq_id": rfq["id"],
        "validation": v,
    }


def _quote_view(q: dict) -> dict:
    """Maker quotes are *bids*: the maker buys YES at yes_bid or NO at
    no_bid. So taking the combo's YES costs 1 - no_bid (you're the other side
    of the maker's NO bid), and taking NO costs 1 - yes_bid."""
    yb, nb = _dollars(q, "yes_bid_dollars") or 0.0, _dollars(q, "no_bid_dollars") or 0.0
    yes_cost = round(1 - nb, 4) if nb > 0 else None
    no_cost = round(1 - yb, 4) if yb > 0 else None
    return {
        "id": q.get("id"), "status": q.get("status"),
        "yes_bid": yb, "no_bid": nb,
        "yes_cost": yes_cost, "no_cost": no_cost,
        "yes_units": (1 / yes_cost - 1) if yes_cost else None,
        "yes_american": american_odds(yes_cost),
        "contracts_yes": q.get("yes_contracts_fp"), "contracts_no": q.get("no_contracts_fp"),
        "created_ts": q.get("created_ts"), "updated_ts": q.get("updated_ts"),
    }


def get_quotes(rfq_id: str) -> dict:
    client = KalshiClient()
    # _request, not get(): quotes must never come from the disk cache.
    data = client._request("GET", "/trade-api/v2/communications/quotes", params={"rfq_id": rfq_id})
    quotes = [_quote_view(q) for q in data.get("quotes", [])]
    live = [q for q in quotes if q["status"] in (None, "open", "active") and q["yes_cost"]]
    best = min(live, key=lambda q: q["yes_cost"]) if live else None
    rfq = {}
    try:
        rfq = client._request("GET", f"/trade-api/v2/communications/rfqs/{rfq_id}").get("rfq", {})
    except Exception:
        pass
    return {"rfq_id": rfq_id, "rfq_status": rfq.get("status"), "quotes": quotes, "best": best}


def cancel_rfq(rfq_id: str) -> dict:
    KalshiClient().delete(f"/trade-api/v2/communications/rfqs/{rfq_id}")
    return {"rfq_id": rfq_id, "cancelled": True}


def accept_quote(rfq_id: str, quote_id: str, side: str) -> dict:
    if not ACCEPT_ENABLED:
        raise ComboError("accepting quotes is disabled; start the server with KALSHI_ENABLE_COMBO_ACCEPT=1")
    if side not in ("yes", "no"):
        raise ComboError("side must be yes or no")
    KalshiClient().put(
        f"/trade-api/v2/communications/rfqs/{rfq_id}/quotes/{quote_id}/accept", {"accepted_side": side}
    )
    return {"rfq_id": rfq_id, "quote_id": quote_id, "accepted_side": side, "status": "accepted, awaiting maker confirm"}
