"""Odds history: recording, the kalshi_book listener hook, and the read API."""

from __future__ import annotations

import json
import threading

import pytest
import requests

import kalshi_book as kb
import odds_history as oh

T0 = 1_760_000_000


def mkt(ticker: str, bid: float | None, ask: float | None, last: float | None = None, status: str = "active",
        title: str = "", volume: float = 10) -> dict:
    d = lambda v: None if v is None else f"{v:.4f}"
    return {"ticker": ticker, "event_ticker": "-".join(ticker.split("-")[:2]), "title": title or ticker, "status": status,
            "yes_bid_dollars": d(bid), "yes_ask_dollars": d(ask), "last_price_dollars": d(last),
            "volume_fp": f"{volume:.2f}", "open_interest_fp": "5.00"}


@pytest.fixture
def store(tmp_path):
    return oh.Store(str(tmp_path / "odds.sqlite3"), heartbeat=900)


def record(store, ts, *markets):
    store.submit(oh.extract_markets({"markets": list(markets)}), ts=ts)
    store.write_pending()


def ticks(store, ticker):
    with store._session():
        conn = store._reader()
        mid_id = conn.execute("SELECT id FROM markets WHERE ticker=?", (ticker,)).fetchone()[0]
        return conn.execute("SELECT ts, yes_bid, yes_ask, last_price FROM ticks WHERE market_id=? ORDER BY ts",
                            (mid_id,)).fetchall()


ML = "KXNFLGAME-26OCT11LVNE-NE"
PROP = "KXNFLRECYDS-26OCT11LVNE-NEXYZ1-50"


# -- recording -------------------------------------------------------------------

def test_change_only_recording_and_heartbeat(store):
    record(store, T0, mkt(ML, 0.60, 0.62, 0.61))
    record(store, T0 + 30, mkt(ML, 0.60, 0.62, 0.61))       # unchanged -> skipped
    record(store, T0 + 60, mkt(ML, 0.61, 0.63, 0.61))       # bid/ask moved
    record(store, T0 + 90, mkt(ML, 0.61, 0.63, 0.61))       # unchanged
    record(store, T0 + 60 + 899, mkt(ML, 0.61, 0.63, 0.61))  # just under heartbeat
    record(store, T0 + 60 + 900, mkt(ML, 0.61, 0.63, 0.61))  # heartbeat
    record(store, T0 + 1000, mkt(ML, 0.61, 0.63, 0.62))     # last trade moved
    assert ticks(store, ML) == [(T0, 60, 62, 61), (T0 + 60, 61, 63, 61), (T0 + 960, 61, 63, 61),
                                (T0 + 1000, 61, 63, 62)]


def test_out_of_order_and_same_second_ignored(store):
    record(store, T0 + 100, mkt(ML, 0.50, 0.52))
    record(store, T0 + 50, mkt(ML, 0.40, 0.42))
    record(store, T0 + 100, mkt(ML, 0.45, 0.47))
    assert ticks(store, ML) == [(T0 + 100, 50, 52, None)]


def test_open_and_last_columns(store):
    record(store, T0, mkt(ML, 0.40, 0.42, 0.41))
    record(store, T0 + 60, mkt(ML, 0.50, 0.52, 0.51))
    with store._session():
        r = store._reader().execute("SELECT series_ticker, event_ticker, first_seen_ts, open_yes_bid, open_yes_ask,"
                                    " open_last, last_ts, last_yes_bid FROM markets").fetchone()
    assert r == ("KXNFLGAME", "KXNFLGAME-26OCT11LVNE", T0, 40, 42, 41, T0 + 60, 50)


def test_state_survives_restart(store, tmp_path):
    record(store, T0, mkt(ML, 0.40, 0.42))
    again = oh.Store(store.path, heartbeat=900)
    record(again, T0 + 60, mkt(ML, 0.40, 0.42))  # unchanged vs what's on disk
    record(again, T0 + 120, mkt(ML, 0.45, 0.47))
    assert [t[0] for t in ticks(again, ML)] == [T0, T0 + 120]


def test_writer_thread(store):
    store.start()
    for i in range(5):
        store.submit(oh.extract_markets({"market": mkt(ML, 0.40 + i / 100, 0.50)}), ts=T0 + i)
    store.flush()
    assert len(ticks(store, ML)) == 5


def test_submit_never_blocks_when_full(store, monkeypatch):
    for _ in range(oh.QUEUE_MAX + 5):
        store.submit([("X-1", "X", "", 1, 2, 1, 0, 0)], ts=T0)
    assert store.dropped == 5


# -- extraction -------------------------------------------------------------------

