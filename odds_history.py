"""
Kalshi odds history: every market price the app already fetches, recorded to
SQLite so pages can show line movement (sparklines, change since open, movers).

Recording is passive -- kalshi_book calls our listener with every FRESH public
fetch (cache hits and account-scoped paths never reach us), so it costs zero
extra Kalshi requests. Coverage is only as fine as the app's own fetch cadence:
the slate refresher (~30s for core game lines), the page warmer (~7 min per
slate pass) and visitor traffic.

  * The listener only extracts a few fields and enqueues them (never blocks,
    never raises); one writer thread owns all writes.
  * A tick is written only when bid/ask/last changed since the last tick for
    that market, plus a heartbeat every HEARTBEAT_SECONDS so flat markets still
    have points and "value at time T" stays well defined.
  * Prices are integer cents. Mid follows MarketQuote.implied_prob_yes: the
    bid/ask midpoint, or the last trade when the book is empty.

Storage: history/odds.sqlite3 (ODDS_DB_PATH overrides), WAL mode.
"""

from __future__ import annotations

import contextlib
import functools
import os
import queue
import sqlite3
import threading
import time
import traceback
from typing import Any, Iterable

from kalshi_markets import DEF_PLAYER_PROP_SERIES, WEEKLY_PLAYER_PROP_SERIES

