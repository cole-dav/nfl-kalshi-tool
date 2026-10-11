"""Param validation and caching for /api/history and /api/movers (no network)."""

from __future__ import annotations

from urllib.parse import parse_qs

import pytest

server = pytest.importorskip("server")


class FakeHandler:
    _odds_history = server.Handler._odds_history

    def __init__(self):
        self.sent = []

    def _send_json(self, payload, status=200, **kw):
        self.sent.append((status, payload))

    def _send_snapshot(self, key, build, ttl, cache=server.CACHE_LIVE):
        self.sent.append((200, key))


def call(path, query):
    h = FakeHandler()
    h._odds_history(path, parse_qs(query))
    return h.sent[-1]


@pytest.mark.parametrize("query", [
    "", "ticker=", "ticker=a b", "ticker=" + ",".join(f"T{i}" for i in range(21)),
    "ticker=X&hours=0", "ticker=X&hours=1000", "ticker=X&hours=nan", "ticker=X&hours=abc",
    "ticker=<script>",
])
def test_history_rejects_bad_params(query):
    assert call("/api/history", query)[0] == 400


def test_history_key_is_normalized():
    # case, order and duplicates don't make new cache keys
    assert call("/api/history", "ticker=kxb-1,KXA-2,kxb-1&hours=48") == (200, "history:48.0:KXA-2,KXB-1")
    assert call("/api/history", "ticker=KXA-2") == (200, "history:48.0:KXA-2")


@pytest.mark.parametrize("query", ["kind=foo", "limit=0", "limit=201", "limit=x", "hours=0.5", "hours=999"])
def test_movers_rejects_bad_params(query):
    assert call("/api/movers", query)[0] == 400


def test_movers_defaults():
    assert call("/api/movers", "") == (200, "movers:24.0:all:50")
    assert call("/api/movers", "hours=6&kind=PROP&limit=10") == (200, "movers:6.0:prop:10")
