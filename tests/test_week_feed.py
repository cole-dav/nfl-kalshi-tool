from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import engine_agent as ea
import fair_value as fv
import scenario_sim as ss
import week_feed as wf
import week_overview as wo
from tests.test_engine_agent import fake_client, fake_price_legs, text, tool_use

EVENT = "KXNFLGAME-26NOV12WASNYG"
SPREAD = "KXNFLSPREAD-26NOV12WASNYG-NYG3"

DETAIL = {
    "away": "WAS", "home": "NYG", "kickoff": "2026-11-13T01:15:00Z", "records": {"away": "2-5-1", "home": "5-2-1"},
    "moneyline": {"home": {"ticker": f"{EVENT}-NYG", "yes_bid": 0.55, "yes_ask": 0.56}},
    "spread": {"ticker": SPREAD, "team": "NYG", "line": 2.5, "yes_bid": 0.48, "yes_ask": 0.49},
    "total": None, "team_totals": {},
    "injuries": {"away": [
        {"full_name": "Laremy Tunsil", "position": "OL", "ovr": 94, "report_status": "IR", "practice_status": "Injured reserve"},
        {"full_name": "Healthy Guy", "position": "WR", "ovr": 90, "report_status": None,
         "practice_status": "Full Participation in Practice"},
    ], "home": []},
    "matchups": [], "tendencies": {"sides": []},
}

PUBLISH = {
    "headline": "Thomas walls off Oweh", "lean": "NYG -2.5", "confidence": "medium",
    "writeup": "**NYG** win the trenches.", "key_injuries": [{"team": "WAS", "player": "Laremy Tunsil", "impact": "LT out"}],
    "key_matchups": [{"title": "NYG LT vs WAS edge", "note": "+15 OVR"}],
    "bets": [{"title": "NYG -2.5", "kind": "single", "rationale": "trenches", "legs": [{"market_ticker": SPREAD, "side": "yes"}]}],
}


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(wf, "FEED_PATH", str(tmp_path / "feed" / "posts.json"))
    monkeypatch.setattr(wf, "AB_PATH", str(tmp_path / "feed" / "ab.json"))
    monkeypatch.setattr(fv, "current_games", lambda: [])
    monkeypatch.setattr(ss, "price_legs", fake_price_legs)
    monkeypatch.setattr(wo, "build_game_detail", lambda ev: dict(DETAIL))
    monkeypatch.setattr(wf, "_edges", lambda ev: [])
    monkeypatch.setattr(wf, "_week_of", lambda a, h: 10)


def test_write_preview_runs_tools_then_publishes_priced_post():
    client, msgs = fake_client([
        SimpleNamespace(stop_reason="tool_use", content=[
            text("Checking the spread."), tool_use("tu_1", "price_market", {"market_ticker": SPREAD, "side": "yes"})]),
        SimpleNamespace(stop_reason="tool_use", content=[tool_use("tu_2", "publish_preview", PUBLISH)]),
    ])
    post = wf.write_preview(EVENT, client=client)

    assert post["headline"] == "Thomas walls off Oweh" and post["week"] == 10
    # bets are re-priced by the engine, not taken from the model
    assert post["bets"][0]["legs"][0]["fair"] == 0.5 and post["bets"][0]["legs"][0]["cost"] == 0.45
    assert post["trace"] and post["trace"][0].startswith("price")
    # the context handed over drops healthy players and keeps the IR starter
    ctx = msgs.calls[0]["messages"][0]["content"][0]["text"]
    assert "Laremy Tunsil" in ctx and "Healthy Guy" not in ctx
    assert msgs.calls[0]["model"] == wf.MODEL and msgs.calls[0]["output_config"] == {"effort": "low"} and "propose_bets" not in [t["name"] for t in msgs.calls[0]["tools"]]
    # tool results went back as a user turn before the publish call
    assert msgs.calls[1]["messages"][-1]["content"][0]["tool_use_id"] == "tu_1"

    saved = json.load(open(wf.FEED_PATH))
    assert saved[EVENT]["lean"] == "NYG -2.5"
    overview = {"week": 10, "weeks": [10, 11], "games": [
        {"event_ticker": EVENT, "away": "WAS", "home": "NYG"},
        {"event_ticker": "KXNFLGAME-26NOV16KCDEN", "away": "KC", "home": "DEN"}]}
    out = wf.feed(overview)
    assert [p["event_ticker"] for p in out["posts"]] == [EVENT]
    assert [g["has_post"] for g in out["games"]] == [True, False]
    assert wf.delete_post(EVENT) and not wf.feed(overview)["posts"]


