from __future__ import annotations

import numpy as np

import fair_value as fv
from conftest import AWAY, HOME, synthetic_game


def _by_title(ms):
    return {m["title"]: m for m in ms}


def test_monotone_fair_ladder():
    ms = synthetic_game(mu_home=-4.0)
    model = fv.fit_game(ms, AWAY, HOME)
    t = _by_title(ms)
    p_ml, p35, p65 = (model.fair(t[k]) for k in ("SEA ML", "SEA -3.5", "SEA -6.5"))
    assert p_ml >= p35 >= p65
    assert p_ml > 0.5 > p65
    # every same-team spread ladder is monotone
    for team in (AWAY, HOME):
        lad = sorted([m for m in ms if m["family"] == "SPREAD" and m["team_code"] == team], key=lambda m: m["threshold"])
        ps = [model.fair(m) for m in lad]
        assert all(a >= b for a, b in zip(ps, ps[1:]))
    # ML probabilities + tie mass sum to one
    assert abs(model.fair(t["SEA ML"]) + model.fair(t["ARI ML"]) - 1) < 0.01


def test_fit_reproduces_synthetic_ladder():
    for mu in (-7.0, -1.5, 3.0):
        ms = synthetic_game(mu_home=mu, total=47.0)
        model = fv.fit_game(ms, AWAY, HOME)
        assert abs(model.mu_m - mu) < 0.6, (mu, model.mu_m)
        assert abs(model.mu_t - 47.0) < 0.6
        errs = [abs(model.fair(m) - fv.mid(m)) for m in ms if m["family"] in ("ML", "SPREAD", "TOTAL")]
        assert max(errs) < 0.02
        assert model.fit["margin_rms"] < 0.01


def test_nflverse_blend_moves_mu():
    ms = synthetic_game(mu_home=-4.0)
    base = fv.fit_game(ms, AWAY, HOME)
    blended = fv.fit_game(ms, AWAY, HOME, spread_line=-8.0, line_weight=0.5)
    assert blended.mu_m < base.mu_m - 1.5


def test_team_totals_consistent_with_game():
    ms = synthetic_game(mu_home=-4.0, total=45.0)
    model = fv.fit_game(ms, AWAY, HOME)
    ip = model.implied_points()
    assert ip[AWAY] > ip[HOME]
    assert abs(ip[AWAY] + ip[HOME] - model.mu_t) < 1e-6
    assert model.p_team_points_gt(AWAY, 17.5) > model.p_team_points_gt(AWAY, 24.5)


def test_spread_above_ml_flagged_as_arb():
    # SEA -3.5 bid 0.66 while SEA ML ask is 0.60: YES ML + NO -3.5 pays >= $1 always.
    ms = synthetic_game(mu_home=-4.0, overrides={"SEA ML": (0.59, 0.60), "SEA -3.5": (0.66, 0.67)})
    arbs, soft = fv.ladder_arbs(ms, AWAY, HOME)
    hit = [a for a in arbs if {l["ticker"].rsplit("-", 1)[-1] for l in a["legs"]} == {"SEA", "SEA4"}]
    assert hit, arbs
    a = hit[0]
    assert a["profit_floor"] > 0.05
    sides = {l["ticker"].rsplit("-", 1)[-1]: l["side"] for l in a["legs"]}
    assert sides == {"SEA": "yes", "SEA4": "no"}


def test_no_arbs_on_clean_ladder():
    arbs, soft = fv.ladder_arbs(synthetic_game(), AWAY, HOME)
    assert arbs == [] and soft == []


def test_disjoint_arb():
    ms = synthetic_game(mu_home=0.0, overrides={"SEA -1.5": (0.55, 0.56), "ARI -1.5": (0.50, 0.51)})
    arbs, _ = fv.ladder_arbs(ms, AWAY, HOME)
    assert any(a["type"] == "disjoint" for a in arbs)


def test_edges_cost_rules():
    ms = synthetic_game(mu_home=-4.0)
    t = _by_title(ms)
    rows = fv.edge_rows(t["SEA -6.5"], 0.60, "SEA @ ARI", min_edge=-1)
    yes = next(r for r in rows if r["side"] == "yes")
    no = next(r for r in rows if r["side"] == "no")
    assert yes["cost"] == t["SEA -6.5"]["yes_ask"]
    assert abs(no["cost"] - (1 - t["SEA -6.5"]["yes_bid"])) < 1e-9
    assert yes["edge"] == round(0.60 - yes["cost"], 4)
    assert isinstance(yes["american_fair"], int)


def test_norm_helpers():
    xs = np.array([-2.0, -0.5, 0.0, 1.3])
    assert np.allclose(fv.norm_ppf(fv.norm_cdf(xs)), xs, atol=1e-5)
