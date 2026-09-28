"""
Game-line fair value: a margin distribution and a total distribution per game,
fitted to the Kalshi ladder and blended with the nflverse closing-style line.

Conventions (all confirmed against live Kalshi markets):
  * Every NFL game/prop market resolves YES iff the stat is strictly greater
    than `floor_strike` (strike_type "greater"). Spreads/totals/team totals use
    half-point strikes, so there are no pushes; "N+" props use floor N-0.5.
  * KXNFLGAME-{G}-{TEAM}: TEAM wins. A (rare, ~0.3%) tie resolves NO for both
    sides, so P(home ML) = P(M > 0) and P(away ML) = P(M < 0) with the tie mass
    left in neither.
  * KXNFLSPREAD-{G}-{TEAM}{N}: TEAM wins by more than floor_strike (N - 0.5).
  * M = home score - away score (nflverse `result` sign), T = total points.

Shape: residuals `result - spread_line` / `total - total_line` from nflverse
schedules 2019-2025 give per-integer "key number" weights (how much more often
a final margin of 3, 7, 10, ... happens than a smooth normal would say). The
pmf for one game is a discretized normal(mu, sd) times those weights, so
moving mu keeps the key-number spikes on the integers they belong to.

Location: weighted least squares of the model's P(yes) against every ML /
spread (or total) strike mid, weights ~ log(volume) / bid-ask width, then a
light blend toward nflverse spread_line / total_line when available.
"""

from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import numpy as np

from combo import american_odds

# -- math helpers (no scipy) ------------------------------------------------------

_SQRT2 = math.sqrt(2.0)


def erf(x):
    """Vectorized erf (Abramowitz & Stegun 7.1.26, |err| < 1.5e-7)."""
    x = np.asarray(x, dtype=float)
    s = np.sign(x)
    a = np.abs(x)
    t = 1.0 / (1.0 + 0.3275911 * a)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * np.exp(-a * a)
    return s * y


def norm_cdf(x):
    return 0.5 * (1.0 + erf(np.asarray(x, dtype=float) / _SQRT2))


