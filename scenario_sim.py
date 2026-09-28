"""
Joint Monte Carlo for one game: margin + total -> team points -> player stats.

  1. (M, T) drawn from the fair_value pmfs through a Gaussian copula with a
     mild correlation (favorite's margin vs total, ~0.03 historically); T is
     nudged by 1 when T+M is odd so both team scores are whole numbers.
  2. Team-level latent factors per side, built from the normal scores of that
     team's points (zP) and its own margin (zS):
        pass volume  F = a*zP + b*zS + noise   (b < 0: trailing teams throw more;
                                                 |b| scales with the team's
                                                 pass_trailing - pass_leading gap)
        rush volume  R = a*zP + c*zS + noise   (c > 0: leading teams run)
        scoring      D = 0.75*zP + noise        (TDs)
  3. Each player-stat is a Gaussian copula draw loaded on its team factor plus a
     player-level shared term (so the same player's REC and RECYDS move
     together), pushed through prop_model's marginal (StatDist.ppf). Marginals
     are preserved exactly; only the dependence comes from the game script.

Per-event sims are cached for fair_value.EVENT_TTL and rebuilt whenever the
event's quotes are reloaded.
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
from dataclasses import dataclass, field

import numpy as np

import fair_value as fv
import prop_model as pm

N_SIMS = 20_000

PASS_LOAD = {"PASSYDS": 0.85, "PASSCOMP": 0.85, "PASSATT": 0.85, "RECYDS": 0.55, "REC": 0.5, "LONGREC": 0.35}
RUSH_LOAD = {"RSHYDS": 0.6, "RSHATT": 0.65, "LONGRSH": 0.35}
TD_LOAD = {"PASSTDS": 0.7, "TD": 0.5}
PLAYER_SHARED = 0.8  # weight of the player-level shared term in the idiosyncratic part


class ScenarioError(ValueError):
    pass


def _normal_scores(x: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Rank-based normal scores with random tie-breaking."""
    n = len(x)
    order = np.lexsort((rng.random(n), x))
    ranks = np.empty(n)
    ranks[order] = np.arange(n)
    return fv.norm_ppf((ranks + 0.5) / n)


def _pmf_quantile(pmf: np.ndarray, support: np.ndarray, u: np.ndarray) -> np.ndarray:
    cdf = np.cumsum(pmf)
    cdf[-1] = 1.0
    return support[np.searchsorted(cdf, u, side="left").clip(0, len(support) - 1)]


def _script_coef(ev, team: str) -> float:
    """Pass-volume loading on own margin: more negative when the team's pass
    rate swings a lot between leading and trailing."""
    try:
        import nflverse_data as nd
        t = pm._tend(nd.kalshi_to_nflverse_team(team))
        pl, _ = pm._bv(t["off"], "pass_leading")
        ptr, _ = pm._bv(t["off"], "pass_trailing")
        if pl is not None and ptr is not None:
            gap = ptr - pl                       # league typical ~0.25-0.30
            return -0.3 * max(0.3, min(1.6, gap / 0.28))
    except Exception:
        pass
    return -0.3


