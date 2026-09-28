from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import engine_agent as ea
import fair_value as fv
import scenario_sim as ss

LOCK = "KXNFLSPREAD-26OCT04SEAARI-SEA4"
EXCL = "KXNFLTOTAL-26OCT04SEAARI-45"
OTHER = "KXNFLRECYDS-26OCT04SEAARI-SEAJSMITHNJIGBA11-70"


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        # snapshot what was sent (the loop mutates the list afterwards)
        self.calls.append({**kwargs, "messages": [dict(m) for m in kwargs["messages"]]})
        return self.responses.pop(0)


def fake_client(responses):
    msgs = FakeMessages(responses)
    return SimpleNamespace(beta=SimpleNamespace(messages=msgs)), msgs


def text(t):
    return SimpleNamespace(type="text", text=t)


def tool_use(id_, name, inp):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=inp)


def fake_price_legs(legs, scenario=None):
    views = [{"market_ticker": l["market_ticker"], "side": l["side"], "title": l["market_ticker"][-6:],
              "event_ticker": "-".join(l["market_ticker"].split("-")[:2]), "fair": 0.5, "cost": 0.45,
              "edge": 0.05, "american_cost": 122, "american_fair": 100, "yes_bid": 0.44, "yes_ask": 0.45}
             for l in legs]
    return {"legs": views, "joint_fair": 0.5 ** len(legs), "indep_fair": 0.5 ** len(legs),
            "parlay_cost": 0.45 ** len(legs), "joint_american": 100, "parlay_american_cost": 300,
            "ev_per_dollar": 0.1, "scenario_applied": bool(scenario), "same_event": True}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(fv, "current_games", lambda: [])
    monkeypatch.setattr(ss, "price_legs", fake_price_legs)
    ea._conversations.clear()


SLIP = {
    "legs": [{"market_ticker": LOCK, "side": "yes", "title": "SEA -3.5"}],
    "bookmarks": [],
    "locks": [{"market_ticker": LOCK, "side": "yes", "title": "SEA -3.5"}],   # object form
    "excludes": [f"{EXCL}|no"],                                                 # string form
    "scenarios": [{"event": "KXNFLGAME-26OCT04SEAARI", "constraints": [{"team": "SEA", "margin_gte": 7}],
                   "label": "SEA by 7+"}],
    "odds_mode": "american",
}


def test_chat_tool_loop_enforces_locks_and_excludes():
    proposal = {"proposals": [{
        "title": "SEA blowout SGP", "kind": "parlay", "rationale": "script",
        "legs": [{"market_ticker": EXCL, "side": "no"}, {"market_ticker": OTHER, "side": "yes"}],
    }]}
    client, msgs = fake_client([
        SimpleNamespace(stop_reason="tool_use", content=[text("Checking."), tool_use("tu_1", "propose_bets", proposal)]),
        SimpleNamespace(stop_reason="end_turn", content=[text("Here is the **SEA** parlay.")]),
    ])
    out = ea.chat("find me something on SEA", SLIP, client=client)

    assert out["conversation_id"]
    assert "SEA" in out["reply"]
    assert len(out["proposals"]) == 1
    p = out["proposals"][0]
    keys = {f"{l['market_ticker']}|{l['side']}" for l in p["legs"]}
    assert f"{EXCL}|no" not in keys            # excluded leg dropped
    assert f"{LOCK}|yes" in keys               # locked leg added
    assert f"{OTHER}|yes" in keys
    assert p["scenario"] and p["scenario"][0]["event"] == "KXNFLGAME-26OCT04SEAARI"   # user scenario applied
    assert p["legs"][0]["cost"] == 0.45 and p["legs"][0]["american_fair"] == 100
    assert out["trace"][0]["tool"] == "propose_bets"

    # the model was told what the server changed
    second = msgs.calls[1]["messages"]
    tool_result = second[-1]["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "tu_1"
    body = json.loads(tool_result["content"])
    assert any("dropped excluded" in n for n in body["enforcement"])
    assert any("added locked" in n for n in body["enforcement"])

    # request shape: frozen cached system prompt, strict tools, slip in the user turn
    call = msgs.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["thinking"] == {"type": "adaptive"} and call["output_config"] == {"effort": "medium"}
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert all(t["strict"] and t["input_schema"]["additionalProperties"] is False for t in call["tools"])
    first_user = call["messages"][0]["content"][0]["text"]
    assert "<slip_state>" in first_user and f"{LOCK}|yes" in first_user and f"{EXCL}|no" in first_user


def test_conversation_persists_full_content():
    client, msgs = fake_client([SimpleNamespace(stop_reason="end_turn", content=[text("hi")]),
                                SimpleNamespace(stop_reason="end_turn", content=[text("again")])])
    a = ea.chat("hello", {}, client=client)
    b = ea.chat("more", {}, conversation_id=a["conversation_id"], client=client)
    assert b["conversation_id"] == a["conversation_id"]
    sent = msgs.calls[1]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]


def test_refusal_and_loop_cap():
    client, _ = fake_client([SimpleNamespace(stop_reason="refusal", content=[])])
    out = ea.chat("x", {}, client=client)
    assert "declined" in out["reply"]
    loop = [SimpleNamespace(stop_reason="tool_use", content=[tool_use(f"t{i}", "propose_bets", {"proposals": []})])
            for i in range(20)]
    client, msgs = fake_client(loop)
    out = ea.chat("y", {}, client=client)
    assert len(msgs.calls) == ea.MAX_TOOL_ITERS
    assert "maximum" in out["reply"]


def test_enforce_slip_replaces_opposite_side_of_locked_market():
    slip = ea.normalize_slip({"locks": [f"{LOCK}|yes"], "excludes": []})
    legs, notes = ea.enforce_slip([{"market_ticker": LOCK, "side": "no"}], slip)
    assert legs == [{"market_ticker": LOCK, "side": "yes"}]


def test_chat_unavailable_without_key(monkeypatch):
    monkeypatch.setattr(ea, "chat_enabled", lambda: False)
    with pytest.raises(ea.ChatUnavailable):
        ea.chat("hi", {})
