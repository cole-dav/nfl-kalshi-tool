from __future__ import annotations

from datetime import datetime, timezone

import pytest

import bet_ledger as bl
import injury_watch as iw
import week_feed as wf

EV = "KXNFLGAME-26NOV29DETGB"
KO = "2026-11-29T18:00:00+00:00"
POST = {"post_id": "old1", "event_ticker": EV, "away": "DET", "home": "GB", "kickoff": KO, "week": 13,
        "key_injuries": [{"team": "GB", "player": "Backup Guy", "impact": "x"}],
        "bets": [{"title": "GB ML", "legs": [{"market_ticker": "KXNFLGAME-26NOV29DETGB-GB", "side": "yes",
                                              "cost": 0.5, "fair": 0.55}]}]}


def P(team, ovr, status, pos="WR"):
    return {"team": team, "pos": pos, "ovr": ovr, "status": status}


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.setattr(wf, "FEED_PATH", str(tmp_path / "posts.json"))
    monkeypatch.setattr(wf, "AB_PATH", str(tmp_path / "ab.json"))
    monkeypatch.setattr(bl, "LEDGER_PATH", str(tmp_path / "ledger.json"))
    monkeypatch.delenv("FEED_AUTO_AB", raising=False)
    monkeypatch.delenv("FEED_INJURY_HOUR_ET", raising=False)
    wf.save_post(dict(POST))
    bl.record(dict(POST), "live")
    return tmp_path


def _reports(monkeypatch, by_team):
    monkeypatch.setattr(iw, "snapshot", lambda team: dict(by_team.get(team, {})))


def test_diff_marks_only_material_crossings():
    before = {"Star": P("GB", 90, "Questionable"), "Mid": P("GB", 78, "Questionable"),
              "Scrub": P("GB", 60, "Questionable"), "Backup Guy": P("GB", 65, "Questionable"),
              "Hurt": P("DET", 88, "Out")}
    after = {"Star": P("GB", 90, "Out"), "Mid": P("GB", 78, "Out"), "Scrub": P("GB", 60, "Out"),
             "Backup Guy": P("GB", 65, "Doubtful")}
    ch = {c["player"]: c for c in iw.diff(before, after, {"backup guy"})}
    assert ch["Star"]["material"] and ch["Backup Guy"]["material"]           # 82+ / named by the preview
    assert not ch["Mid"]["material"]                                          # notable, logged only
    assert "Scrub" not in ch                                                  # below notable
    assert ch["Hurt"]["material"] and ch["Hurt"]["to"] == "off the report"    # cleared to play
    qb = iw.diff({"Qb One": P("GB", None, "Questionable", pos="QB")}, {"Qb One": P("GB", None, "Out", pos="QB")}, set())
    assert qb and qb[0]["material"]                                           # unrated QB still counts
    practice = iw.diff({"Star": P("GB", 90, "practice: Did Not Participate")},
                       {"Star": P("GB", 90, "practice: Limited")}, set())
    assert practice and not practice[0]["material"]


def test_heartbeat_rewrites_on_material_change_and_supersedes_bets(stores, monkeypatch):
    _reports(monkeypatch, {"GB": {"Star": P("GB", 90, "Questionable")}})
    iw.note_written(EV, "DET", "GB")
    _reports(monkeypatch, {"GB": {"Star": P("GB", 90, "Out")}})
    new = {**POST, "post_id": "new1", "bets": []}
    monkeypatch.setattr(wf, "write_preview", lambda ev: (wf.save_post(new), new)[1])
    fri_night = datetime(2026, 11, 28, 3, 30, tzinfo=timezone.utc)          # Fri 22:30 ET
    lines = iw.heartbeat(fri_night)
    assert len(lines) == 1 and "rewritten" in lines[0] and "Star Questionable -> Out" in lines[0]
    u = iw.updates_for(EV)[-1]
    assert u["action"] == "rewritten" and u["post_id"] == "new1" and u["superseded"] == ["old1"]
    led = bl._load()
    assert led["old1:0"]["superseded_at"] and bl.week_bets({EV}) == []
    assert iw.heartbeat(fri_night) == []                                      # once per night


def test_heartbeat_waits_for_the_hour_and_logs_minor_changes(stores, monkeypatch):
    _reports(monkeypatch, {"GB": {"Mid": P("GB", 78, "Questionable")}})
    iw.note_written(EV, "DET", "GB")
    _reports(monkeypatch, {"GB": {"Mid": P("GB", 78, "Out")}})
    monkeypatch.setattr(wf, "write_preview", lambda ev: pytest.fail("no rewrite for a minor change"))
    assert iw.heartbeat(datetime(2026, 11, 27, 20, 0, tzinfo=timezone.utc)) == []   # 15:00 ET: too early
    lines = iw.heartbeat(datetime(2026, 11, 28, 3, 30, tzinfo=timezone.utc))
    assert "noted" in lines[0]
    assert not bl._load()["old1:0"].get("superseded_at")


def test_no_rewrite_near_kickoff_or_past_cap(stores, monkeypatch):
    calls = []
    monkeypatch.setattr(wf, "write_preview", lambda ev: calls.append(ev) or {**POST, "post_id": f"n{len(calls)}"})
    w = {EV: {"baseline": {"Star": P("GB", 90, "Questionable")}, "updates": [], "rewrites": 0}}
    _reports(monkeypatch, {"GB": {"Star": P("GB", 90, "Out")}})
    near = datetime(2026, 11, 29, 17, 30, tzinfo=timezone.utc)               # 30 min before kickoff
    assert iw.check_game(EV, POST, w, near, lambda ev: ([], wf.write_preview(ev)))["action"] == "noted"
    w[EV].update(baseline={"Star": P("GB", 90, "Questionable")}, rewrites=iw.MAX_REWRITES)
    early = datetime(2026, 11, 28, 3, 30, tzinfo=timezone.utc)
    assert iw.check_game(EV, POST, w, early, lambda ev: ([], wf.write_preview(ev)))["action"] == "noted"
    assert calls == []


def test_post_written_before_watch_starts_from_current(stores, monkeypatch):
    _reports(monkeypatch, {"GB": {"Star": P("GB", 90, "Out")}})
    w = {}
    assert iw.check_game(EV, POST, w, datetime(2026, 11, 28, 3, 30, tzinfo=timezone.utc), None) is None
    assert w[EV]["baseline"] == {"Star": P("GB", 90, "Out")}