@dataclass
class Sim:
    suffix: str
    away: str
    home: str
    n: int
    M: np.ndarray
    T: np.ndarray
    pts: dict
    stats: dict = field(default_factory=dict)      # (player_id, family) -> array
    meta: dict = field(default_factory=dict)       # (player_id, family) -> {name, team}
    as_of: float = 0.0

    def team_margin(self, team: str) -> np.ndarray:
        return self.M if team == self.home else -self.M

    def yes(self, m: dict) -> np.ndarray | None:
        """Boolean array: did market `m` resolve YES in each sim."""
        fam, t, team = m["family"], m.get("threshold"), m.get("team_code")
        if fam == "ML" and team in (self.home, self.away):
            return self.team_margin(team) > 0
        if t is None:
            return None
        if fam == "SPREAD" and team in (self.home, self.away):
            return self.team_margin(team) > t
        if fam == "TOTAL":
            return self.T > t
        if fam == "TEAMTOTAL" and team in self.pts:
            return self.pts[team] > t
        arr = self.stats.get((m.get("player_id"), fam))
        if arr is not None:
            return arr > t
        return None

    def find_player(self, player: str, family: str) -> tuple | None:
        fam = fv.family_of(family.upper()) if family.upper().startswith("KXNFL") else family.upper()
        key = (player, fam)
        if key in self.stats:
            return key
        want = pm._norm_name(player)
        for (pid, f), meta in self.meta.items():
            if f == fam and pm._norm_name(meta.get("name") or "") == want:
                return (pid, f)
        # last-name fallback
        for (pid, f), meta in self.meta.items():
            if f == fam and want and want.split()[-1] == pm._norm_name(meta.get("name") or "x").split()[-1]:
                return (pid, f)
        return None

    def mask(self, constraints: list[dict] | None) -> np.ndarray:
        mk = np.ones(self.n, dtype=bool)
        for c in constraints or []:
            if not isinstance(c, dict):
                raise ScenarioError(f"bad constraint: {c!r}")
            team = c.get("team")
            if team is not None and team not in (self.home, self.away):
                raise ScenarioError(f"team {team} is not in {self.away} @ {self.home}")
            if "margin_gte" in c:
                mk &= self.team_margin(_need_team(c)) >= float(c["margin_gte"])
            elif "margin_lte" in c:
                mk &= self.team_margin(_need_team(c)) <= float(c["margin_lte"])
            elif "total_gte" in c:
                mk &= self.T >= float(c["total_gte"])
            elif "total_lte" in c:
                mk &= self.T <= float(c["total_lte"])
            elif "points_gte" in c:
                mk &= self.pts[_need_team(c)] >= float(c["points_gte"])
            elif "points_lte" in c:
                mk &= self.pts[_need_team(c)] <= float(c["points_lte"])
            elif "player" in c and ("gte" in c or "lte" in c):
                key = self.find_player(str(c["player"]), str(c.get("series") or ""))
                if key is None:
                    raise ScenarioError(f"no {c.get('series')} market for player {c['player']} in this game")
                arr = self.stats[key]
                mk &= (arr >= float(c["gte"])) if "gte" in c else (arr <= float(c["lte"]))
            else:
                raise ScenarioError(f"unrecognized constraint: {c}")
        return mk


def _need_team(c: dict) -> str:
    if not c.get("team"):
        raise ScenarioError(f"constraint needs a team: {c}")
    return c["team"]


