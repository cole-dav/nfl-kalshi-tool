"""
Player prop projections -> a full distribution per (player, stat), so every
Kalshi strike ("N+" = stat > N - 0.5) gets P(yes) from one consistent shape.

Projection (per player, per stat):
    recency-weighted baseline from nflverse game logs (this season + last,
    exponential decay by games-ago, last season down-weighted)
  x opponent defense   (EPA/dropback or EPA/rush allowed, regressed to last season)
  x implied team points from the fair_value game model vs the team's usual output
  x opponent pace      (plays/game the defense allows vs league)
  x game script        (team_tendencies pass_leading / pass_trailing vs neutral,
                        weighted by how likely the team is to be up/down 8+)
  x weather            (wind > 15 mph cuts passing)
  x injury status      (Out / Doubtful / Questionable)

Distributions:
    yards (pass/rush/rec, longest rec/rush): lognormal with the player's own
        coefficient of variation (shrunk to a position prior), plus a point
        mass at 0 for receiving yards (games with no catch)
    counts (receptions, completions, attempts, carries): negative binomial
    TDs / INTs: Poisson; anytime TD uses TD share x implied team TDs
        (drives/game x TD-drive rate, offense vs defense allowed)

Market blend: the same distribution family is also fitted to the Kalshi
ladder mids (location only). The final location is a geometric blend,
model weight <= MODEL_WEIGHT scaled by sample quality, so edges reflect where
the model disagrees without letting a thin early-season sample run wild.
"""

from __future__ import annotations

import math
import threading
import time
import traceback
from dataclasses import dataclass, field

import numpy as np

import fair_value as fv
import nflverse_data as nd

MODEL_WEIGHT = 0.35
DECAY = 0.85            # weight per game back
PRIOR_SEASON_W = 0.6    # extra multiplier on last season's games

STAT_COLS = {
    "PASSYDS": "passing_yards", "PASSTDS": "passing_tds", "PASSCOMP": "completions", "PASSATT": "attempts",
    "PASSINT": "passing_interceptions", "RSHYDS": "rushing_yards", "RSHATT": "carries", "REC": "receptions",
    "RECYDS": "receiving_yards", "TD": "_tds", "LONGREC": "_longrec", "LONGRSH": "_longrsh",
}
YARDS = {"PASSYDS", "RSHYDS", "RECYDS", "LONGREC", "LONGRSH"}
COUNTS_NB = {"REC", "PASSCOMP", "PASSATT", "RSHATT"}
POISSON = {"PASSTDS", "PASSINT", "TD"}
PASS_FAMS = {"PASSYDS", "PASSTDS", "PASSCOMP", "PASSATT", "RECYDS", "REC", "LONGREC"}
RUSH_FAMS = {"RSHYDS", "RSHATT", "LONGRSH"}

CV_PRIOR = {"PASSYDS": 0.30, "RSHYDS": 0.55, "RECYDS": 0.65, "LONGREC": 0.55, "LONGRSH": 0.60}
NB_VAR_RATIO = {"REC": 1.15, "PASSCOMP": 1.35, "PASSATT": 1.45, "RSHATT": 1.35}


# -- distributions ----------------------------------------------------------------------


