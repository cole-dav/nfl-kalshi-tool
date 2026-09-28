from __future__ import annotations

import time

import numpy as np
import pytest

import fair_value as fv
import prop_model as pm
import scenario_sim as ss
from conftest import AWAY, HOME, SUFFIX, raw_market, synthetic_game

QB, WR, RB = "qb-uuid", "wr-uuid", "rb-uuid"


def _player_markets():
    out = []
    for x, (b, a) in {199.5: (0.60, 0.62), 224.5: (0.45, 0.47), 249.5: (0.30, 0.32)}.items():
        out.append(raw_market("KXNFLPASSYDS", f"SEAGSMITH7-{int(x + 0.5)}", x, b, a, player=QB))
    for x, (b, a) in {49.5: (0.62, 0.64), 69.5: (0.43, 0.45), 89.5: (0.26, 0.28)}.items():
        out.append(raw_market("KXNFLRECYDS", f"SEAJSMITHNJIGBA11-{int(x + 0.5)}", x, b, a, player=WR))
    for x, (b, a) in {49.5: (0.55, 0.57), 79.5: (0.25, 0.27)}.items():
        out.append(raw_market("KXNFLRSHYDS", f"SEAKWALKER9-{int(x + 0.5)}", x, b, a, player=RB))
    names = {QB: "Geno Smith", WR: "Jaxon Smith-Njigba", RB: "Kenneth Walker"}
    return [fv.normalize_market(m, player_name=names[m["custom_strike"]["football_player"]], teams=(AWAY, HOME))
            for m in out]


@pytest.fixture
def synth(monkeypatch):
    markets = synthetic_game(mu_home=-4.0) + _player_markets()
    model = fv.fit_game(markets, AWAY, HOME)
    ev = fv.EventData(suffix=SUFFIX, event_ticker=f"KXNFLGAME-{SUFFIX}", away=AWAY, home=HOME,
                      matchup=f"{AWAY} @ {HOME}", markets=markets, model=model, nflverse={}, as_of=time.time())
    props = {
        (QB, "PASSYDS"): pm.PlayerProp(QB, "Geno Smith", AWAY, "PASSYDS", pm.base_dist("PASSYDS", 225.0), 225.0, 225.0, 1.0, []),
        (WR, "RECYDS"): pm.PlayerProp(WR, "Jaxon Smith-Njigba", AWAY, "RECYDS", pm.base_dist("RECYDS", 72.0, cv=0.6, p0=0.04), 72.0, 72.0, 1.0, []),
        (RB, "RSHYDS"): pm.PlayerProp(RB, "Kenneth Walker", AWAY, "RSHYDS", pm.base_dist("RSHYDS", 60.0), 60.0, 60.0, 1.0, []),
    }
    monkeypatch.setattr(fv, "load_event", lambda event, **kw: ev)
    monkeypatch.setattr(pm, "event_props", lambda e: props)
    monkeypatch.setattr(ss, "_script_coef", lambda e, team: -0.3)
    ss._sim_cache.clear()
    return ev


def _t(ev, title):
    return next(m for m in ev.markets if m["title"] == title)


def test_condition_margin_gte_7_locks_minus_6_5(synth):
    out = ss.condition(synth.event_ticker, [{"team": "SEA", "margin_gte": 7}])
    rows = {r["title"]: r for r in out["markets"]}
    assert rows["SEA -6.5"]["fair_cond"] == pytest.approx(1.0)
    assert rows["SEA ML"]["fair_cond"] == pytest.approx(1.0)
    assert rows["SEA -9.5"]["fair_cond"] < 1.0
    assert rows["ARI ML"]["fair_cond"] == pytest.approx(0.0)
    assert 0.2 < out["p_scenario"] < 0.5
    # base fair is the analytic model, matching /edges
    assert rows["SEA -6.5"]["fair_base"] == pytest.approx(synth.model.fair(_t(synth, "SEA -6.5")), abs=1e-4)