def simulate(ev, props: dict | None = None, n: int = N_SIMS, seed: int | None = None) -> Sim:
    model = ev.model
    if seed is None:
        seed = int(hashlib.sha1(ev.suffix.encode()).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)

    # 1. margin & total via Gaussian copula
    rho = fv.MT_RHO * (1 if model.mu_m >= 0 else -1)
    z1 = rng.standard_normal(n)
    z2 = rho * z1 + math.sqrt(1 - rho ** 2) * rng.standard_normal(n)
    M = _pmf_quantile(model.pmf_m, fv.MARGIN_SUPPORT, fv.norm_cdf(z1)).astype(float)
    T = _pmf_quantile(model.pmf_t, fv.TOTAL_SUPPORT, fv.norm_cdf(z2)).astype(float)
    odd = ((T + M) % 2) != 0
    T = np.where(odd, T + rng.choice([-1.0, 1.0], n), T)
    T = np.maximum(T, np.abs(M))
    home_pts, away_pts = (T + M) / 2, (T - M) / 2
    pts = {model.home: home_pts, model.away: away_pts}
    sim = Sim(suffix=ev.suffix, away=ev.away, home=ev.home, n=n, M=M, T=T, pts=pts, as_of=ev.as_of)

    if props is None:
        try:
            props = pm.event_props(ev)
        except Exception:
            props = {}
    if not props:
        return sim

    # 2. team factors
    zM = _normal_scores(M, rng)
    factors = {}
    for team in (model.home, model.away):
        zP = _normal_scores(pts[team], rng)
        zS = zM if team == model.home else -zM
        a, b, c = 0.45, _script_coef(ev, team), 0.3
        F = a * zP + b * zS + math.sqrt(max(0.05, 1 - a * a - b * b)) * rng.standard_normal(n)
        R = a * zP + c * zS + math.sqrt(max(0.05, 1 - a * a - c * c)) * rng.standard_normal(n)
        D = 0.75 * zP + math.sqrt(1 - 0.75 ** 2) * rng.standard_normal(n)
        factors[team] = {"F": F / F.std(), "R": R / R.std(), "D": D / D.std(), "S": zS}

    # 3. players
    shared: dict[str, np.ndarray] = {}
    for (pid, fam), pp in props.items():
        team = pp.team if pp.team in factors else None
        if pid not in shared:
            shared[pid] = rng.standard_normal(n)
        idio = PLAYER_SHARED * shared[pid] + math.sqrt(1 - PLAYER_SHARED ** 2) * rng.standard_normal(n)
        if team is None:
            z = idio
        elif fam in PASS_LOAD:
            lam = PASS_LOAD[fam]
            z = lam * factors[team]["F"] + math.sqrt(1 - lam * lam) * idio
        elif fam in RUSH_LOAD:
            lam = RUSH_LOAD[fam]
            z = lam * factors[team]["R"] + math.sqrt(1 - lam * lam) * idio
        elif fam in TD_LOAD:
            lam = TD_LOAD[fam]
            z = lam * factors[team]["D"] + math.sqrt(1 - lam * lam) * idio
        elif fam == "PASSINT":
            lam = -0.3
            z = lam * factors[team]["S"] + math.sqrt(1 - lam * lam) * idio
        else:
            z = idio
        u = fv.norm_cdf(z)
        sim.stats[(pid, fam)] = pp.dist.ppf(u)
        sim.meta[(pid, fam)] = {"name": pp.player_name, "team": pp.team}
    return sim


_sim_cache: dict[str, tuple[float, Sim]] = {}
_sim_lock = threading.Lock()
_sim_key_locks: dict[str, threading.Lock] = {}


def get_sim(ev) -> Sim:
    with _sim_lock:
        hit = _sim_cache.get(ev.suffix)
        if hit and hit[1].as_of == ev.as_of:
            return hit[1]
        lock = _sim_key_locks.setdefault(ev.suffix, threading.Lock())
    with lock:
        hit = _sim_cache.get(ev.suffix)
        if hit and hit[1].as_of == ev.as_of:
            return hit[1]
        sim = simulate(ev)
        with _sim_lock:
            _sim_cache[ev.suffix] = (time.time(), sim)
        return sim


# -- analytic fair (matches /edges) ------------------------------------------------------


def analytic_fair(ev, m: dict) -> float | None:
    if m["kind"] in ("game", "team"):
        return ev.model.fair(m)
    try:
        return pm.fair_for_prop(ev, m)
    except Exception:
        return None


def _leg_view(m: dict, side: str, fair_yes: float | None) -> dict:
    cost = fv.side_cost(m, side)
    fair = None if fair_yes is None else (fair_yes if side == "yes" else 1 - fair_yes)
    return {
        "market_ticker": m["ticker"], "side": side, "title": m["title"], "event_ticker": m["event_ticker"],
        "yes_bid": m.get("yes_bid"), "yes_ask": m.get("yes_ask"),
        "fair": None if fair is None else round(fair, 4), "cost": cost,
        "edge": None if fair is None or cost is None else round(fair - cost, 4),
        "american_cost": fv.american(cost), "american_fair": fv.american(fair),
        "family": m["family"], "team_code": m.get("team_code"), "player_name": m.get("player_name"),
        "threshold": m.get("threshold"),
    }


