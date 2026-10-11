from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import server
import snapshots as snap


@pytest.fixture
def base_url():
    snap.clear()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    snap.clear()


def test_healthz_is_cheap_and_uncached(base_url, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("/healthz must not build anything")

    monkeypatch.setattr(snap, "get", boom)
    monkeypatch.setattr(snap, "refresh", boom)
    with urllib.request.urlopen(base_url + "/healthz", timeout=5) as r:
        assert r.status == 200
        assert r.headers["Cache-Control"] == "no-store"
        body = json.loads(r.read())
    assert body["ok"] is True
    assert body["snapshots"] == 0
    assert body["slate_age_s"] is None
    assert body["uptime_s"] >= 0


def test_count_ignores_unbuilt_entries():
    snap.clear()
    snap.get("a", lambda: {"x": 1}, 60)
    with pytest.raises(RuntimeError):
        snap.get("b", lambda: (_ for _ in ()).throw(RuntimeError("no")), 60)
    assert snap.count() == 1
    snap.clear()
