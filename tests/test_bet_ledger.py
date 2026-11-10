from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import bet_ledger as bl

EV = "KXNFLGAME-26NOV12WASNYG"
SPREAD, ML, REC, TD = (f"KXNFLSPREAD-26NOV12WASNYG-NYG3", f"{EV}-NYG",
                       "KXNFLREC-26NOV12WASNYG-NYGTJOHNSON84-1", "KXNFLTD-26NOV12WASNYG-WASSDIGGS3-1")
END = datetime(2026, 11, 13, 4, 30, tzinfo=timezone.utc)


def post(pid="p1", model="claude-sonnet-5-5", bets=None):
    return {"post_id": pid, "event_ticker": EV, "away": "WAS", "home": "NYG", "week": 10,
            "kickoff": "2026-11-13T01:15:00Z", "created_at": "2026-11-10T17:00:00+00:00", "model": model,
            "effort": "low", "bets": bets if bets is not None else [
                {"title": "NYG -2.5", "kind": "single", "legs": [{"market_ticker": SPREAD, "side": "yes", "cost": 0.5, "fair": 0.55}]},
                {"title": "NYG ML + TJ 1+ rec", "kind": "parlay", "parlay_cost": 0.4, "joint_fair": 0.45, "legs": [
                    {"market_ticker": ML, "side": "yes", "cost": 0.5}, {"market_ticker": REC, "side": "yes", "cost": 0.8}]},
                {"title": "NYG ladder", "kind": "ladder", "legs": [
                    {"market_ticker": ML, "side": "yes", "cost": 0.5}, {"market_ticker": SPREAD, "side": "yes", "cost": 0.5}]},
                {"title": "Diggs TD (voided)", "kind": "single", "legs": [{"market_ticker": TD, "side": "yes", "cost": 0.3}]},
            ]}


class FakeIdx:
    def __init__(self, markets, ml_closed=True):
        self.markets, self.ml_closed, self.calls = markets, ml_closed, 0

    def get_event_markets(self, ev):
        return [{"ticker": ML, "result": "yes" if self.ml_closed else "", "close_time": END.isoformat()}]

    def get(self, path):
        self.calls += 1
        return {"market": self.markets.get(path.rsplit("/", 1)[1], {})}


MARKETS = {SPREAD: {"result": "no", "status": "finalized"}, ML: {"result": "yes", "status": "finalized"},
           REC: {"result": "yes", "status": "finalized"}, TD: {"result": "", "status": "finalized"}}


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(bl, "LEDGER_PATH", str(tmp_path / "ledger.json"))


def test_record_is_idempotent_and_tracks_promotion():
    assert bl.record(post(), "ab") == 4
    assert bl.record(post(), "live") == 0          # same post promoted: no duplicates
    led = bl._load()
    assert len(led) == 4 and led["p1:0"]["sources"] == ["ab", "live"]
    assert led["p1:1"]["price"] == 0.4 and led["p1:0"]["price"] == 0.5
    assert bl.record({**post(), "post_id": None}, "live") == 0


def test_grades_only_after_12h_then_scores_each_kind():
    bl.record(post(), "ab")
    idx = FakeIdx(MARKETS)
    assert bl.grade_due(END + timedelta(hours=11, minutes=59), idx) == 0
    assert idx.calls == 0                           # nothing fetched before it's due
    assert bl._load()["p1:0"]["due_at"] == (END + timedelta(hours=12)).isoformat()

    assert bl.grade_due(END + timedelta(hours=12, minutes=5), idx) == 4
    led = bl._load()
    assert (led["p1:0"]["outcome"], led["p1:0"]["pnl"]) == ("loss", -1.0)
    assert (led["p1:1"]["outcome"], led["p1:1"]["pnl"]) == ("win", round(1 / 0.4 - 1, 4))   # parlay at product of costs
    assert led["p1:2"]["record"] == "1-1" and led["p1:2"]["outcome"] == "push"            # ladder: +1 and -1
    assert led["p1:3"]["outcome"] == "push" and led["p1:3"]["legs"][0]["result"] == "void"


def test_game_still_going_or_leg_unsettled_waits():
    bl.record(post(), "ab")
    assert bl.grade_due(END + timedelta(hours=20), FakeIdx(MARKETS, ml_closed=False)) == 0
    unsettled = {**MARKETS, REC: {"result": "", "status": "active"}}
    assert bl.grade_due(END + timedelta(hours=13), FakeIdx(unsettled)) == 3          # the parlay waits
    assert not bl._load()["p1:1"]["graded_at"]
    assert bl.grade_due(END + timedelta(hours=80), FakeIdx(unsettled)) == 1          # gives up: leg voids
    assert bl._load()["p1:1"]["outcome"] == "win"                                   # ML alone at 0.5


def test_results_window_report_and_scorecard():
    bl.record(post(), "ab")
    bl.record(post("p2", "claude-opus-5-5", bets=[post()["bets"][1]]), "live")
    graded_at = END + timedelta(hours=12, minutes=10)
    bl.grade_due(graded_at, FakeIdx(MARKETS))
    out = bl.results(graded_at - timedelta(minutes=1), graded_at + timedelta(minutes=1))
    assert out["count"] == 5
    assert "[LIVE · opus-5-5] NYG ML + TJ 1+ rec" in out["report"] and "WIN +$1.50" in out["report"]
    card = {r["label"]: r for r in out["scorecard"]}
    assert (card["sonnet-5-5"]["w"], card["sonnet-5-5"]["l"], card["sonnet-5-5"]["p"]) == (1, 1, 2)
    assert card["sonnet-5-5"]["pnl"] == 0.5 and card["sonnet-5-5"]["roi"] == 0.25
    assert card["LIVE FEED"]["w"] == 1 and out["scorecard"][0]["label"] == "LIVE FEED"
    assert bl.results(graded_at + timedelta(hours=1), graded_at + timedelta(hours=2))["count"] == 0


def test_scorecard_counts_pending():
    bl.record(post(), "ab")
    (row,) = bl.scorecard(list(bl._load().values()))
    assert row["pending"] == 4 and row["w"] == 0 and row["roi"] is None


def test_next_check_sleeps_until_something_can_happen():
    assert bl.next_check(END) is None
    bl.record(post(), "ab")
    kick = datetime(2026, 11, 13, 1, 15, tzinfo=timezone.utc)
    assert bl.next_check(kick) == kick + timedelta(hours=3)               # earliest possible end
    later = kick + timedelta(hours=3, minutes=5)
    assert bl.next_check(later) == later + bl.END_POLL                    # polling for the final whistle
    idx = FakeIdx(MARKETS)
    assert bl.grade_due(later, idx) == 0 and idx.calls == 0               # end found; nothing due yet
    assert bl.next_check(later) == END + timedelta(hours=12)              # sleep until due
    unsettled = {**MARKETS, REC: {"result": "", "status": "active"}}
    due = END + timedelta(hours=12, minutes=1)
    bl.grade_due(due, FakeIdx(unsettled))
    assert bl.next_check(due) == due + bl.RETRY                           # parlay leg still open


def test_no_kalshi_calls_before_a_game_can_be_over():
    bl.record(post(), "ab")
    idx = FakeIdx(MARKETS)
    kick = datetime(2026, 11, 13, 1, 15, tzinfo=timezone.utc)
    bl.grade_due(kick + timedelta(hours=1), idx)
    assert bl._load()["p1:0"]["game_end"] is None