@dataclass
class StatDist:
    """One player-stat distribution. `kind` in {'lognormal','nb','poisson'}."""
    kind: str
    mean: float
    cv: float = 0.5            # lognormal
    p0: float = 0.0            # lognormal point mass at 0
    var_ratio: float = 1.0     # nb: var / mean
    _table: np.ndarray | None = field(default=None, repr=False)

    def with_mean(self, mean: float) -> "StatDist":
        return StatDist(self.kind, max(mean, 1e-3), self.cv, self.p0, self.var_ratio)

    # lognormal on the positive part, mean of the whole distribution = self.mean
    def _ln_params(self):
        m_pos = self.mean / max(1e-6, 1 - self.p0)
        s2 = math.log(1 + self.cv ** 2)
        return math.log(max(m_pos, 1e-6)) - s2 / 2, math.sqrt(s2)

    def _cdf_table(self) -> np.ndarray:
        if self._table is None:
            self._table = _count_cdf(self.mean, self.var_ratio if self.kind == "nb" else 1.0)
        return self._table

    def sf(self, x: float) -> float:
        """P(X > x)."""
        if self.kind == "lognormal":
            if x < 0:
                return 1.0
            mu, s = self._ln_params()
            return float((1 - self.p0) * (1 - fv.norm_cdf((math.log(max(x, 1e-9)) - mu) / s)))
        cdf = self._cdf_table()
        k = int(math.floor(x))
        if k < 0:
            return 1.0
        if k >= len(cdf):
            return 0.0
        return float(1 - cdf[k])

    def ppf(self, u: np.ndarray) -> np.ndarray:
        u = np.asarray(u, dtype=float)
        if self.kind == "lognormal":
            mu, s = self._ln_params()
            out = np.zeros_like(u)
            pos = u > self.p0
            q = (u[pos] - self.p0) / max(1e-9, 1 - self.p0)
            out[pos] = np.exp(mu + s * fv.norm_ppf(q))
            return out
        cdf = self._cdf_table()
        return np.searchsorted(cdf, u, side="left").astype(float)


def _count_cdf(mean: float, var_ratio: float = 1.0) -> np.ndarray:
    """CDF table of a negative binomial (var = mean * var_ratio) or Poisson
    (var_ratio <= 1.02), built by the pmf ratio recursion in log space."""
    m = max(mean, 1e-6)
    k_max = int(m + 12 * math.sqrt(m * max(var_ratio, 1)) + 20)
    k = np.arange(1, k_max + 1, dtype=float)
    if var_ratio > 1.02:
        p = 1.0 / var_ratio
        r = m * p / (1 - p)
        log0 = r * math.log(p)
        steps = np.log((k - 1 + r) / k) + math.log(1 - p)
    else:
        log0 = -m
        steps = np.log(m / k)
    logpmf = np.concatenate([[log0], log0 + np.cumsum(steps)])
    pmf = np.exp(logpmf - logpmf.max())
    return np.cumsum(pmf / pmf.sum())


def base_dist(family: str, mean: float, cv: float | None = None, p0: float = 0.0,
              var_ratio: float | None = None) -> StatDist:
    if family in YARDS:
        return StatDist("lognormal", max(mean, 1e-3), cv=cv or CV_PRIOR.get(family, 0.6), p0=p0)
    if family in COUNTS_NB:
        return StatDist("nb", max(mean, 1e-3), var_ratio=var_ratio or NB_VAR_RATIO.get(family, 1.2))
    return StatDist("poisson", max(mean, 1e-3))


def _ln_sf_grid(means: np.ndarray, cvs: np.ndarray, p0: float, xs: np.ndarray) -> np.ndarray:
    """P(X > x) for lognormal+zero-mass over a (mean, cv) grid: shape (M, C, K)."""
    M, C = np.meshgrid(means, cvs, indexing="ij")
    s2 = np.log(1 + C ** 2)
    mu = np.log(np.maximum(M / max(1e-6, 1 - p0), 1e-9)) - s2 / 2
    lx = np.log(np.maximum(xs, 1e-9))
    z = (lx[None, None, :] - mu[..., None]) / np.sqrt(s2)[..., None]
    out = (1 - p0) * (1 - fv.norm_cdf(z))
    return np.where(xs[None, None, :] < 0, 1.0, out)