DEFAULT_DB_PATH = os.environ.get(
    "ODDS_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "history", "odds.sqlite3"))
HEARTBEAT_SECONDS = float(os.environ.get("ODDS_HEARTBEAT_SECONDS", str(15 * 60)))
QUEUE_MAX = 2000          # fetch payloads waiting for the writer; extras are dropped
MAX_POINTS = 200          # history() downsampling target
SPARK_POINTS = 24

PROP_SERIES = frozenset(WEEKLY_PLAYER_PROP_SERIES) | frozenset(DEF_PLAYER_PROP_SERIES)

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets(
  id INTEGER PRIMARY KEY,
  ticker TEXT NOT NULL UNIQUE,
  event_ticker TEXT,
  series_ticker TEXT,
  title TEXT,
  first_seen_ts INTEGER NOT NULL,
  open_yes_bid INTEGER, open_yes_ask INTEGER, open_last INTEGER,
  last_ts INTEGER NOT NULL,
  last_yes_bid INTEGER, last_yes_ask INTEGER, last_last INTEGER,
  last_volume INTEGER, last_open_interest INTEGER
);
CREATE INDEX IF NOT EXISTS markets_last_ts ON markets(last_ts);
-- Ticks key on the integer market id (not the ~30-byte ticker) to keep rows small.
CREATE TABLE IF NOT EXISTS ticks(
  market_id INTEGER NOT NULL,
  ts INTEGER NOT NULL,
  yes_bid INTEGER, yes_ask INTEGER, last_price INTEGER,
  volume INTEGER, open_interest INTEGER,
  PRIMARY KEY(market_id, ts)
) WITHOUT ROWID;
"""


# -- price helpers --------------------------------------------------------------

def _cents(v: Any) -> int | None:
    if v in (None, ""):
        return None
    try:
        return int(round(float(v) * 100))
    except (TypeError, ValueError):
        return None


def _count(v: Any) -> int | None:
    if v in (None, ""):
        return None
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


MOVERS_MAX_SPREAD = int(os.environ.get("ODDS_MOVERS_MAX_SPREAD", "15"))  # cents


def mid(bid: int | None, ask: int | None, last: int | None) -> float | None:
    """Implied YES price in cents (same rule as MarketQuote.implied_prob_yes):
    the midpoint, or the last trade when the book is empty (bid 0, ask 100)."""
    empty_book = not bid and ask in (None, 100)
    if bid is not None and ask is not None and not empty_book:
        return (bid + ask) / 2
    return float(last) if last is not None else None


def game_key(event_ticker: str | None) -> str:
    """'KXNFLRECYDS-26OCT11LVNE' -> '26OCT11LVNE' (the per-game suffix every
    series shares for one matchup)."""
    parts = (event_ticker or "").split("-")
    return parts[1] if len(parts) > 1 else ""


def kind_of(series: str | None) -> str:
    return "prop" if series in PROP_SERIES else "game"


# -- extraction (runs inside the fetch path: keep it cheap and total) ----------

def _row(m: dict) -> tuple | None:
    ticker = m.get("ticker")
    if not isinstance(ticker, str) or not ticker:
        return None
    # Settled/closed markets have an empty book; only live prices are history.
    if m.get("status") not in (None, "active", "open"):
        return None
    bid, ask, last = _cents(m.get("yes_bid_dollars")), _cents(m.get("yes_ask_dollars")), _cents(m.get("last_price_dollars"))
    if bid is None and ask is None and last is None:
        return None
    event = m.get("event_ticker") or "-".join(ticker.split("-")[:2])
    return (ticker, event, str(m.get("title") or ""), bid, ask, last,
            _count(m.get("volume_fp")), _count(m.get("open_interest_fp")))


def extract_markets(data: Any) -> list[tuple]:
    """Market rows from any Kalshi payload shape that carries prices:
    {events:[{markets}]}, {event:{markets}}, {markets:[...]}, {market:{...}}."""
    if not isinstance(data, dict):
        return []
    found: list[dict] = []
    for ev in data.get("events") or []:
        if isinstance(ev, dict):
            found.extend(ev.get("markets") or [])
    ev = data.get("event")
    if isinstance(ev, dict):
        found.extend(ev.get("markets") or [])
    markets = data.get("markets")
    if isinstance(markets, list):
        found.extend(markets)
    if isinstance(data.get("market"), dict):
        found.append(data["market"])
    rows, seen = [], set()
    for m in found:
        if not isinstance(m, dict):
            continue
        r = _row(m)
        if r and r[0] not in seen:
            seen.add(r[0])
            rows.append(r)
    return rows


# -- storage --------------------------------------------------------------------

def _reads(fn):
    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        with self._session():
            return fn(self, *a, **kw)
    return wrapper


class Store:
    """One SQLite file: a writer thread fed by a queue, plus thread-local
    read connections (WAL lets reads run alongside the writer)."""

    def __init__(self, path: str = DEFAULT_DB_PATH, heartbeat: float = HEARTBEAT_SECONDS):
        self.path = path
        self.heartbeat = heartbeat
        self.queue: "queue.Queue[tuple[int, list[tuple]]]" = queue.Queue(maxsize=QUEUE_MAX)
        self.dropped = 0
        self._local = threading.local()
        self._pool: list[sqlite3.Connection] = []
        self._pool_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last: dict[str, list] | None = None  # writer-only: ticker -> [id, ts, bid, ask, last]
        self._init_lock = threading.Lock()
        self._ready = False

    # connections
    def _connect(self) -> sqlite3.Connection:
        # Switching to WAL and creating the schema take locks that don't wait on
        # the busy timeout, so do both once, before any other connection exists.
        with self._init_lock:
            if not self._ready:
                d = os.path.dirname(self.path)
                if d:
                    os.makedirs(d, exist_ok=True)
                init = sqlite3.connect(self.path, timeout=10)
                try:
                    init.execute("PRAGMA journal_mode=WAL")
                    init.executescript(SCHEMA)
                finally:
                    init.close()
                self._ready = True
        conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _reader(self) -> sqlite3.Connection:
        return self._local.conn

    @contextlib.contextmanager
    def _session(self):
        """Borrow a pooled read connection for this thread (the HTTP server
        runs every request on a fresh thread, so per-thread connections would
        churn)."""
        if getattr(self._local, "conn", None) is not None:
            yield
            return
        with self._pool_lock:
            conn = self._pool.pop() if self._pool else None
        if conn is None:
            conn = self._connect()
        self._local.conn = conn
        try:
            yield
        finally:
            self._local.conn = None
            with self._pool_lock:
                if len(self._pool) < 8:
                    self._pool.append(conn)
                else:
                    conn.close()

    # writing
    def submit(self, rows: list[tuple], ts: float | None = None) -> None:
        """Queue observed market rows; never blocks or raises."""
        if not rows:
            return
        try:
            self.queue.put_nowait((int(ts if ts is not None else time.time()), rows))
        except queue.Full:
            self.dropped += 1

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="odds-history-writer")
            self._thread.start()

    def flush(self, timeout: float = 10.0) -> None:
        """Wait until everything queued so far is written (tests, shutdown)."""
        if self._thread is None:
            self.write_pending()
            return
        deadline = time.time() + timeout
        while self.queue.unfinished_tasks and time.time() < deadline:
            time.sleep(0.01)

    def write_pending(self) -> None:
        """Synchronously write whatever is queued (no writer thread needed)."""
        conn = self._writer_conn()
        batch = []
        while True:
            try:
                batch.append(self.queue.get_nowait())
            except queue.Empty:
                break
        try:
            if batch:
                self._write(conn, batch)
        finally:
            for _ in batch:
                self.queue.task_done()

    def _writer_conn(self) -> sqlite3.Connection:
        conn = getattr(self, "_wconn", None)
        if conn is None:
            conn = self._wconn = self._connect()
        return conn

    def _run(self) -> None:
        conn = self._writer_conn()
        while True:
            batch = [self.queue.get()]
            while len(batch) < 200:
                try:
                    batch.append(self.queue.get_nowait())
                except queue.Empty:
                    break
            try:
                self._write(conn, batch)
            except Exception:
                traceback.print_exc()
                try:
                    conn.rollback()
                except Exception:
                    pass
                self._last = None  # reload from disk; the failed batch is lost
            finally:
                for _ in batch:
                    self.queue.task_done()

    def _load_last(self, conn: sqlite3.Connection) -> dict[str, list]:
        return {t: [i, ts, b, a, l] for i, t, ts, b, a, l in conn.execute(
            "SELECT id, ticker, last_ts, last_yes_bid, last_yes_ask, last_last FROM markets")}

    def _write(self, conn: sqlite3.Connection, batch: list[tuple[int, list[tuple]]]) -> None:
        if self._last is None:
            self._last = self._load_last(conn)
        last = self._last
        ticks: list[tuple] = []
        updates: list[tuple] = []
        with conn:
            for ts, rows in batch:
                for ticker, event, title, bid, ask, lp, vol, oi in rows:
                    st = last.get(ticker)
                    if st is None:
                        cur = conn.execute(
                            "INSERT INTO markets(ticker, event_ticker, series_ticker, title, first_seen_ts,"
                            " open_yes_bid, open_yes_ask, open_last, last_ts, last_yes_bid, last_yes_ask, last_last,"
                            " last_volume, last_open_interest) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (ticker, event, event.split("-", 1)[0], title, ts, bid, ask, lp, ts, bid, ask, lp, vol, oi))
                        last[ticker] = [cur.lastrowid, ts, bid, ask, lp]
                        ticks.append((cur.lastrowid, ts, bid, ask, lp, vol, oi))
                        continue
                    mid_id, last_ts, *prices = st
                    if ts <= last_ts:
                        continue  # same second or an older payload finishing late
                    if prices == [bid, ask, lp] and ts - last_ts < self.heartbeat:
                        continue
                    st[1:] = [ts, bid, ask, lp]
                    ticks.append((mid_id, ts, bid, ask, lp, vol, oi))
                    updates.append((ts, bid, ask, lp, vol, oi, mid_id))
            if ticks:
                conn.executemany("INSERT OR REPLACE INTO ticks VALUES (?,?,?,?,?,?,?)", ticks)
            if updates:
                conn.executemany(
                    "UPDATE markets SET last_ts=?, last_yes_bid=?, last_yes_ask=?, last_last=?, last_volume=?,"
                    " last_open_interest=? WHERE id=?", updates)

    # reading
    def _markets(self, tickers: Iterable[str]) -> dict[str, tuple]:
        tickers = list(dict.fromkeys(t for t in tickers if t))
        out: dict[str, tuple] = {}
        conn = self._reader()
        for i in range(0, len(tickers), 500):
            chunk = tickers[i:i + 500]
            q = ("SELECT ticker, id, event_ticker, series_ticker, title, first_seen_ts, open_yes_bid, open_yes_ask,"
                 " open_last, last_ts, last_yes_bid, last_yes_ask, last_last, last_volume FROM markets"
                 f" WHERE ticker IN ({','.join('?' * len(chunk))})")
            for r in conn.execute(q, chunk):
                out[r[0]] = r
        return out

    def _value_at(self, mid_id: int, ts: float) -> float | None:
        r = self._reader().execute(
            "SELECT yes_bid, yes_ask, last_price FROM ticks WHERE market_id=? AND ts<=? ORDER BY ts DESC LIMIT 1",
            (mid_id, int(ts))).fetchone()
        return mid(*r) if r else None

    def _series(self, mid_id: int, since: float) -> list[tuple]:
        """[(ts, mid, bid, ask)] from `since` on, starting with the value in
        force at `since` (when the market was already being tracked)."""
        conn = self._reader()
        out = []
        r = conn.execute("SELECT ts, yes_bid, yes_ask, last_price FROM ticks WHERE market_id=? AND ts<=?"
                         " ORDER BY ts DESC LIMIT 1", (mid_id, int(since))).fetchone()
        if r:
            out.append((int(since), mid(*r[1:]), r[1], r[2]))
        for ts, b, a, lp in conn.execute("SELECT ts, yes_bid, yes_ask, last_price FROM ticks WHERE market_id=?"
                                         " AND ts>? ORDER BY ts", (mid_id, int(since))):
            out.append((ts, mid(b, a, lp), b, a))
        return out

    @_reads
    def history(self, tickers: list[str], hours: float, max_points: int = MAX_POINTS,
                now: float | None = None) -> dict:
        now = time.time() if now is None else now
        since = now - hours * 3600
        out = {}
        for t, m in self._markets(tickers).items():
            pts = downsample(self._series(m[1], since), max_points)
            out[t] = {
                "title": m[4], "event_ticker": m[2], "series": m[3], "kind": kind_of(m[3]),
                "first_seen_ts": m[5], "last_ts": m[9],
                "open": mid(m[6], m[7], m[8]),
                # [ts, mid, bid, ask]
                "points": [[ts, _r(v), b, a] for ts, v, b, a in pts],
            }
        return {"hours": hours, "now": int(now), "markets": out}

    @_reads
    def summary(self, tickers: list[str], now: float | None = None) -> dict[str, dict]:
        now = time.time() if now is None else now
        out = {}
        for t, m in self._markets(tickers).items():
            cur, opn = mid(m[10], m[11], m[12]), mid(m[6], m[7], m[8])
            h1 = self._value_at(m[1], now - 3600)
            h24 = self._value_at(m[1], now - 86400)
            out[t] = {"open": _r(opn), "h1": _r(h1), "h24": _r(h24), "now": _r(cur),
                      "chg_open": _d(cur, opn), "chg_1h": _d(cur, h1), "chg_24h": _d(cur, h24),
                      "first_seen_ts": m[5], "last_ts": m[9]}
        return out

    @_reads
    def spark(self, mid_id: int, since: float, until: float, n: int = SPARK_POINTS,
              first_seen: float | None = None) -> list:
        """`n` evenly spaced samples of the mid (step function) over
        [max(since, first_seen), until]."""
        start = max(since, first_seen or since)
        pts = self._series(mid_id, start)
        if not pts:
            return []
        return [_r(v) for v in sample(pts, start, until, n)]

    @_reads
    def line_moves(self, tickers: Iterable[str], hours: float = 24, now: float | None = None) -> dict[str, dict]:
        """Compact per-ticker movement for page payloads: open (first recorded),
        now, change since open and since `hours` ago, and a sparkline over the
        last `hours` (or since first seen)."""
        now = time.time() if now is None else now
        since = now - hours * 3600
        out = {}
        for t, m in self._markets(tickers).items():
            cur, opn = mid(m[10], m[11], m[12]), mid(m[6], m[7], m[8])
            ago = self._value_at(m[1], since)
            out[t] = {"open": _r(opn), "now": _r(cur), "chg": _d(cur, opn), "chg_24h": _d(cur, ago),
                      "since": m[5], "spark": self.spark(m[1], since, now, first_seen=m[5])}
        return out

    @_reads
    def movers(self, hours: float = 24, kind: str = "all", limit: int = 50,
               event_suffixes: set[str] | None = None, now: float | None = None) -> dict:
        """Largest mid-price moves over the last `hours`: value now vs value at
        the window start (or first recorded, if the market appeared later).
        `event_suffixes` limits to those games (e.g. this week's '26OCT11LVNE')."""
        now = time.time() if now is None else now
        since = now - hours * 3600
        conn = self._reader()
        rows = conn.execute(
            "SELECT id, ticker, event_ticker, series_ticker, title, first_seen_ts, open_yes_bid, open_yes_ask,"
            " open_last, last_ts, last_yes_bid, last_yes_ask, last_last, last_volume FROM markets WHERE last_ts>=?",
            (int(since),)).fetchall()
        tracked = conn.execute("SELECT COUNT(*), MIN(first_seen_ts) FROM markets").fetchone()
        cands = []
        for r in rows:
            k = kind_of(r[3])
            if kind != "all" and k != kind:
                continue
            if event_suffixes is not None and game_key(r[2]) not in event_suffixes:
                continue
            # A wide book's midpoint swings with no trading (1/69 -> mid 35), so
            # only markets with a tight spread now can rank as movers.
            if r[10] is not None and r[11] is not None and r[11] - r[10] > MOVERS_MAX_SPREAD:
                continue
            cur = mid(r[10], r[11], r[12])
            if cur is None:
                continue
            then = self._value_at(r[0], since)
            if then is None:
                if r[5] < since:
                    continue
                then = mid(r[6], r[7], r[8])
            if then is None or cur == then:
                continue
            cands.append((abs(cur - then), r[13] or 0, r, k, then, cur))
        cands.sort(key=lambda c: (-c[0], -c[1]))
        movers = []
        for _, vol, r, k, then, cur in cands[:limit]:
            movers.append({
                "ticker": r[1], "event_ticker": r[2], "series": r[3], "title": r[4], "kind": k,
                "then": _r(then), "now": _r(cur), "change": _r(cur - then),
                "bid": r[10], "ask": r[11], "volume": vol,
                "first_seen_ts": r[5], "last_ts": r[9],
                "spark": self.spark(r[0], since, now, first_seen=r[5]),
            })
        return {"hours": hours, "kind": kind, "now": int(now), "count": len(cands), "movers": movers,
                "tracking": {"markets": tracked[0], "since": tracked[1]}}

    @_reads
    def stats(self) -> dict:
        conn = self._reader()
        m, t = conn.execute("SELECT (SELECT COUNT(*) FROM markets), (SELECT COUNT(*) FROM ticks)").fetchone()
        return {"markets": m, "ticks": t, "queued": self.queue.qsize(), "dropped": self.dropped}


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 1)


def _d(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else round(a - b, 1)


def downsample(points: list[tuple], max_points: int) -> list[tuple]:
    """Keep at most `max_points`: the first point, then the last point in each
    of max_points-1 equal time buckets (preserves the step shape and the
    current value)."""
    if len(points) <= max_points or max_points < 2:
        return points
    t0, t1 = points[0][0], points[-1][0]
    span = max(t1 - t0, 1)
    buckets = max_points - 1
    out, last_b = [points[0]], None
    for i, p in enumerate(points[1:], 1):
        b = min(int((p[0] - t0) * buckets / span), buckets - 1)
        if b == last_b:
            out[-1] = p
        else:
            out.append(p)
            last_b = b
    return out


def sample(points: list[tuple], start: float, end: float, n: int) -> list[float | None]:
    """Value in force at n evenly spaced times in [start, end] (step function
    over (ts, value, ...) points sorted by ts)."""
    if n < 2 or end <= start:
        return [points[-1][1]] if points else []
    out, j, cur = [], 0, None
    for i in range(n):
        t = start + (end - start) * i / (n - 1)
        while j < len(points) and points[j][0] <= t:
            cur = points[j][1]
            j += 1
        out.append(cur if cur is not None else points[0][1])
    return out


# -- module-level default store + kalshi_book listener ------------------------

_store: Store | None = None
_store_lock = threading.Lock()


def store() -> Store:
    global _store
    with _store_lock:
        if _store is None:
            _store = Store()
        return _store


def on_fetch(path: str, params: dict | None, data: Any) -> None:
    """kalshi_book fetch listener: extract prices, hand them to the writer."""
    rows = extract_markets(data)
    if rows:
        store().submit(rows)


def install() -> Store:
    """Start the writer thread and record every fresh public Kalshi fetch."""
    import kalshi_book
    s = store()
    s.start()
    kalshi_book.add_fetch_listener(on_fetch)
    return s


def line_moves(tickers: Iterable[str], hours: float = 24) -> dict[str, dict]:
    """Movement map for a page payload; {} on any problem so a page build
    never fails because of history."""
    try:
        return store().line_moves([t for t in tickers if t], hours=hours)
    except Exception:
        traceback.print_exc()
        return {}