def _normalize_legs(legs) -> list[dict]:
    if not isinstance(legs, list) or not legs:
        raise ScenarioError("legs must be a non-empty list of {market_ticker, side}")
    out = []
    for l in legs:
        if not isinstance(l, dict) or not l.get("market_ticker"):
            raise ScenarioError(f"bad leg: {l!r}")
        side = str(l.get("side") or "yes").lower()
        if side not in ("yes", "no"):
            raise ScenarioError(f"side must be yes or no: {l!r}")
        out.append({"market_ticker": str(l["market_ticker"]).upper(), "side": side})
    return out


def price_legs(legs: list[dict], scenario: dict | None = None) -> dict:
    """Correlated parlay price. Legs in the same game are priced jointly from
    that game's sim; different games are independent. Without a scenario,
    single-leg fairs are the analytic ones (same as /edges) and the joint is
    the analytic product times the sim's correlation ratio."""
    legs = _normalize_legs(legs)
    scen_suffix = fv.game_suffix(scenario["event"]) if scenario and scenario.get("event") else None
    constraints = (scenario or {}).get("constraints") or []
    by_event: dict[str, list[int]] = {}
    for i, l in enumerate(legs):
        by_event.setdefault(fv.game_suffix(l["market_ticker"]), []).append(i)

    views: list[dict | None] = [None] * len(legs)
    joint = 1.0
    indep = 1.0
    scenario_applied = False
    for suffix, idxs in by_event.items():
        ev = fv.load_event(suffix)
        tick = ev.by_ticker()
        sim = get_sim(ev)
        apply = bool(constraints) and scen_suffix == suffix
        mk = sim.mask(constraints) if apply else np.ones(sim.n, dtype=bool)
        if apply:
            if mk.sum() < 50:
                raise ScenarioError("scenario is (nearly) impossible under the model: fewer than 50 of "
                                    f"{sim.n} sims satisfy it")
            scenario_applied = True
        hits, sim_marg, fairs = [], [], []
        for i in idxs:
            l = legs[i]
            m = tick.get(l["market_ticker"])
            if m is None:
                raise ScenarioError(f"unknown or closed market: {l['market_ticker']}")
            y = sim.yes(m)
            if y is not None:
                h = y if l["side"] == "yes" else ~y
                hits.append(h)
                sim_marg.append(float(h[mk].mean()))
            if apply and y is not None:
                fy = float(y[mk].mean())
            else:
                fy = analytic_fair(ev, m)
                if fy is None and y is not None:
                    fy = float(y.mean())
                if fy is None:
                    fy = fv.mid(m)
            v = _leg_view(m, l["side"], fy)
            views[i] = v
            fairs.append(v["fair"] if v["fair"] is not None else 0.0)
        # joint for this event
        prod_fair = float(np.prod(fairs))
        indep *= prod_fair
        if hits and len(hits) == len(idxs):
            allh = np.logical_and.reduce(hits)[mk]
            sim_joint = float(allh.mean())
            if apply:
                ej = sim_joint
            else:
                sim_prod = float(np.prod(sim_marg))
                ratio = (sim_joint / sim_prod) if sim_prod > 0 else 1.0
                ej = min(prod_fair * ratio, min(fairs))
        else:
            ej = prod_fair
        joint *= ej

    costs = [v["cost"] for v in views]
    parlay_cost = float(np.prod(costs)) if all(c for c in costs) else None
    return {
        "legs": views,
        "joint_fair": round(joint, 5),
        "indep_fair": round(indep, 5),
        "parlay_cost": None if parlay_cost is None else round(parlay_cost, 5),
        "joint_american": fv.american(joint),
        "parlay_american_cost": fv.american(parlay_cost),
        "ev_per_dollar": None if not parlay_cost else round(joint / parlay_cost - 1, 4),
        "scenario_applied": scenario_applied,
        "same_event": len(by_event) == 1,
    }