def fit_market(dist: StatDist, ladder: list[tuple[float, float, float]]) -> tuple[float | None, float | None]:
    """Fit the distribution to ladder mids: location always, and for yards
    (lognormal) with >= 3 usable strikes also the CV. Returns (mean, cv)."""
    if not ladder:
        return None, None
    xs = np.array([x for x, _, _ in ladder], dtype=float)
    ps = np.array([p for _, p, _ in ladder], dtype=float)
    ws = np.array([w for _, _, w in ladder], dtype=float)
    if ws.sum() <= 0:
        return None, None
    ws = ws / ws.sum()
    x50 = xs[int(np.argmin(np.abs(ps - 0.5)))]
    base = max(x50 + 0.5, 0.05)
    scales = np.exp(np.linspace(math.log(0.1), math.log(5.0), 161))
    if dist.kind == "lognormal":
        cvs = np.arange(0.25, 1.21, 0.05) if len(ladder) >= 3 else np.array([dist.cv])
        pen = 0.0005 * ((cvs - dist.cv) / 0.2) ** 2  # soft pull toward the player's own CV
        grid = _ln_sf_grid(base * scales, cvs, dist.p0, xs)
        loss = (((grid - ps) ** 2) * ws).sum(axis=2) + pen[None, :]
        i, j = np.unravel_index(int(np.argmin(loss)), loss.shape)
        m0, cv = base * scales[i], float(cvs[j])
        fine = m0 * np.linspace(0.97, 1.03, 25)
        g2 = _ln_sf_grid(fine, np.array([cv]), dist.p0, xs)
        l2 = (((g2 - ps) ** 2) * ws).sum(axis=2)[:, 0]
        return float(fine[int(np.argmin(l2))]), cv
    best, best_loss = None, None
    for mm in list(base * scales):
        cdf = _count_cdf(mm, dist.var_ratio if dist.kind == "nb" else 1.0)
        ks = np.floor(xs).astype(int)
        sf = np.where(ks < 0, 1.0, np.where(ks >= len(cdf), 0.0, 1 - cdf[np.clip(ks, 0, len(cdf) - 1)]))
        loss = float((((sf - ps) ** 2) * ws).sum())
        if best_loss is None or loss < best_loss:
            best, best_loss = mm, loss
    return float(best), None


def fit_market_mean(dist: StatDist, ladder: list[tuple[float, float, float]]) -> float | None:
    return fit_market(dist, ladder)[0]


# -- data -----------------------------------------------------------------------------


def _nd():
    return nd


def _tt():
    import team_tendencies as tt
    return tt


def _norm_name(n: str) -> str:
    return _nd()._normalize_name(n or "")


@dataclass
class PlayerProp:
    player_id: str
    player_name: str | None
    team: str | None
    family: str
    dist: StatDist
    model_mean: float | None
    market_mean: float | None
    quality: float
    reasons: list[str]
    position: str | None = None
    gsis_id: str | None = None

    def fair(self, strike: float) -> float:
        return self.dist.sf(strike)


@nd.memo_on_data("pbp")
def _long_plays(season: int | None = None):
    """Per player-game longest reception / rush, from play-by-play."""
    pbp = nd.load_pbp(season)
    rec = pbp[(pbp["complete_pass"] == 1) & pbp["receiver_player_id"].notna()]
    rec = rec.groupby(["game_id", "receiver_player_id"])["yards_gained"].max().reset_index()
    rec.columns = ["game_id", "player_id", "_longrec"]
    rsh = pbp[(pbp["play_type"] == "run") & pbp["rusher_player_id"].notna()]
    rsh = rsh.groupby(["game_id", "rusher_player_id"])["yards_gained"].max().reset_index()
    rsh.columns = ["game_id", "player_id", "_longrsh"]
    return rec.merge(rsh, on=["game_id", "player_id"], how="outer")


_hist_cache: dict[str, tuple[float, object]] = {}