def test_joint_parlay_differs_for_correlated_legs(synth):
    qb = next(m for m in synth.markets if m["family"] == "PASSYDS" and m["threshold"] == 249.5)
    wr = next(m for m in synth.markets if m["family"] == "RECYDS" and m["threshold"] == 89.5)
    out = ss.price_legs([{"market_ticker": qb["ticker"], "side": "yes"},
                         {"market_ticker": wr["ticker"], "side": "yes"}])
    assert out["same_event"] is True
    assert out["joint_fair"] > out["indep_fair"] * 1.15   # QB and his WR go over together
    # a QB over and his RB's under is positively tied too (passing script) -- but
    # QB over + QB under on different lines should be ~ impossible:
    qb_lo = next(m for m in synth.markets if m["family"] == "PASSYDS" and m["threshold"] == 199.5)
    out2 = ss.price_legs([{"market_ticker": qb["ticker"], "side": "yes"},
                          {"market_ticker": qb_lo["ticker"], "side": "no"}])
    assert out2["joint_fair"] < 0.01
    assert out["parlay_cost"] == pytest.approx(out["legs"][0]["cost"] * out["legs"][1]["cost"], rel=1e-3)


def test_game_leg_correlation_with_margin(synth):
    """SEA -6.5 and SEA team total over 24.5 are positively correlated."""
    a, b = _t(synth, "SEA -6.5"), _t(synth, "SEA over 24.5 pts")
    out = ss.price_legs([{"market_ticker": a["ticker"], "side": "yes"}, {"market_ticker": b["ticker"], "side": "yes"}])
    assert out["joint_fair"] > out["indep_fair"]


def test_price_with_scenario(synth):
    a = _t(synth, "SEA -6.5")
    out = ss.price_legs([{"market_ticker": a["ticker"], "side": "yes"}],
                        scenario={"event": synth.event_ticker, "constraints": [{"team": "SEA", "margin_gte": 7}]})
    assert out["scenario_applied"] is True
    assert out["legs"][0]["fair"] == pytest.approx(1.0)


def test_player_scenario(synth):
    out = ss.condition(synth.event_ticker, [{"player": "Geno Smith", "series": "PASSYDS", "gte": 300}])
    rows = {r["title"]: r for r in out["markets"]}
    wr = next(r for r in out["markets"] if r["family"] == "RECYDS" and r["threshold"] == 89.5)
    assert wr["fair_cond"] > wr["fair_base"] + 0.05
    assert rows["Over 50.5"]["fair_cond"] > rows["Over 50.5"]["fair_base"]


def test_ladder_outcome_table(synth):
    out = ss.ladder(synth.event_ticker, "SEA", "SPREAD", [0, 3.5, -6.5], stakes=[1, 1, 2])
    assert [l["title"] for l in out["legs"]] == ["SEA ML", "SEA -3.5", "SEA -6.5"]
    probs = [b["prob"] for b in out["outcome_table"]]
    assert abs(sum(probs) - 1) < 1e-3
    assert [b["legs_hit"] for b in out["outcome_table"]] == [0, 1, 2, 3]
    assert out["outcome_table"][0]["label"] == "SEA loses or ties"
    assert out["outcome_table"][-1]["label"] == "SEA by 7+"
    assert out["max_loss"] == pytest.approx(-4.0)
    assert out["p_all"] == out["outcome_table"][-1]["prob"]
    fair_ml, fair_35, fair_65 = (l["fair"] for l in out["legs"])
    assert fair_ml >= fair_35 >= fair_65


def test_bad_scenario_raises(synth):
    with pytest.raises(ss.ScenarioError):
        ss.condition(synth.event_ticker, [{"team": "NYJ", "margin_gte": 3}])
    with pytest.raises(ss.ScenarioError):
        ss.condition(synth.event_ticker, [{"team": "SEA", "margin_gte": 70}])