def norm_ppf(p):
    """Vectorized inverse normal CDF (Acklam's rational approximation, ~1e-9)."""
    p = np.clip(np.asarray(p, dtype=float), 1e-12, 1 - 1e-12)
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00]
    lo, hi = 0.02425, 1 - 0.02425
    out = np.empty_like(p)
    m = p < lo
    q = np.sqrt(-2 * np.log(p[m]))
    out[m] = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
             ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    m2 = p > hi
    q = np.sqrt(-2 * np.log(1 - p[m2]))
    out[m2] = -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
              ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    mid = ~(m | m2)
    q = p[mid] - 0.5
    r = q * q
    out[mid] = (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
               (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    return out


def american(p) -> int | None:
    try:
        return american_odds(float(p)) if p is not None else None
    except (TypeError, ValueError):
        return None


# -- empirical shape ---------------------------------------------------------------

MARGIN_SUPPORT = np.arange(-80, 81)      # home - away
TOTAL_SUPPORT = np.arange(0, 141)
MARGIN_SD_PRIOR = 12.7                   # sd(result - spread_line), 2019-2025
TOTAL_SD_PRIOR = 13.1                    # sd(total - total_line), 2019-2025
MT_RHO = 0.03                            # corr(fav margin resid, total resid) ~ 0.026
NFLVERSE_LINE_WEIGHT = 0.15              # blend weight on nflverse line vs Kalshi fit
_KEY_PSEUDO = 8.0                        # shrink per-integer key weights toward 1


def _disc_normal(support: np.ndarray, mu, sd) -> np.ndarray:
    """P(X = k) for a normal rounded to integers. mu/sd may be column vectors."""
    mu = np.asarray(mu, dtype=float)
    sd = np.asarray(sd, dtype=float)
    return norm_cdf((support + 0.5 - mu) / sd) - norm_cdf((support - 0.5 - mu) / sd)


@lru_cache(maxsize=1)
def _key_weights_from_history() -> tuple[np.ndarray, np.ndarray]:
    """Observed/expected frequency for each final margin (symmetric in |k|)
    and each final total, vs. a smooth normal around each game's line."""
    import pandas as pd

    import nflverse_data as nd

    frames = []
    for s in range(2019, 2026):
        try:
            frames.append(nd.load_schedules(s))
        except Exception:
            continue
    if not frames:
        raise RuntimeError("no schedules")
    d = pd.concat(frames)
    d = d[d["result"].notna() & d["spread_line"].notna() & d["total_line"].notna()]
    res = d["result"].to_numpy(float)
    spr = d["spread_line"].to_numpy(float)[:, None]
    tot = d["total"].to_numpy(float)
    tl = d["total_line"].to_numpy(float)[:, None]

    exp_m = _disc_normal(MARGIN_SUPPORT[None, :], spr, MARGIN_SD_PRIOR).sum(axis=0)
    obs_m = np.array([(res == k).sum() for k in MARGIN_SUPPORT], dtype=float)
    # symmetrize by |k|
    absk = np.abs(MARGIN_SUPPORT)
    obs_abs = np.array([obs_m[absk == a].sum() for a in range(81)])
    exp_abs = np.array([exp_m[absk == a].sum() for a in range(81)])
    w_abs = (obs_abs + _KEY_PSEUDO) / (exp_abs + _KEY_PSEUDO)
    w_m = w_abs[absk]

    exp_t = _disc_normal(TOTAL_SUPPORT[None, :], tl, TOTAL_SD_PRIOR).sum(axis=0)
    obs_t = np.array([(tot == k).sum() for k in TOTAL_SUPPORT], dtype=float)
    w_t = (obs_t + _KEY_PSEUDO) / (exp_t + _KEY_PSEUDO)
    # A lightly smoothed total weight: totals have weak key numbers, and the
    # per-integer counts are noisy, so average each weight with its neighbours.
    w_t = np.convolve(np.pad(w_t, 1, mode="edge"), [0.25, 0.5, 0.25], mode="valid")
    return w_m, w_t


def key_weights() -> tuple[np.ndarray, np.ndarray]:
    """(margin weights over MARGIN_SUPPORT, total weights over TOTAL_SUPPORT).
    Falls back to flat weights (plain discretized normal) with a tie
    suppression if history can't be loaded."""
    try:
        return _key_weights_from_history()
    except Exception:
        w_m = np.ones(len(MARGIN_SUPPORT))
        w_m[MARGIN_SUPPORT == 0] = 0.1
        return w_m, np.ones(len(TOTAL_SUPPORT))


def margin_pmf(mu, sd, weights: np.ndarray | None = None) -> np.ndarray:
    w = key_weights()[0] if weights is None else weights
    mu = np.atleast_1d(np.asarray(mu, dtype=float))[:, None]
    sd = np.atleast_1d(np.asarray(sd, dtype=float))[:, None]
    p = _disc_normal(MARGIN_SUPPORT[None, :], mu, sd) * w[None, :]
    return p / p.sum(axis=1, keepdims=True)


def total_pmf(mu, sd, weights: np.ndarray | None = None) -> np.ndarray:
    w = key_weights()[1] if weights is None else weights
    mu = np.atleast_1d(np.asarray(mu, dtype=float))[:, None]
    sd = np.atleast_1d(np.asarray(sd, dtype=float))[:, None]
    p = _disc_normal(TOTAL_SUPPORT[None, :], mu, sd) * w[None, :]
    return p / p.sum(axis=1, keepdims=True)


# -- market normalization ------------------------------------------------------------

GAME_FAMILIES = {"KXNFLGAME": "ML", "KXNFLSPREAD": "SPREAD", "KXNFLTOTAL": "TOTAL", "KXNFLTEAMTOTAL": "TEAMTOTAL"}


def _f(v) -> float | None:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def game_suffix(event_or_ticker: str) -> str:
    """'KXNFLGAME-26SEP27LARDEN' / any market ticker / bare suffix -> '26SEP27LARDEN'."""
    s = (event_or_ticker or "").strip().upper()
    parts = s.split("-")
    return parts[1] if len(parts) >= 2 else parts[0]


def family_of(series: str) -> str:
    return GAME_FAMILIES.get(series) or series.replace("KXNFL", "")


def normalize_market(m: dict, player_name: str | None = None, team_code: str | None = None,
                     teams: tuple[str, ...] = ()) -> dict:
    """Raw Kalshi market dict -> the flat shape every engine module uses."""
    ticker = m["ticker"]
    parts = ticker.split("-")
    series = parts[0]
    family = family_of(series)
    threshold = _f(m.get("floor_strike"))
    if threshold is None:
        threshold = _f(m.get("cap_strike"))
    custom = m.get("custom_strike") or {}
    player_id = custom.get("football_player")
    last = parts[2] if len(parts) >= 3 else ""
    if team_code is None and teams and len(parts) >= 3 and family != "TOTAL":
        for t in sorted(teams, key=len, reverse=True):
            if last.startswith(t):
                team_code = t
                break
    if team_code is None and len(parts) >= 3:
        mt = re.match(r"^([A-Z]{2,3})", last)
        if series in GAME_FAMILIES and family != "TOTAL" and mt:
            team_code = mt.group(1)
        elif player_id and mt:
            team_code = mt.group(1)
    if family in ("ML", "SPREAD", "TOTAL"):
        kind = "game"
    elif family == "TEAMTOTAL":
        kind = "team"
    else:
        kind = "player"
    out = {
        "ticker": ticker,
        "series": series,
        "event_ticker": m.get("event_ticker") or "-".join(parts[:2]),
        "game_suffix": parts[1] if len(parts) > 1 else "",
        "family": family,
        "kind": kind,
        "team_code": team_code,
        "player_id": player_id,
        "player_name": player_name,
        "threshold": threshold,
        "yes_bid": _f(m.get("yes_bid_dollars")),
        "yes_ask": _f(m.get("yes_ask_dollars")),
        "last": _f(m.get("last_price_dollars")),
        "volume": _f(m.get("volume_fp")) or 0.0,
        "open_interest": _f(m.get("open_interest_fp")) or 0.0,
        "status": m.get("status"),
        "raw_title": m.get("title"),
    }
    out["title"] = market_title(out)
    return out


def market_title(m: dict) -> str:
    fam, t, team = m["family"], m.get("threshold"), m.get("team_code")
    if fam == "ML":
        return f"{team} ML"
    if fam == "SPREAD" and t is not None:
        return f"{team} -{t:g}"
    if fam == "TOTAL" and t is not None:
        return f"Over {t:g}"
    if fam == "TEAMTOTAL" and t is not None:
        return f"{team} over {t:g} pts"
    return m.get("raw_title") or m["ticker"]


def mid(m: dict) -> float | None:
    b, a = m.get("yes_bid"), m.get("yes_ask")
    if b and a:
        return (b + a) / 2
    if a and not b:
        return a / 2 if a < 0.1 else None
    return m.get("last") or None


def side_cost(m: dict, side: str) -> float | None:
    """Same rule as combo.leg_prices: YES = ask, NO = 1 - yes_bid."""
    if side == "yes":
        return m.get("yes_ask") or None
    b = m.get("yes_bid")
    return round(1 - b, 4) if b else None


def kalshi_fee(p: float | None) -> float:
    """Kalshi taker fee per $1 contract: ceil-ish 0.07 * p * (1-p) (estimate)."""
    if not p:
        return 0.0
    return 0.07 * p * (1 - p)


# -- the game model --------------------------------------------------------------------


@dataclass
class GameModel:
    away: str
    home: str
    mu_m: float
    sd_m: float
    mu_t: float
    sd_t: float
    pmf_m: np.ndarray = field(repr=False)
    pmf_t: np.ndarray = field(repr=False)
    fit: dict = field(default_factory=dict)

    # -- probabilities --
    def p_margin_gt(self, team: str, x: float) -> float:
        """P(team's margin > x). ML is x = 0 (tie -> neither)."""
        sign = 1 if team == self.home else -1
        return float(self.pmf_m[(sign * MARGIN_SUPPORT) > x].sum())

    def p_total_gt(self, x: float) -> float:
        return float(self.pmf_t[TOTAL_SUPPORT > x].sum())

    @property
    def _pts_pmf(self) -> tuple[np.ndarray, np.ndarray]:
        """pmf of T+M (= 2*home) and T-M (= 2*away), independent approx."""
        cached = getattr(self, "_pts_cache", None)
        if cached is None:
            conv_home = np.convolve(self.pmf_t, self.pmf_m)          # support T+M
            conv_away = np.convolve(self.pmf_t, self.pmf_m[::-1])    # support T-M
            lo = TOTAL_SUPPORT[0] + MARGIN_SUPPORT[0]
            sup = np.arange(lo, lo + len(conv_home))
            cached = (sup, conv_home, conv_away)
            object.__setattr__(self, "_pts_cache", cached)
        return cached

    def p_team_points_gt(self, team: str, x: float) -> float:
        sup, home, away = self._pts_pmf
        arr = home if team == self.home else away
        return float(arr[sup > 2 * x].sum())

    def implied_points(self) -> dict:
        return {self.home: (self.mu_t + self.mu_m) / 2, self.away: (self.mu_t - self.mu_m) / 2}

    def fair(self, m: dict) -> float | None:
        fam, t, team = m["family"], m.get("threshold"), m.get("team_code")
        if fam == "ML" and team in (self.home, self.away):
            return self.p_margin_gt(team, 0)
        if t is None:
            return None
        if fam == "SPREAD" and team in (self.home, self.away):
            return self.p_margin_gt(team, t)
        if fam == "TOTAL":
            return self.p_total_gt(t)
        if fam == "TEAMTOTAL" and team in (self.home, self.away):
            return self.p_team_points_gt(team, t)
        return None

    def summary(self) -> dict:
        ip = self.implied_points()
        fav = self.home if self.mu_m >= 0 else self.away
        return {
            "away": self.away, "home": self.home,
            "fav": fav, "fav_line": round(abs(self.mu_m), 2),
            "home_margin_mu": round(self.mu_m, 2), "margin_sd": round(self.sd_m, 2),
            "total_mu": round(self.mu_t, 2), "total_sd": round(self.sd_t, 2),
            "implied_points": {k: round(v, 2) for k, v in ip.items()},
            "p_home_ml": round(self.p_margin_gt(self.home, 0), 4),
            "p_away_ml": round(self.p_margin_gt(self.away, 0), 4),
            "fit": self.fit,
        }


def _obs_weight(m: dict) -> float:
    b, a = m.get("yes_bid"), m.get("yes_ask")
    if not b or not a or a <= b - 1e-9:
        return 0.0
    width = a - b
    if width > 0.15:
        return 0.0
    return math.log1p(m.get("volume") or 0) / (width + 0.01)


def _grid_fit(masks: np.ndarray, mids: np.ndarray, w: np.ndarray, pmf_fn, mu_grid: np.ndarray,
              sd_grid: np.ndarray, sd_prior: float) -> tuple[float, float, float]:
    """Brute-force WLS over (mu, sd) with a soft prior on sd. masks: (n_obs, n_support)."""
    MU, SD = np.meshgrid(mu_grid, sd_grid, indexing="ij")
    pm = pmf_fn(MU.ravel(), SD.ravel())                     # (G, S)
    pred = pm @ masks.T                                      # (G, n_obs)
    wn = w / w.sum()
    loss = ((pred - mids[None, :]) ** 2 * wn[None, :]).sum(axis=1)
    loss = loss + 0.0004 * ((SD.ravel() - sd_prior) / 1.5) ** 2
    i = int(np.argmin(loss))
    rms = float(math.sqrt(((pred[i] - mids) ** 2 * wn).sum()))
    return float(MU.ravel()[i]), float(SD.ravel()[i]), rms


def fit_game(markets: list[dict], away: str, home: str, spread_line: float | None = None,
             total_line: float | None = None, line_weight: float = NFLVERSE_LINE_WEIGHT) -> GameModel:
    """markets: normalized markets for this game (any families; non-game ones ignored).
    spread_line: nflverse home-margin line (positive = home favored)."""
    w_m, w_t = key_weights()

    # Margin observations: P(sign*M > x)
    rows, mids_m, ws_m = [], [], []
    for m in markets:
        if m["family"] not in ("ML", "SPREAD") or m.get("team_code") not in (home, away):
            continue
        p, w = mid(m), _obs_weight(m)
        if p is None or w <= 0:
            continue
        x = 0.0 if m["family"] == "ML" else m.get("threshold")
        if x is None:
            continue
        sign = 1 if m["team_code"] == home else -1
        rows.append(((sign * MARGIN_SUPPORT) > x).astype(float))
        mids_m.append(p)
        ws_m.append(w)
    fit: dict[str, Any] = {"margin_obs": len(rows)}
    if len(rows) >= 2:
        masks = np.array(rows)
        mids_a, ws_a = np.array(mids_m), np.array(ws_m)
        coarse = np.arange(-30, 30.01, 0.5)
        mu0, sd0, _ = _grid_fit(masks, mids_a, ws_a, lambda a, b: margin_pmf(a, b, w_m), coarse,
                                np.arange(10.0, 16.01, 1.0), MARGIN_SD_PRIOR)
        mu_m, sd_m, rms_m = _grid_fit(masks, mids_a, ws_a, lambda a, b: margin_pmf(a, b, w_m),
                                      np.arange(mu0 - 1, mu0 + 1.001, 0.05), np.arange(sd0 - 1, sd0 + 1.01, 0.25),
                                      MARGIN_SD_PRIOR)
        fit.update(margin_mu_kalshi=round(mu_m, 2), margin_rms=round(rms_m, 4))
        if spread_line is not None and not _isnan(spread_line):
            mu_m = (1 - line_weight) * mu_m + line_weight * float(spread_line)
    elif spread_line is not None and not _isnan(spread_line):
        mu_m, sd_m = float(spread_line), MARGIN_SD_PRIOR
        fit["margin_source"] = "nflverse"
    elif len(rows) == 1:
        # single ML: invert with the prior sd
        grid = np.arange(-30, 30.01, 0.1)
        mu_m, sd_m, _ = _grid_fit(np.array(rows), np.array(mids_m), np.array(ws_m),
                                  lambda a, b: margin_pmf(a, b, w_m), grid, np.array([MARGIN_SD_PRIOR]), MARGIN_SD_PRIOR)
    else:
        mu_m, sd_m = 0.0, MARGIN_SD_PRIOR
        fit["margin_source"] = "default"
    fit["nflverse_spread_line"] = None if spread_line is None or _isnan(spread_line) else float(spread_line)

    rows, mids_t, ws_t = [], [], []
    for m in markets:
        if m["family"] != "TOTAL" or m.get("threshold") is None:
            continue
        p, w = mid(m), _obs_weight(m)
        if p is None or w <= 0:
            continue
        rows.append((TOTAL_SUPPORT > m["threshold"]).astype(float))
        mids_t.append(p)
        ws_t.append(w)
    fit["total_obs"] = len(rows)
    if len(rows) >= 2:
        masks = np.array(rows)
        mids_a, ws_a = np.array(mids_t), np.array(ws_t)
        mu0, sd0, _ = _grid_fit(masks, mids_a, ws_a, lambda a, b: total_pmf(a, b, w_t),
                                np.arange(25, 70.01, 0.5), np.arange(10.0, 17.01, 1.0), TOTAL_SD_PRIOR)
        mu_t, sd_t, rms_t = _grid_fit(masks, mids_a, ws_a, lambda a, b: total_pmf(a, b, w_t),
                                      np.arange(mu0 - 1, mu0 + 1.001, 0.05), np.arange(sd0 - 1, sd0 + 1.01, 0.25),
                                      TOTAL_SD_PRIOR)
        fit.update(total_mu_kalshi=round(mu_t, 2), total_rms=round(rms_t, 4))
        if total_line is not None and not _isnan(total_line):
            mu_t = (1 - line_weight) * mu_t + line_weight * float(total_line)
    elif total_line is not None and not _isnan(total_line):
        mu_t, sd_t = float(total_line), TOTAL_SD_PRIOR
        fit["total_source"] = "nflverse"
    else:
        mu_t, sd_t = 44.0, TOTAL_SD_PRIOR
        fit["total_source"] = "default"
    fit["nflverse_total_line"] = None if total_line is None or _isnan(total_line) else float(total_line)

    return GameModel(away=away, home=home, mu_m=mu_m, sd_m=sd_m, mu_t=mu_t, sd_t=sd_t,
                     pmf_m=margin_pmf(mu_m, sd_m, w_m)[0], pmf_t=total_pmf(mu_t, sd_t, w_t)[0], fit=fit)


def _isnan(v) -> bool:
    try:
        return math.isnan(float(v))
    except (TypeError, ValueError):
        return True


# -- edges & arbs ------------------------------------------------------------------------


def confidence(m: dict, fit_rms: float | None = None, model_quality: float = 1.0) -> str:
    b, a = m.get("yes_bid"), m.get("yes_ask")
    width = (a - b) if (a and b) else 1.0
    vol = m.get("volume") or 0
    score = 0
    score += 2 if vol >= 50_000 else 1 if vol >= 5_000 else 0
    score += 2 if width <= 0.02 else 1 if width <= 0.05 else 0
    if fit_rms is not None:
        score += 1 if fit_rms <= 0.015 else 0 if fit_rms <= 0.03 else -1
    if model_quality < 0.5:
        score -= 1
    return "high" if score >= 4 else "med" if score >= 2 else "low"


def edge_rows(m: dict, fair_yes: float | None, matchup: str, min_edge: float = -1.0,
              conf: str = "med", reasons: list[str] | None = None) -> list[dict]:
    """Both sides of one market as contract `edges[]` rows, filtered by min_edge."""
    if fair_yes is None:
        return []
    out = []
    for side in ("yes", "no"):
        cost = side_cost(m, side)
        if not cost or cost <= 0 or cost >= 1:
            continue
        fair = fair_yes if side == "yes" else 1 - fair_yes
        edge = fair - cost
        if edge < min_edge:
            continue
        mp = mid(m)
        rs = list(reasons or [])
        if mp is not None:
            rs.insert(0, f"fair {fair * 100:.1f}% vs Kalshi mid {((mp if side == 'yes' else 1 - mp)) * 100:.1f}%")
        out.append({
            "ticker": m["ticker"], "side": side,
            "event_ticker": m["event_ticker"], "matchup": matchup,
            "title": m["title"] if side == "yes" else f"NO: {m['title']}",
            "kind": m["kind"], "family": m["family"],
            "team_code": m.get("team_code"), "player_name": m.get("player_name"),
            "threshold": m.get("threshold"),
            "fair": round(fair, 4), "cost": round(cost, 4), "edge": round(edge, 4),
            "ev_per_dollar": round(fair / cost - 1, 4),
            "american_cost": american(cost), "american_fair": american(fair),
            "confidence": conf, "reasons": rs,
            "yes_bid": m.get("yes_bid"), "yes_ask": m.get("yes_ask"), "volume": m.get("volume"),
        })
    return out


def game_edges(markets: list[dict], model: GameModel, matchup: str, min_edge: float = 0.03) -> list[dict]:
    out = []
    for m in markets:
        if m["kind"] not in ("game", "team"):
            continue
        p = model.fair(m)
        if p is None:
            continue
        rms = model.fit.get("total_rms" if m["family"] == "TOTAL" else "margin_rms")
        if m["family"] == "TEAMTOTAL":
            rms = None
        reasons = []
        fam = m["family"]
        if fam in ("ML", "SPREAD"):
            fav = model.home if model.mu_m >= 0 else model.away
            reasons.append(f"model line {fav} -{abs(model.mu_m):.1f} (sd {model.sd_m:.1f}, key-number pmf)")
        elif fam == "TOTAL":
            reasons.append(f"model total {model.mu_t:.1f} (sd {model.sd_t:.1f})")
        else:
            ip = model.implied_points()
            reasons.append(f"model implied {m['team_code']} {ip.get(m['team_code'], 0):.1f} pts from line+total")
        out += edge_rows(m, p, matchup, min_edge, confidence(m, rms), reasons)
    return out


def _nested_ladders(markets: list[dict], away: str, home: str) -> dict[str, list[tuple[float, dict]]]:
    """Groups of markets whose YES events are nested: for each group, a list of
    (x, market) where YES == (variable > x). Larger x => subset event."""
    lad: dict[str, list[tuple[float, dict]]] = {}
    for m in markets:
        fam, t, team = m["family"], m.get("threshold"), m.get("team_code")
        if fam == "ML" and team in (away, home):
            lad.setdefault(f"MARGIN:{team}", []).append((0.0, m))
        elif fam == "SPREAD" and team in (away, home) and t is not None:
            lad.setdefault(f"MARGIN:{team}", []).append((t, m))
        elif fam == "TOTAL" and t is not None:
            lad.setdefault("TOTAL", []).append((t, m))
        elif fam == "TEAMTOTAL" and team and t is not None:
            lad.setdefault(f"TEAMTOTAL:{team}", []).append((t, m))
        elif m["kind"] == "player" and t is not None and m.get("player_id"):
            lad.setdefault(f"{fam}:{m['player_id']}", []).append((t, m))
    for v in lad.values():
        v.sort(key=lambda x: x[0])
    return lad


def ladder_arbs(markets: list[dict], away: str, home: str, event_ticker: str = "") -> tuple[list[dict], list[dict]]:
    """Returns (arbs, soft_violations).

    arb 1 (nested): YES(x_hi) implies YES(x_lo). If bid(x_hi) > ask(x_lo):
      buy YES x_lo at ask + buy NO x_hi at 1-bid -> pays >= $1 in every outcome.
    arb 2 (disjoint): home by > a and away by > b (a, b >= 0) can't both hit.
      If bid_h + bid_a > 1: buy NO on both -> pays >= $1.
    arb 3 (exhaustive-ish): home ML + away ML cover everything except a tie.
      If ask_h + ask_a < 1: buy both YES -> pays $1 unless tie (~0.3%).
    soft: mids that break monotonicity without a tradable arb."""
    arbs, soft = [], []
    lad = _nested_ladders(markets, away, home)
    for key, items in lad.items():
        for i in range(len(items)):
            xl, ml = items[i]
            for j in range(i + 1, len(items)):
                xh, mh = items[j]
                if xh <= xl:
                    continue
                ask_lo, bid_hi = ml.get("yes_ask"), mh.get("yes_bid")
                if ask_lo and bid_hi and bid_hi > ask_lo + 1e-9:
                    cost_lo, cost_hi = ask_lo, round(1 - bid_hi, 4)
                    arbs.append({
                        "event_ticker": event_ticker or ml["event_ticker"],
                        "description": f"{mh['title']} bid {bid_hi:.2f} > {ml['title']} ask {ask_lo:.2f}: "
                                       f"buy YES {ml['title']} + NO {mh['title']}",
                        "legs": [{"ticker": ml["ticker"], "side": "yes", "cost": cost_lo},
                                 {"ticker": mh["ticker"], "side": "no", "cost": cost_hi}],
                        "profit_floor": round(1 - cost_lo - cost_hi, 4),
                        "fee_est": round(kalshi_fee(cost_lo) + kalshi_fee(cost_hi), 4),
                        "type": "nested",
                    })
                else:
                    pl, ph = mid(ml), mid(mh)
                    if pl is not None and ph is not None and ph > pl + 0.005:
                        soft.append({"event_ticker": event_ticker or ml["event_ticker"],
                                     "description": f"non-monotone ladder: {mh['title']} mid {ph:.3f} > {ml['title']} mid {pl:.3f}",
                                     "tickers": [ml["ticker"], mh["ticker"]]})
    # disjoint margin events
    h_items, a_items = lad.get(f"MARGIN:{home}", []), lad.get(f"MARGIN:{away}", [])
    for xh, mh in h_items:
        for xa, ma in a_items:
            bh, ba = mh.get("yes_bid"), ma.get("yes_bid")
            if bh and ba and bh + ba > 1 + 1e-9:
                ch, ca = round(1 - bh, 4), round(1 - ba, 4)
                arbs.append({
                    "event_ticker": event_ticker or mh["event_ticker"],
                    "description": f"{mh['title']} bid {bh:.2f} + {ma['title']} bid {ba:.2f} > $1 (can't both win): buy NO on both",
                    "legs": [{"ticker": mh["ticker"], "side": "no", "cost": ch},
                             {"ticker": ma["ticker"], "side": "no", "cost": ca}],
                    "profit_floor": round(1 - ch - ca, 4),
                    "fee_est": round(kalshi_fee(ch) + kalshi_fee(ca), 4),
                    "type": "disjoint",
                })
    ml_h = next((m for x, m in h_items if m["family"] == "ML"), None)
    ml_a = next((m for x, m in a_items if m["family"] == "ML"), None)
    if ml_h and ml_a and ml_h.get("yes_ask") and ml_a.get("yes_ask"):
        s = ml_h["yes_ask"] + ml_a["yes_ask"]
        if s < 1 - 1e-9:
            arbs.append({
                "event_ticker": event_ticker or ml_h["event_ticker"],
                "description": f"both moneylines ask {s:.2f} < $1: buy YES on both (loses only on a tie)",
                "legs": [{"ticker": ml_h["ticker"], "side": "yes", "cost": ml_h["yes_ask"]},
                         {"ticker": ml_a["ticker"], "side": "yes", "cost": ml_a["yes_ask"]}],
                "profit_floor": round(1 - s, 4),
                "fee_est": round(kalshi_fee(ml_h["yes_ask"]) + kalshi_fee(ml_a["yes_ask"]), 4),
                "type": "moneyline_pair",
            })
    arbs.sort(key=lambda a: -a["profit_floor"])
    return arbs, soft


# -- live loading ----------------------------------------------------------------------------

EVENT_TTL = 20.0
_event_cache: dict[str, tuple[float, "EventData"]] = {}
_event_lock = threading.Lock()
_key_locks: dict[str, threading.Lock] = {}


@dataclass
class EventData:
    suffix: str
    event_ticker: str           # KXNFLGAME-{suffix}
    away: str
    home: str
    matchup: str
    markets: list[dict]         # normalized: game + team + player props
    model: GameModel
    nflverse: dict
    as_of: float
    kickoff_ts: float | None = None

    @property
    def in_play(self) -> bool:
        """Kickoff has passed: the pregame model no longer applies."""
        return self.kickoff_ts is not None and time.time() >= self.kickoff_ts

    def by_ticker(self) -> dict[str, dict]:
        return {m["ticker"]: m for m in self.markets}


def _index():
    from kalshi_markets import MarketIndex
    return MarketIndex()


def current_games() -> list[dict]:
    return _index().get_current_week_games()


def _nflverse_lines(away: str, home: str) -> dict:
    try:
        import nflverse_data as nd
        a, h = nd.kalshi_to_nflverse_team(away), nd.kalshi_to_nflverse_team(home)
        sched = nd.load_schedules()
        rows = sched[(sched["away_team"] == a) & (sched["home_team"] == h) & sched["result"].isna()]
        if rows.empty:
            return {}
        r = rows.sort_values("gameday").iloc[0]
        return {k: (None if _isnan(r.get(k)) else float(r.get(k))) if k in ("spread_line", "total_line",
                                                                              "home_moneyline", "away_moneyline")
                else r.get(k) for k in ("spread_line", "total_line", "home_moneyline", "away_moneyline",
                                        "gameday", "gametime", "roof", "week", "game_id")}
    except Exception:
        return {}


def _clean_lines(nv: dict) -> dict:
    if nv.get("week") is not None:
        nv["week"] = int(nv["week"])
    return nv


def load_event(event: str, with_props: bool = True, force: bool = False) -> EventData:
    """All Kalshi game + prop markets for one game, normalized, with the fitted
    game model. Cached EVENT_TTL seconds (same as Kalshi's GET cache)."""
    suffix = game_suffix(event)
    key = f"{suffix}|{int(with_props)}"
    now = time.time()
    with _event_lock:
        hit = _event_cache.get(key)
        if hit and not force and now - hit[0] < EVENT_TTL:
            return hit[1]
        lock = _key_locks.setdefault(key, threading.Lock())
    with lock:
        hit = _event_cache.get(key)
        if hit and not force and time.time() - hit[0] < EVENT_TTL:
            return hit[1]
        data = _load_event_uncached(suffix, with_props)
        with _event_lock:
            _event_cache[key] = (time.time(), data)
        return data


def _load_event_uncached(suffix: str, with_props: bool) -> EventData:
    from concurrent.futures import ThreadPoolExecutor

    from kalshi_markets import TEAM_GAME_SERIES, WEEKLY_PLAYER_PROP_SERIES

    idx = _index()
    game_ev = f"KXNFLGAME-{suffix}"
    games = {g["event_ticker"]: g for g in idx.get_week_games()}
    g = games.get(game_ev)
    if g is None:
        raise ValueError(f"unknown or closed game event: {game_ev}")
    away, home = g["away"], g["home"]
    series = list(TEAM_GAME_SERIES) + (list(WEEKLY_PLAYER_PROP_SERIES) if with_props else [])

    def fetch(s):
        try:
            return s, idx.get_event_markets(f"{s}-{suffix}")
        except Exception:
            return s, []

    with ThreadPoolExecutor(max_workers=8) as pool:
        raw = dict(pool.map(fetch, series))
    pids = list({(m.get("custom_strike") or {}).get("football_player")
                 for ms in raw.values() for m in ms} - {None})
    names = {}
    if pids:
        try:
            resolved = idx.resolve_targets(pids)
            names = {pid: (t or {}).get("name") for pid, t in resolved.items()}
        except Exception:
            names = {}
    markets = []
    for s, ms in raw.items():
        for m in ms:
            if m.get("status") not in (None, "active", "open"):
                continue
            pid = (m.get("custom_strike") or {}).get("football_player")
            markets.append(normalize_market(m, player_name=names.get(pid), teams=(away, home)))
    nv = _clean_lines(_nflverse_lines(away, home))
    model = fit_game(markets, away, home, nv.get("spread_line"), nv.get("total_line"))
    return EventData(suffix=suffix, event_ticker=game_ev, away=away, home=home, matchup=f"{away} @ {home}",
                     markets=markets, model=model, nflverse=nv, as_of=time.time(), kickoff_ts=_kickoff_ts(nv))


def _kickoff_ts(nv: dict) -> float | None:
    """nflverse gameday + gametime are US/Eastern."""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        dt = datetime.strptime(f"{nv['gameday']} {nv.get('gametime') or '13:00'}", "%Y-%m-%d %H:%M")
        return dt.replace(tzinfo=ZoneInfo("America/New_York")).timestamp()
    except Exception:
        return None