def player_history(gsis_id: str):
    """This season + last season game rows (REG+POST), oldest first, with
    derived _tds/_longrec/_longrsh columns and a recency weight `w`."""
    import pandas as pd
    nd = _nd()
    hit = _hist_cache.get(gsis_id)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    cur = nd.current_season()
    frames = []
    for s in (cur - 1, cur):
        try:
            ps = nd.load_player_stats(s)
        except Exception:
            continue
        rows = ps[ps["player_id"] == gsis_id].copy()
        if rows.empty:
            continue
        try:
            lp = _long_plays(s)
            rows = rows.merge(lp, on=["game_id", "player_id"], how="left")
        except Exception:
            rows["_longrec"] = np.nan
            rows["_longrsh"] = np.nan
        frames.append(rows)
    if not frames:
        _hist_cache[gsis_id] = (time.time(), None)
        return None
    h = pd.concat(frames, ignore_index=True).sort_values(["season", "week"]).reset_index(drop=True)
    h["_tds"] = h["rushing_tds"].fillna(0) + h["receiving_tds"].fillna(0)
    # no catch / no carry that game -> longest = 0 (the market resolves NO)
    h.loc[h["receptions"].fillna(0) == 0, "_longrec"] = 0.0
    h.loc[h["carries"].fillna(0) == 0, "_longrsh"] = 0.0
    n = len(h)
    ago = np.arange(n)[::-1]
    w = DECAY ** ago
    w = np.where(h["season"].to_numpy() < cur, w * PRIOR_SEASON_W, w)
    h["w"] = w
    _hist_cache[gsis_id] = (time.time(), h)
    return h


def _wstats(vals: np.ndarray, w: np.ndarray) -> tuple[float, float]:
    ok = ~np.isnan(vals)
    vals, w = vals[ok], w[ok]
    if len(vals) == 0 or w.sum() <= 0:
        return float("nan"), float("nan")
    m = float((vals * w).sum() / w.sum())
    v = float((w * (vals - m) ** 2).sum() / w.sum())
    return m, math.sqrt(max(v, 0))


def _team_game_totals(team: str, game_ids: list[str]) -> dict[str, dict]:
    """Team carries+targets and offensive TDs per game (for shares)."""
    nd = _nd()
    out = {}
    cur = nd.current_season()
    for s in (cur - 1, cur):
        try:
            ps = nd.load_player_stats(s)
        except Exception:
            continue
        sub = ps[ps["game_id"].isin(game_ids)]
        for gid, g in sub.groupby("game_id"):
            for tm, gg in g.groupby("team"):
                out[(gid, tm)] = {
                    "opps": float(gg["carries"].fillna(0).sum() + gg["targets"].fillna(0).sum()),
                    "tds": float(gg["rushing_tds"].fillna(0).sum() + gg["receiving_tds"].fillna(0).sum()),
                    "targets": float(gg["targets"].fillna(0).sum()),
                }
    return out


# -- context multipliers --------------------------------------------------------------


def _tend(team_nv: str) -> dict | None:
    try:
        return _tt().team_tendencies(team_nv)
    except Exception:
        return None


def _bv(side: dict, key: str):
    m = (side or {}).get(key) or {}
    v = m.get("blend", m.get("value"))
    return v, m.get("lg")


def _def_sd(col: str) -> float | None:
    try:
        t = _tt().defense_table()
        v = float(t[col].std())
        return v if v > 0 else None
    except Exception:
        return None


@dataclass
class GameContext:
    team_mult: dict = field(default_factory=dict)       # (team, family) -> (mult, [reasons])
    team_td_exp: dict = field(default_factory=dict)      # team -> expected offensive TDs
    injuries: dict = field(default_factory=dict)         # normalized name -> status
    wind_mph: float | None = None
    weather_note: str | None = None


_weather_cache: dict[str, tuple[float, dict]] = {}


def _weather(ev) -> dict | None:
    hit = _weather_cache.get(ev.suffix)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    res = None
    try:
        import weather as wx
        nd = _nd()
        st = nd.load_stadiums().get(ev.home, {})
        gd = ev.nflverse.get("gameday")
        if st and gd:
            roof = ev.nflverse.get("roof") or st.get("roof")
            roof = {"outdoors": "outdoor", "dome": "fixed_dome"}.get(roof, roof)
            res = wx.get_game_weather(st["lat"], st["lon"], str(gd), ev.nflverse.get("gametime"), roof)
    except Exception:
        res = None
    _weather_cache[ev.suffix] = (time.time(), res)
    return res