def test_write_preview_requires_publish():
    client, _ = fake_client([SimpleNamespace(stop_reason="end_turn", content=[text("Here's my preview...")])])
    with pytest.raises(ea.EngineError):
        wf.write_preview(EVENT, client=client)
    assert not wf._load()


def test_write_preview_refusal():
    client, _ = fake_client([SimpleNamespace(stop_reason="refusal", content=[])])
    with pytest.raises(ea.EngineError):
        wf.write_preview(EVENT, client=client)


@pytest.mark.parametrize("status,practice,kind", [
    ("IR", "Injured reserve", "reserve"),
    ("IR", "Injured reserve (designated to return)", "reserve"),
    ("Out", "Did Not Participate In Practice", "report"),
    ("Questionable", "Full Participation in Practice", "report"),
    (None, "Limited Participation in Practice", "report"),
    (None, "Did Not Participate In Practice", "report"),
    (None, "Full Participation in Practice", None),
])
def test_injury_kind(status, practice, kind):
    assert wf._injury_kind({"report_status": status, "practice_status": practice}) == kind


def test_week_injuries_sorted_by_ovr_with_opponent(monkeypatch):
    inj = {
        "WAS": [{"full_name": "A", "ovr": 80, "report_status": "Out", "practice_status": None}],
        "NYG": [{"full_name": "B", "ovr": 91, "report_status": "IR", "practice_status": "Injured reserve"},
                {"full_name": "C", "ovr": 95, "report_status": None, "practice_status": "Full Participation in Practice"}],
    }
    import madden_ratings as mr
    monkeypatch.setattr(mr, "injuries_by_ovr", lambda team: [dict(r) for r in inj.get(team, [])])
    out = wf.week_injuries([{"event_ticker": EVENT, "away": "WAS", "home": "NYG"}])
    assert [(r["full_name"], r["team"], r["opp"], r["kind"]) for r in out["injuries"]] == [
        ("B", "NYG", "WAS", "reserve"), ("A", "WAS", "NYG", "report")]


class PerModelMessages:
    """Scripted replies per model, so parallel A/B runs don't share a queue."""
    def __init__(self, script):
        self.script = {m: list(r) for m, r in script.items()}
        self.calls = []

    def create(self, **kwargs):
        self.calls.append({**kwargs, "messages": [dict(m) for m in kwargs["messages"]]})
        return self.script[kwargs["model"]].pop(0)


def test_write_ab_runs_models_independently_and_promotes():
    usage = SimpleNamespace(input_tokens=1000, output_tokens=500, cache_creation_input_tokens=0, cache_read_input_tokens=0)
    sonnet_pub = {**PUBLISH, "headline": "Sonnet take"}
    msgs = PerModelMessages({
        "claude-sonnet-5-5": [SimpleNamespace(stop_reason="tool_use", model="claude-sonnet-5-5", usage=usage,
                                              content=[tool_use("s1", "publish_preview", sonnet_pub)])],
        "claude-opus-5-5": [SimpleNamespace(stop_reason="end_turn", model="claude-opus-5-5", usage=usage,
                                            content=[text("no publish")])],
    })
    client = SimpleNamespace(beta=SimpleNamespace(messages=msgs))
    out = wf.write_ab(EVENT, ["claude-sonnet-5-5", "claude-opus-5-5"], client=client)

    s = out["claude-sonnet-5-5"]
    assert s["headline"] == "Sonnet take" and s["model"] == "claude-sonnet-5-5" and s["effort"] == "low"
    assert s["stats"]["cost_usd"] == round((1000 * 2 + 500 * 10) / 1e6, 4)
    assert "error" in out["claude-opus-5-5"]
    # each model got only the shared game context, never the other's output
    for c in msgs.calls:
        assert len(c["messages"]) == 1 and "Sonnet take" not in json.dumps(c["messages"], default=str)
    # A/B results stay out of the live feed until promoted
    assert not wf._load()
    feed = wf.feed({"week": 10, "weeks": [10], "games": [{"event_ticker": EVENT, "away": "WAS", "home": "NYG"}]})
    assert set(feed["ab"][EVENT]["variants"]) == {"claude-sonnet-5-5", "claude-opus-5-5"} and not feed["posts"]
    wf.promote(EVENT, "claude-sonnet-5-5")
    assert wf._load()[EVENT]["headline"] == "Sonnet take"
    with pytest.raises(ea.EngineError):
        wf.promote(EVENT, "claude-opus-5-5")
