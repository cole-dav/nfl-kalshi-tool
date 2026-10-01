"""
Shared, precomputed API payloads so visitor traffic never fans out upstream.

Every public GET endpoint reads through `get(key, build, ttl)`:

  * fresh entry          -> returned as is
  * stale entry          -> returned as is, and one background thread rebuilds it
  * missing entry        -> built once; concurrent callers for the same key wait
                            for that single build instead of each starting one
  * build fails          -> the last good payload keeps being served

So upstream load (Kalshi, nflverse, Google News, ...) scales with the number of
distinct keys and their TTLs, not with the number of visitors. The server's
refresher thread also calls `refresh()` on the current slate's keys so the
common pages are rebuilt before anyone asks for them.

Entries are in memory only and capped at MAX_ENTRIES (least recently used
dropped first); keys come from user input (player names), so the cap matters.
"""

from __future__ import annotations

import threading
import time
import traceback
from collections import OrderedDict
from typing import Any, Callable

MAX_ENTRIES = 2000


class _Entry:
    __slots__ = ("data", "built_at", "ttl", "lock", "refreshing")

    def __init__(self, ttl: float):
        self.data: Any = None
        self.built_at = 0.0
        self.ttl = ttl
        self.lock = threading.Lock()
        self.refreshing = False


_entries: "OrderedDict[str, _Entry]" = OrderedDict()
_lock = threading.Lock()


def _entry(key: str, ttl: float) -> _Entry:
    with _lock:
        e = _entries.get(key)
        if e is None:
            e = _entries[key] = _Entry(ttl)
            while len(_entries) > MAX_ENTRIES:
                _entries.popitem(last=False)
        else:
            _entries.move_to_end(key)
            e.ttl = ttl
        return e


def _build_into(e: _Entry, build: Callable[[], Any]) -> None:
    data = build()
    e.data, e.built_at = data, time.time()


def _background_refresh(key: str, e: _Entry, build: Callable[[], Any]) -> None:
    try:
        with e.lock:
            _build_into(e, build)
    except Exception:
        print(f"snapshot refresh failed for {key!r}; serving the previous payload")
        traceback.print_exc()
    finally:
        e.refreshing = False


def get(key: str, build: Callable[[], Any], ttl: float) -> Any:
    """The payload for `key`, building it with `build()` at most once per `ttl`
    seconds no matter how many callers. Exceptions from a first build propagate
    (and nothing is cached); later failures keep the last good payload."""
    e = _entry(key, ttl)
    if e.built_at:
        with _lock:
            start = time.time() - e.built_at >= e.ttl and not e.refreshing
            if start:
                e.refreshing = True
        if start:
            threading.Thread(target=_background_refresh, args=(key, e, build), daemon=True,
                             name=f"snapshot:{key[:40]}").start()
        return e.data
    with e.lock:
        if not e.built_at:
            _build_into(e, build)
        return e.data


def refresh(key: str, build: Callable[[], Any], ttl: float) -> None:
    """Rebuild `key` now if it is stale or missing (used by the refresher thread,
    which runs this synchronously so it paces its own upstream calls)."""
    e = _entry(key, ttl)
    if e.built_at and time.time() - e.built_at < e.ttl:
        return
    with e.lock:
        if e.built_at and time.time() - e.built_at < e.ttl:
            return
        _build_into(e, build)


def age(key: str) -> float | None:
    """Seconds since `key` was last built, or None if it never was."""
    with _lock:
        e = _entries.get(key)
    return None if e is None or not e.built_at else time.time() - e.built_at


def clear() -> None:
    with _lock:
        _entries.clear()