def game_context(ev) -> GameContext:
    nd = _nd()
    ctx = GameContext()
    model = ev.model
    implied = model.implied_points()
    wx = _weather(ev)
    if wx and wx.get("forecast_available"):
        ctx.wind_mph = wx.get("wind_speed_mph")
        ctx.weather_note = f"wind {ctx.wind_mph:.0f} mph, {wx.get('temperature_f') or 0:.0f}F" if ctx.wind_mph is not None else None
    sd_pass, sd_rush = _def_sd("epa_dropback"), _def_sd("epa_rush")
    for team, opp in ((ev.home, ev.away), (ev.away, ev.home)):
        t_nv, o_nv = nd.kalshi_to_nflverse_team(team), nd.kalshi_to_nflverse_team(opp)
        tt_t, tt_o = _tend(t_nv), _tend(o_nv)
        off, dfn = (tt_t or {}).get("off", {}), (tt_o or {}).get("def", {})
        base_pts, _ = _bv(off, "pts")
        ip = implied.get(team)
        r = 1.0
        if base_pts and ip:
            r = min(1.4, max(0.7, ip / base_pts))
        # opponent defense
        dp, lgp = _bv(dfn, "epa_dropback")
        dr, lgr = _bv(dfn, "epa_rush")
        zp = (dp - lgp) / sd_pass if None not in (dp, lgp) and sd_pass else 0.0
        zr = (dr - lgr) / sd_rush if None not in (dr, lgr) and sd_rush else 0.0
        zp, zr = max(-2.5, min(2.5, zp)), max(-2.5, min(2.5, zr))
        # opponent pace (plays allowed per game vs league)
        ppg, lgppg = _bv(dfn, "plays_per_game")
        pace = (ppg / lgppg) if ppg and lgppg else 1.0
        pace = max(0.9, min(1.1, pace))
        # game script: P(up 8+) / P(down 8+) from the margin pmf, team perspective
        sign = 1 if team == model.home else -1
        p_lead = float(model.pmf_m[(sign * fv.MARGIN_SUPPORT) >= 8].sum())
        p_trail = float(model.pmf_m[(sign * fv.MARGIN_SUPPORT) <= -8].sum())
        pl, _ = _bv(off, "pass_leading")
        ptr, _ = _bv(off, "pass_trailing")
        pn, _ = _bv(off, "neutral_pass")
        script = 1.0
        if None not in (pl, ptr, pn) and pn:
            # half weight: the player's own history already includes typical scripts
            exp_pass = pn + 0.5 * (p_lead * (pl - pn) + p_trail * (ptr - pn))
            script = max(0.85, min(1.15, exp_pass / pn))
            # neutral-ish baseline game has p_lead ~ p_trail ~ 0.25; remove that average tilt
            base_tilt = pn + 0.5 * (0.25 * (pl - pn) + 0.25 * (ptr - pn))
            script = max(0.85, min(1.15, exp_pass / base_tilt)) if base_tilt else script
        wind = 1.0
        if ctx.wind_mph is not None and ctx.wind_mph > 15:
            wind = 0.93 if ctx.wind_mph <= 20 else 0.87
        for fam in STAT_COLS:
            reasons = []
            mult = 1.0
            if fam in PASS_FAMS:
                d = (0.05 if fam in YARDS or fam == "PASSTDS" else 0.025) * zp
                mult *= 1 + d
                if abs(zp) >= 0.5:
                    reasons.append(f"{opp} pass D {'soft' if zp > 0 else 'tough'} (EPA/db z {zp:+.1f})")
                mult *= script if fam in ("PASSATT", "PASSCOMP", "PASSYDS", "REC", "RECYDS") else 1.0
                if abs(script - 1) >= 0.02 and fam in ("PASSATT", "PASSYDS", "RECYDS", "REC"):
                    reasons.append(f"script: pass volume x{script:.2f} (P up 8+ {p_lead:.0%}, down 8+ {p_trail:.0%})")
                mult *= wind
                if wind < 1:
                    reasons.append(f"wind {ctx.wind_mph:.0f} mph cuts passing x{wind:.2f}")
            if fam in RUSH_FAMS:
                mult *= 1 + (0.05 if fam != "RSHATT" else 0.0) * zr
                if abs(zr) >= 0.5 and fam != "RSHATT":
                    reasons.append(f"{opp} run D {'soft' if zr > 0 else 'tough'} (EPA/rush z {zr:+.1f})")
                inv = 2 - script
                mult *= inv if fam in ("RSHATT", "RSHYDS") else 1.0
            if fam in ("PASSYDS", "RECYDS", "RSHYDS", "REC", "PASSCOMP", "PASSATT", "RSHATT"):
                mult *= r ** 0.35
            if fam in ("PASSTDS", "TD"):
                mult *= r
            if fam == "PASSINT":
                mult *= 1 + 0.03 * zp
            if fam not in ("PASSTDS", "TD", "PASSINT"):
                mult *= pace ** 0.7
            if abs(r - 1) >= 0.05 and fam in ("PASSYDS", "RECYDS", "RSHYDS", "PASSTDS", "TD"):
                reasons.append(f"implied {team} {ip:.1f} pts vs {base_pts:.1f} avg")
            ctx.team_mult[(team, fam)] = (mult, reasons)
        # expected offensive TDs: drives x TD-drive rate (offense vs defense allowed)
        drv, _ = _bv(off, "drives_per_game")
        tdo, lgtd = _bv(off, "td_drive")
        tdd, _ = _bv(dfn, "td_drive")
        if drv and tdo and tdd:
            ctx.team_td_exp[team] = drv * (tdo + tdd) / 2 * r
        elif ip:
            ctx.team_td_exp[team] = ip / 7.6
    try:
        for team in (ev.home, ev.away):
            inj = nd.team_injuries(nd.kalshi_to_nflverse_team(team))
            for row in inj.to_dict(orient="records"):
                if isinstance(row.get("report_status"), str) and row["report_status"]:
                    ctx.injuries[_norm_name(row["full_name"])] = row["report_status"]
    except Exception:
        pass
    return ctx