def condition(event: str, constraints: list[dict]) -> dict:
    """Every market in the game re-priced under the scenario."""
    if not event:
        raise ScenarioError("missing event")
    if not isinstance(constraints, list) or not constraints:
        raise ScenarioError("constraints must be a non-empty list")
    ev = fv.load_event(event)
    sim = get_sim(ev)
    mk = sim.mask(constraints)
    p = float(mk.mean())
    if mk.sum() < 50:
        raise ScenarioError(f"scenario is (nearly) impossible under the model (p ~ {p:.4f})")
    rows = []
    for m in ev.markets:
        y = sim.yes(m)
        if y is None:
            continue
        base = analytic_fair(ev, m)
        if base is None:
            base = float(y.mean())
        cond = float(y[mk].mean())
        cost = m.get("yes_ask")
        rows.append({
            "ticker": m["ticker"], "side": "yes", "title": m["title"], "family": m["family"],
            "team_code": m.get("team_code"), "player_name": m.get("player_name"), "threshold": m.get("threshold"),
            "fair_base": round(base, 4), "fair_cond": round(cond, 4), "cost": cost,
            "edge_cond": None if not cost else round(cond - cost, 4),
            "american_fair_cond": fv.american(cond),
            "yes_bid": m.get("yes_bid"), "no_cost": fv.side_cost(m, "no"),
        })
    rows.sort(key=lambda r: -abs(r["fair_cond"] - r["fair_base"]))
    return {"event": ev.event_ticker, "constraints": constraints, "p_scenario": round(p, 4), "markets": rows}


