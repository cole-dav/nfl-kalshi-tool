from __future__ import annotations

import threading
import time

import pytest

import kalshi_book as kb
import snapshots as snap


@pytest.fixture(autouse=True)
def _clean():
    snap.clear()
    yield
    snap.clear()


def test_concurrent_misses_build_once():
    calls = []
    gate = threading.Event()

    def build():
        calls.append(1)
        gate.wait(1)
        return {"n": len(calls)}

    out = []
    threads = [threading.Thread(target=lambda: out.append(snap.get("k", build, 60))) for _ in range(8)]
    for t in threads:
        t.start()
    time.sleep(0.05)
    gate.set()
    for t in threads:
        t.join()
    assert len(calls) == 1
    assert out == [{"n": 1}] * 8


def test_stale_served_while_one_background_rebuild_runs():
    snap.get("k", lambda: "old", 0.01)
    time.sleep(0.02)
    started = threading.Event()
    release = threading.Event()
    calls = []

    def slow_build():
        calls.append(1)
        started.set()
        release.wait(1)
        return "new"

    assert snap.get("k", slow_build, 0.01) == "old"
    started.wait(1)
    assert snap.get("k", slow_build, 0.01) == "old"  # still rebuilding: no second build
    release.set()
    for _ in range(100):
        if snap.get("k", slow_build, 60) == "new":
            break
        time.sleep(0.01)
    assert snap.get("k", slow_build, 60) == "new"
    assert len(calls) == 1


def test_failed_refresh_keeps_last_good_payload():
    snap.get("k", lambda: "good", 0.01)
    time.sleep(0.02)

    def boom():
        raise RuntimeError("upstream down")

    assert snap.get("k", boom, 0.01) == "good"
    time.sleep(0.05)
    assert snap.get("k", boom, 60) == "good"


def test_first_build_error_propagates_and_is_not_cached():
    with pytest.raises(ValueError):
        snap.get("k", lambda: (_ for _ in ()).throw(ValueError("nope")), 60)
    assert snap.get("k", lambda: "ok", 60) == "ok"


def test_refresh_skips_fresh_entries():
    calls = []
    snap.refresh("k", lambda: calls.append(1) or "a", 60)
    snap.refresh("k", lambda: calls.append(1) or "b", 60)
    assert calls == [1]
    assert snap.get("k", lambda: "c", 60) == "a"


def test_lru_cap(monkeypatch):
    monkeypatch.setattr(snap, "MAX_ENTRIES", 3)
    for i in range(5):
        snap.get(f"k{i}", lambda i=i: i, 60)
    assert snap.age("k0") is None and snap.age("k1") is None
    assert snap.age("k4") is not None


def test_account_scoped_kalshi_paths_are_never_cached(monkeypatch):
    monkeypatch.setattr(kb, "CACHE_TTL_SECONDS", 20)
    assert kb._cacheable("/trade-api/v2/markets")
    assert kb._cacheable("/trade-api/v2/events")
    assert not kb._cacheable("/trade-api/v2/portfolio/positions")
    assert not kb._cacheable("/trade-api/v2/portfolio/balance")
    assert not kb._cacheable("/trade-api/v2/communications/quotes")


def test_kalshi_cached_get_single_flight(monkeypatch, tmp_path):
    monkeypatch.setattr(kb, "CACHE_TTL_SECONDS", 20)
    monkeypatch.setattr(kb, "_API_CACHE_DIR", str(tmp_path))
    calls = []
    gate = threading.Event()

    def fetch():
        calls.append(1)
        gate.wait(1)
        return {"ok": True}

    out = []
    threads = [threading.Thread(target=lambda: out.append(kb._cached_get("/trade-api/v2/markets", {"a": 1}, fetch)))
               for _ in range(6)]
    for t in threads:
        t.start()
    time.sleep(0.05)
    gate.set()
    for t in threads:
        t.join()
    assert len(calls) == 1 and out == [{"ok": True}] * 6


def test_kalshi_portfolio_get_bypasses_disk_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(kb, "CACHE_TTL_SECONDS", 20)
    monkeypatch.setattr(kb, "_API_CACHE_DIR", str(tmp_path))
    n = iter(range(10))
    for _ in range(2):
        kb._cached_get("/trade-api/v2/portfolio/positions", None, lambda: {"user": next(n)})
    assert list(tmp_path.iterdir()) == []