INJURY_MULT = {"Out": 0.02, "Doubtful": 0.3, "Questionable": 0.92}


# -- per-player projection ---------------------------------------------------------------


_roster_cache: dict = {}


def _roster_maps():
    hit = _roster_cache.get("m")
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    nd = _nd()
    r = nd.load_rosters()
    by_sr = {row.sportradar_id: row for row in r.itertuples() if isinstance(row.sportradar_id, str)}
    by_name = {}
    for row in r.itertuples():
        by_name.setdefault((_norm_name(row.full_name), nd.nflverse_to_kalshi_team(row.team)), row)
        by_name.setdefault((_norm_name(row.full_name), None), row)
    _roster_cache["m"] = (time.time(), (by_sr, by_name))
    return by_sr, by_name


def resolve_gsis(player_id: str, name: str | None, team: str | None) -> tuple[str | None, str | None]:
    """Kalshi structured-target UUID -> (gsis_id, position)."""
    try:
        by_sr, by_name = _roster_maps()
    except Exception:
        return None, None
    target = None
    try:
        from kalshi_markets import _load_json_cache
        target = _load_json_cache("structured_targets.json").get(player_id) if player_id else None
    except Exception:
        target = None
    if target:
        sids = [target.get("source_id"), (target.get("source_ids") or {}).get("source_3_id")]
        for sid in sids:
            row = by_sr.get(sid)
            if row is not None:
                return row.gsis_id, row.position
    if name:
        row = by_name.get((_norm_name(name), team)) or by_name.get((_norm_name(name), None))
        if row is not None:
            return row.gsis_id, row.position
    return None, None