def test_extract_each_payload_shape():
    a, b, c, d = (mkt(f"KXNFLTOTAL-26OCT11LVNE-{n}", 0.4, 0.5) for n in (40, 43, 46, 49))
    assert [r[0] for r in oh.extract_markets({"events": [{"markets": [a]}, {"markets": [b]}], "cursor": ""})] == \
        [a["ticker"], b["ticker"]]
    assert [r[0] for r in oh.extract_markets({"event": {"event_ticker": "E", "markets": [c]}})] == [c["ticker"]]
    assert [r[0] for r in oh.extract_markets({"markets": [d], "cursor": None})] == [d["ticker"]]
    assert [r[0] for r in oh.extract_markets({"market": a})] == [a["ticker"]]
    # event without nested markets + top-level markets (with_nested_markets=False)
    assert [r[0] for r in oh.extract_markets({"event": {"event_ticker": "E"}, "markets": [b]})] == [b["ticker"]]


def test_extract_skips_settled_unpriced_and_junk():
    rows = oh.extract_markets({"markets": [
        mkt("A-1", 0.4, 0.5), mkt("A-2", 0.0, 1.0, 1.0, status="finalized"),
        mkt("A-3", None, None, None), {"no_ticker": 1}, "junk", mkt("A-1", 0.4, 0.5)]})
    assert [r[0] for r in rows] == ["A-1"]
    assert oh.extract_markets({"orderbook": {}}) == []
    assert oh.extract_markets(None) == []
    assert oh.extract_markets({"structured_target": {"name": "x"}}) == []


def test_extract_row_values():
    (row,) = oh.extract_markets({"market": mkt(PROP, 0.335, 0.37, 0.35, title="X: 50+", volume=1234.6)})
    assert row == (PROP, "KXNFLRECYDS-26OCT11LVNE", "X: 50+", 34, 37, 35, 1235, 5)


def test_mid_matches_market_quote_rule():
    assert oh.mid(40, 44, 10) == 42
    assert oh.mid(0, 100, 37) == 37      # empty book -> last trade
    assert oh.mid(None, None, 37) == 37
    assert oh.mid(0, 5, None) == 2.5     # one-sided but not empty


# -- kalshi_book listener hook ------------------------------------------------------

@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(kb, "_API_CACHE_DIR", str(tmp_path / "kalshi_api"))
    monkeypatch.setattr(kb, "CACHE_TTL_SECONDS", 20.0)
    monkeypatch.setattr(kb, "_fetch_listeners", [])
    return tmp_path


def test_listener_sees_fresh_fetch_not_cache_hit(isolated_cache):
    seen = []
    kb.add_fetch_listener(lambda path, params, data: seen.append((path, params, data)))
    payload = {"markets": [mkt(ML, 0.4, 0.5)]}
    calls = []

    def fetch():
        calls.append(1)
        return payload

    assert kb._cached_get("/trade-api/v2/markets", {"event_ticker": "E"}, fetch) == payload
    assert kb._cached_get("/trade-api/v2/markets", {"event_ticker": "E"}, fetch) == payload  # disk cache hit
    assert len(calls) == 1
    assert seen == [("/trade-api/v2/markets", {"event_ticker": "E"}, payload)]


def test_raising_listener_does_not_break_fetch(isolated_cache, capsys):
    good = []

    def bad(path, params, data):
        raise RuntimeError("boom")

    kb.add_fetch_listener(bad)
    kb.add_fetch_listener(lambda p, q, d: good.append(p))
    assert kb._cached_get("/trade-api/v2/events/E", None, lambda: {"event": {}}) == {"event": {}}
    assert good == ["/trade-api/v2/events/E"]
    assert "boom" in capsys.readouterr().out


def test_portfolio_and_communications_never_notify(isolated_cache):
    seen = []
    kb.add_fetch_listener(lambda *a: seen.append(a))
    kb._cached_get("/trade-api/v2/portfolio/positions", None, lambda: {"market": mkt(ML, 0.4, 0.5)})
    kb._cached_get("/trade-api/v2/communications/rfqs", None, lambda: {"markets": [mkt(ML, 0.4, 0.5)]})
    assert seen == []


def test_404_does_not_notify(isolated_cache):
    seen = []
    kb.add_fetch_listener(lambda *a: seen.append(a))

    def fetch():
        resp = requests.Response()
        resp.status_code = 404
        raise requests.exceptions.HTTPError("404", response=resp)

    with pytest.raises(requests.exceptions.HTTPError):
        kb._cached_get("/trade-api/v2/events/NOPE", None, fetch)
    with pytest.raises(requests.exceptions.HTTPError):
        kb._cached_get("/trade-api/v2/events/NOPE", None, fetch)  # cached 404
    assert seen == []


def test_uncached_public_fetch_still_notifies(isolated_cache, monkeypatch):
    monkeypatch.setattr(kb, "CACHE_TTL_SECONDS", 0.0)
    seen = []
    kb.add_fetch_listener(lambda p, q, d: seen.append(p))
    kb._cached_get("/trade-api/v2/markets/X", None, lambda: {"market": {}})
    kb._cached_get("/trade-api/v2/portfolio/balance", None, lambda: {"balance": 1})
    assert seen == ["/trade-api/v2/markets/X"]


def test_on_fetch_records_through_listener(isolated_cache, tmp_path, monkeypatch):
    s = oh.Store(str(tmp_path / "h.sqlite3"))
    monkeypatch.setattr(oh, "_store", s)
    kb.add_fetch_listener(oh.on_fetch)
    kb._cached_get("/trade-api/v2/events", {"series_ticker": "KXNFLGAME"},
                   lambda: {"events": [{"markets": [mkt(ML, 0.4, 0.5)]}]})
    s.write_pending()
    assert s.stats()["ticks"] == 1