def ladder(event: str, team: str, family: str, strikes: list | None = None, stakes: list | None = None,
           scenario: dict | None = None, include_ml: bool = False) -> dict:
    """A same-team stack of singles (e.g. SEA ML / -3.5 / -6.5) priced jointly,
    with P&L by final-margin (or team-points) bucket.

    family SPREAD: each strike is a spread (3.5 or -3.5 both mean 'wins by 4+');
    a strike of 0 (or include_ml) adds the moneyline. family ML: just the ML.
    family TEAMTOTAL: team points over each strike."""
    if not event or not team:
        raise ScenarioError("event and team are required")
    family = (family or "").upper()
    if family not in ("ML", "SPREAD", "TEAMTOTAL"):
        raise ScenarioError("family must be ML, SPREAD or TEAMTOTAL")
    ev = fv.load_event(event)
    team = team.upper()
    if team not in (ev.home, ev.away):
        raise ScenarioError(f"team {team} is not in {ev.matchup}")
    sim = get_sim(ev)
    constraints = (scenario or {}).get("constraints") or []
    if constraints and scenario.get("event") and fv.game_suffix(scenario["event"]) != ev.suffix:
        constraints = []
    mk = sim.mask(constraints)
    if mk.sum() < 50:
        raise ScenarioError("scenario is (nearly) impossible under the model")

    fam_markets = [m for m in ev.markets if m.get("team_code") == team]
    chosen: list[tuple[float, dict]] = []
    want = [] if family == "ML" else [abs(float(s)) for s in (strikes or []) if s is not None]
    if family == "ML" or include_ml or (family == "SPREAD" and 0.0 in want):
        ml = next((m for m in fam_markets if m["family"] == "ML"), None)
        if ml is None:
            raise ScenarioError(f"no moneyline market for {team}")
        chosen.append((0.0, ml))
    target_fam = "SPREAD" if family in ("SPREAD", "ML") else "TEAMTOTAL"
    for s in want:
        if family == "SPREAD" and s == 0.0:
            continue
        m = next((m for m in fam_markets if m["family"] == target_fam and m.get("threshold") is not None
                  and abs(m["threshold"] - s) < 0.01), None)
        if m is None:
            avail = sorted({m["threshold"] for m in fam_markets if m["family"] == target_fam and m.get("threshold") is not None})
            raise ScenarioError(f"no {team} {target_fam} market at {s:g}; available: {avail}")
        chosen.append((s, m))
    if not chosen:
        raise ScenarioError("pick at least one strike")
    chosen.sort(key=lambda x: x[0])
    stakes = list(stakes or [])
    stakes = [float(stakes[i]) if i < len(stakes) and stakes[i] is not None else 1.0 for i in range(len(chosen))]

    var = sim.team_margin(team) if target_fam == "SPREAD" else sim.pts[team]
    v = var[mk]
    legs = []
    for (x, m), stake in zip(chosen, stakes):
        cost = m.get("yes_ask")
        if not cost:
            raise ScenarioError(f"{m['title']} has no ask to buy YES at")
        if constraints:
            fair = float((v > x).mean())
        else:
            fair = analytic_fair(ev, m)
            fair = float((v > x).mean()) if fair is None else fair
        legs.append({"market_ticker": m["ticker"], "side": "yes", "title": m["title"],
                     "strike": x if m["family"] != "ML" else 0, "fair": round(fair, 4), "cost": cost,
                     "stake": stake, "contracts": round(stake / cost, 3),
                     "american_cost": fv.american(cost), "american_fair": fv.american(fair)})

    cuts = sorted({int(math.floor(x)) + 1 for x, _ in chosen})
    edges = [None] + cuts
    buckets = []
    for i, lo in enumerate(edges):
        hi = (cuts[i] - 1) if i < len(cuts) else None
        in_b = np.ones(len(v), dtype=bool)
        if lo is not None:
            in_b &= v >= lo
        if hi is not None:
            in_b &= v <= hi
        rep = lo if lo is not None else (hi if hi is not None else 0)
        hit = [rep > x for x, _ in chosen]
        pnl = sum((leg["stake"] / leg["cost"] - leg["stake"]) if h else -leg["stake"] for h, leg in zip(hit, legs))
        buckets.append({"label": _bucket_label(team, target_fam, lo, hi), "margin_lo": lo, "margin_hi": hi,
                        "prob": round(float(in_b.mean()), 4), "legs_hit": int(sum(hit)), "pnl": round(pnl, 4)})
    ev_ = sum(b["prob"] * b["pnl"] for b in buckets)
    return {
        "event": ev.event_ticker, "team": team, "family": family, "matchup": ev.matchup,
        "legs": legs, "outcome_table": buckets,
        "p_all": buckets[-1]["prob"], "p_none": buckets[0]["prob"],
        "ev": round(ev_, 4), "max_loss": round(min(b["pnl"] for b in buckets), 4),
        "max_win": round(max(b["pnl"] for b in buckets), 4),
        "total_stake": round(sum(stakes), 4),
        "scenario_applied": bool(constraints),
    }


def _bucket_label(team: str, fam: str, lo, hi) -> str:
    if fam == "TEAMTOTAL":
        if lo is None:
            return f"{team} {hi} pts or fewer"
        if hi is None:
            return f"{team} {lo}+ pts"
        return f"{team} {lo}-{hi} pts" if lo != hi else f"{team} {lo} pts"
    def by(k):
        return f"{team} by {k}"
    if lo is None:
        if hi == 0:
            return f"{team} loses or ties"
        if hi < 0:
            return f"{team} loses by {-hi}+"
        return f"{team} loses, ties or wins by {hi} or less"
    if hi is None:
        return f"{team} by {lo}+" if lo > 0 else f"{team} within {-lo} or wins"
    if lo == hi:
        return by(lo) if lo > 0 else ("tie" if lo == 0 else f"{team} loses by {-lo}")
    if lo > 0:
        return f"{team} by {lo}-{hi}"
    return f"{team} margin {lo} to {hi}"