def project(player_id: str, name: str | None, team: str | None, family: str, ctx: GameContext,
            ladder: list[tuple[float, float, float]]) -> PlayerProp:
    """Model mean + market-fitted mean -> blended StatDist for one player-stat."""
    reasons: list[str] = []
    gsis, pos = resolve_gsis(player_id, name, team)
    hist = player_history(gsis) if gsis else None
    col = STAT_COLS.get(family)
    model_mean, cv, p0, var_ratio, quality = None, None, 0.0, None, 0.0
    if hist is not None and col in hist.columns:
        vals = hist[col].to_numpy(dtype=float)
        w = hist["w"].to_numpy(dtype=float)
        ok = ~np.isnan(vals)
        n = int(ok.sum())
        if n >= 1:
            m, sd = _wstats(vals, w)
            n_eff = float(w[ok].sum() ** 2 / (w[ok] ** 2).sum())
            quality = min(1.0, n_eff / 6)
            mult, mreasons = ctx.team_mult.get((team, family), (1.0, []))
            model_mean = m * mult
            reasons.append(f"baseline {m:.1f}/g over {n} g (recency-weighted) x{mult:.2f} context")
            reasons += mreasons
            if family in YARDS and m > 0:
                pos_vals = vals[ok & (vals > 0)]
                cv_emp = (sd / m) if m > 0 else CV_PRIOR.get(family, 0.6)
                cv = (n_eff * cv_emp + 4 * CV_PRIOR.get(family, 0.6)) / (n_eff + 4)
                cv = max(0.2, min(1.2, cv))
                if family in ("RECYDS", "LONGREC", "RSHYDS", "LONGRSH"):
                    zero = float(((vals[ok] <= 0) * w[ok]).sum() / w[ok].sum())
                    p0 = (n_eff * zero + 3 * (0.06 if family in ("RECYDS", "LONGREC") else 0.02)) / (n_eff + 3)
                    p0 = min(0.5, p0)
                    cv = max(0.2, min(1.2, cv * 0.85))  # zero games are carried by p0
                del pos_vals
            if family in COUNTS_NB and m > 0:
                vr = (sd ** 2 / m) if m > 0 else NB_VAR_RATIO[family]
                var_ratio = max(1.03, (n_eff * vr + 5 * NB_VAR_RATIO[family]) / (n_eff + 5))
            if family == "TD" and team:
                # TD share x implied team TDs, share shrunk toward opportunity share
                gids = hist["game_id"].tolist()
                tg = _team_game_totals(hist["team"].iloc[-1], gids)
                team_tds = np.array([tg.get((g, t), {}).get("tds", np.nan) for g, t in zip(gids, hist["team"])])
                team_opps = np.array([tg.get((g, t), {}).get("opps", np.nan) for g, t in zip(gids, hist["team"])])
                opps = hist["carries"].fillna(0).to_numpy(float) + hist["targets"].fillna(0).to_numpy(float)
                okk = ~np.isnan(team_tds) & ~np.isnan(team_opps) & (team_opps > 0)
                if okk.any():
                    ww = w[okk]
                    td_n = float((vals[okk] * ww).sum())
                    tt_n = float((team_tds[okk] * ww).sum())
                    opp_share = float((opps[okk] * ww).sum() / (team_opps[okk] * ww).sum())
                    pos_f = {"RB": 1.15, "WR": 0.9, "TE": 0.95, "QB": 0.35}.get(pos or "", 0.9)
                    k = 4.0 * ww.sum() / max(1, okk.sum())
                    share = (td_n + k * opp_share * pos_f) / (tt_n + k) if tt_n + k > 0 else opp_share * pos_f
                    lam_team = ctx.team_td_exp.get(team)
                    if lam_team:
                        model_mean = share * lam_team
                        reasons.append(f"TD share {share:.0%} x {lam_team:.2f} implied {team} TDs")
    if name and ctx.injuries.get(_norm_name(name)):
        st = ctx.injuries[_norm_name(name)]
        im = INJURY_MULT.get(st, 1.0)
        if model_mean is not None:
            model_mean *= im
        reasons.append(f"injury report: {st}" + (f" (x{im:.2f})" if im != 1 else ""))
        if st in ("Out", "Doubtful"):
            quality = max(quality, 0.8)
    base = base_dist(family, model_mean if model_mean else 1.0, cv, p0, var_ratio)
    market_mean, market_cv = fit_market(base, ladder)
    if market_cv is not None and base.kind == "lognormal":
        own_cv = base.cv
        base = StatDist("lognormal", base.mean, cv=MODEL_WEIGHT * quality * own_cv + (1 - MODEL_WEIGHT * quality) * market_cv,
                        p0=base.p0)
    if model_mean is None and market_mean is None:
        final = base
        quality = 0.0
    elif model_mean is None:
        final = base.with_mean(market_mean)
        reasons.append("no nflverse history: market-implied distribution only")
    elif market_mean is None:
        final = base.with_mean(model_mean)
    else:
        wm = MODEL_WEIGHT * quality
        blended = math.exp(wm * math.log(max(model_mean, 1e-3)) + (1 - wm) * math.log(max(market_mean, 1e-3)))
        final = base.with_mean(blended)
        reasons.append(f"model mean {model_mean:.2f} vs Kalshi-implied {market_mean:.2f} -> {blended:.2f} (model wt {wm:.2f})")
    return PlayerProp(player_id=player_id, player_name=name, team=team, family=family, dist=final,
                      model_mean=model_mean, market_mean=market_mean, quality=quality, reasons=reasons,
                      position=pos, gsis_id=gsis)