# -- read API ----------------------------------------------------------------------

def test_downsample_keeps_first_last_and_bound():
    pts = [(T0 + i, float(i)) for i in range(1000)]
    out = oh.downsample(pts, 200)
    assert len(out) <= 200
    assert out[0] == pts[0] and out[-1] == pts[-1]
    assert [p[0] for p in out] == sorted(p[0] for p in out)
    assert oh.downsample(pts[:50], 200) == pts[:50]


def test_sample_step_function():
    pts = [(0, 10.0), (50, 20.0), (100, 30.0)]
    assert oh.sample(pts, 0, 100, 5) == [10.0, 10.0, 20.0, 20.0, 30.0]


def test_history_and_summary(store):
    record(store, T0, mkt(ML, 0.40, 0.42))
    record(store, T0 + 3600, mkt(ML, 0.50, 0.52))
    record(store, T0 + 90000, mkt(ML, 0.60, 0.62))
    now = T0 + 90000 + 60
    h = store.history([ML, "MISSING"], hours=48, now=now)["markets"]
    assert list(h) == [ML]
    assert [p[1] for p in h[ML]["points"]] == [41, 51, 61]
    h = store.history([ML], hours=24, now=now)["markets"][ML]
    assert h["points"][0][:2] == [now - 86400, 51]  # value in force at the window start
    s = store.summary([ML], now=now)[ML]
    assert (s["open"], s["h1"], s["h24"], s["now"]) == (41, 51, 51, 61)
    assert (s["chg_open"], s["chg_1h"], s["chg_24h"]) == (20, 10, 10)


def test_line_moves(store):
    record(store, T0, mkt(ML, 0.40, 0.42))
    record(store, T0 + 600, mkt(ML, 0.44, 0.46))
    lm = store.line_moves([ML, PROP], now=T0 + 1200)
    assert list(lm) == [ML]
    assert lm[ML]["chg"] == 4 and lm[ML]["chg_24h"] is None  # not tracked 24h ago
    assert lm[ML]["spark"][0] == 41 and lm[ML]["spark"][-1] == 45 and len(lm[ML]["spark"]) == oh.SPARK_POINTS


def test_movers_ranking_kind_and_week_filter(store):
    other = "KXNFLSPREAD-26OCT18KCBUF-KC3"
    season = "KXNFLSEASONRECYDS-27C1000-JC1"
    record(store, T0, mkt(ML, 0.40, 0.42), mkt(PROP, 0.30, 0.32), mkt(other, 0.50, 0.52), mkt(season, 0.2, 0.22))
    record(store, T0 + 3600, mkt(ML, 0.45, 0.47), mkt(PROP, 0.10, 0.12), mkt(other, 0.51, 0.53),
           mkt(season, 0.5, 0.52))
    now = T0 + 7200
    m = store.movers(hours=24, now=now)
    assert [x["ticker"] for x in m["movers"]] == [season, PROP, ML, other]
    assert m["movers"][1]["change"] == -20 and m["movers"][1]["kind"] == "prop"
    assert [x["ticker"] for x in store.movers(hours=24, kind="game", now=now)["movers"]] == [season, ML, other]
    assert [x["ticker"] for x in store.movers(hours=24, kind="prop", now=now)["movers"]] == [PROP]
    week = store.movers(hours=24, event_suffixes={"26OCT11LVNE"}, now=now, limit=1)
    assert [x["ticker"] for x in week["movers"]] == [PROP] and week["count"] == 2
    # window that starts after the move: nothing moved since
    assert store.movers(hours=0.25, now=now)["movers"] == []
    assert m["tracking"]["markets"] == 4


def test_movers_skip_wide_books(store):
    wide = "KXNFLRSHYDS-26OCT11LVNE-NEDMAYE10-80"
    record(store, T0, mkt(ML, 0.40, 0.42), mkt(wide, 0.08, 0.09))
    record(store, T0 + 3600, mkt(ML, 0.45, 0.47), mkt(wide, 0.01, 0.69))  # mid 8.5 -> 35, no trading
    assert [x["ticker"] for x in store.movers(hours=24, now=T0 + 7200)["movers"]] == [ML]


def test_reads_on_empty_db(store):
    assert store.movers(now=T0)["movers"] == []
    assert store.history(["X"], 24)["markets"] == {}
    assert store.line_moves(["X"]) == {}


def test_concurrent_reads_while_writing(store):
    store.start()
    errors = []

    def reader():
        try:
            for _ in range(50):
                store.movers(hours=24)
                store.line_moves([ML])
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for i in range(200):
        store.submit(oh.extract_markets({"market": mkt(ML, (i % 90) / 100, 0.99)}), ts=T0 + i)
    for t in threads:
        t.join()
    store.flush()
    assert errors == []
    assert len(ticks(store, ML)) == 200