# -- event-level API --------------------------------------------------------------------------

PROPS_TTL = 120.0
_props_cache: dict[str, tuple[float, float, dict]] = {}
_props_lock = threading.Lock()


def event_props(ev) -> dict[tuple[str, str], PlayerProp]:
    """{(player_id, family): PlayerProp} for every player prop ladder in the event.
    Cached per event for PROPS_TTL, refit if the event's quotes were reloaded."""
    with _props_lock:
        hit = _props_cache.get(ev.suffix)
        if hit and time.time() - hit[0] < PROPS_TTL and hit[1] == ev.as_of:
            return hit[2]
    try:
        ctx = game_context(ev)
    except Exception:
        traceback.print_exc()
        ctx = GameContext()
    groups: dict[tuple[str, str], list[dict]] = {}
    for m in ev.markets:
        if m["kind"] == "player" and m.get("player_id") and m.get("threshold") is not None:
            groups.setdefault((m["player_id"], m["family"]), []).append(m)
    out = {}
    for (pid, fam), ms in groups.items():
        ladder = []
        for m in ms:
            p, w = fv.mid(m), fv._obs_weight(m)
            if p is not None and w > 0:
                ladder.append((m["threshold"], p, w))
        try:
            out[(pid, fam)] = project(pid, ms[0].get("player_name"), ms[0].get("team_code"), fam, ctx, ladder)
        except Exception as e:
            traceback.print_exc()
            base = base_dist(fam, 1.0)
            mm = fit_market_mean(base, ladder)
            out[(pid, fam)] = PlayerProp(pid, ms[0].get("player_name"), ms[0].get("team_code"), fam,
                                         base.with_mean(mm or 1.0), None, mm, 0.0, [f"model error: {e}"])
    with _props_lock:
        _props_cache[ev.suffix] = (time.time(), ev.as_of, out)
    return out


def fair_for_prop(ev, m: dict) -> float | None:
    pp = event_props(ev).get((m.get("player_id"), m["family"]))
    if pp is None or m.get("threshold") is None:
        return None
    return pp.fair(m["threshold"])


def prop_edges(ev, min_edge: float = 0.03) -> list[dict]:
    props = event_props(ev)
    out = []
    for m in ev.markets:
        if m["kind"] != "player":
            continue
        pp = props.get((m.get("player_id"), m["family"]))
        if pp is None or m.get("threshold") is None:
            continue
        fair = pp.fair(m["threshold"])
        mp = fv.mid(m)
        b, a = m.get("yes_bid"), m.get("yes_ask")
        if pp.quality < 0.5 and pp.market_mean is None:
            continue  # neither a usable model nor a usable market ladder
        if mp is not None and abs(fair - mp) > 0.25:
            continue  # model and market disagree wildly: likely a role/lineup change the model can't see
        conf = fv.confidence(m, None, pp.quality)
        if not b or not a or a - b > 0.2:
            conf = "low"
        out += fv.edge_rows(m, fair, ev.matchup, min_edge, conf, pp.reasons[:5])
    return out
