"""
Per-player rankings for the game-view rosters.

Offense: FantasyPros weekly expert consensus rank (ECR), via nflverse's
DynastyProcess mirror.

Defense: a Defensive Power Index (DPI) built here from free nflverse data --
not a fantasy score, a play-quality grade. Each defender is graded against
his own role (IDL / EDGE / LB / CB / S) on per-snap production and coverage
results:

  * PFR advanced defense: pressures, sacks, QB hits, coverage targets,
    completions / yards / TDs / INTs allowed, tackles, missed tackles
  * nflverse player stats: tackles for loss, passes defended
  * snap counts: defensive snaps and snap share

The current season is blended with PRIOR_WEIGHT x last season's counts so a
few weeks of data doesn't swing grades wildly; last season fades out as this
season's snaps pile up. Noisy rate stats (coverage per target, missed tackle
rate) are shrunk toward the role average. Each metric becomes a percentile
within the role, the weighted percentiles form a composite, and DPI is the
composite's own percentile: 90 = better than 90% of qualified players at
that role.
"""

from __future__ import annotations

import re
import unicodedata

import numpy as np
import pandas as pd

import nflverse_data as nd

PRIOR_WEIGHT = 0.35
MIN_SNAPS = 100  # blended defensive snaps to be ranked
COVERAGE_PRIOR_TARGETS = 20
TACKLE_PRIOR_ATTEMPTS = 15
COMPOSITE_PRIOR_SNAPS = 150

# metric -> weight per role; metrics prefixed "-" are better when lower.
ROLE_WEIGHTS = {
    "EDGE": {"prss_rate": .35, "sk_rate": .15, "hit_rate": .10, "tfl_rate": .15, "tkl_rate": .05, "-mtkl_pct": .05, "snap_pct": .15},
    "IDL":  {"prss_rate": .30, "sk_rate": .10, "tfl_rate": .20, "tkl_rate": .15, "-mtkl_pct": .10, "snap_pct": .15},
    "LB":   {"tkl_rate": .20, "tfl_rate": .10, "-mtkl_pct": .15, "prss_rate": .10, "-yds_tgt": .15, "-rating": .10, "plays_rate": .05, "snap_pct": .15},
    "CB":   {"-rating": .25, "-yds_tgt": .20, "plays_rate": .20, "-cmp_pct": .10, "-mtkl_pct": .05, "snap_pct": .20},
    "S":    {"-rating": .20, "-yds_tgt": .15, "plays_rate": .15, "tkl_rate": .10, "-mtkl_pct": .15, "snap_pct": .25},
}
METRIC_LABELS = {
    "prss_rate": "pressure rate", "sk_rate": "sack rate", "hit_rate": "QB hit rate", "tfl_rate": "TFL rate",
    "tkl_rate": "tackle rate", "mtkl_pct": "missed tackle %", "snap_pct": "snap share",
    "yds_tgt": "yds/target allowed", "rating": "passer rtg allowed", "plays_rate": "INT+PD rate",
    "cmp_pct": "comp % allowed",
}
# OLBs rush the passer in 3-4 fronts and drop into coverage in 4-3s; above
# this pressures-per-snap they're graded as EDGE, below it as LB.
OLB_EDGE_PRESSURE_RATE = 0.035

def _season_counts(season: int) -> pd.DataFrame:
    """Regular-season defensive counting stats per pfr_id."""
    pfr = nd.load_pfr_def_week(season)
    pfr = pfr[pfr["game_type"] == "REG"]
    pfr_tot = pfr.groupby("pfr_player_id").agg(
        tgt=("def_targets", "sum"), cmp=("def_completions_allowed", "sum"), yds=("def_yards_allowed", "sum"),
        td=("def_receiving_td_allowed", "sum"), ints=("def_ints", "sum"), prss=("def_pressures", "sum"),
        sk=("def_sacks", "sum"), hit=("def_times_hitqb", "sum"), tkl=("def_tackles_combined", "sum"),
        mtkl=("def_missed_tackles", "sum"),
    )

    snaps = nd.load_snap_counts(season)
    snaps = snaps[(snaps["game_type"] == "REG") & (snaps["defense_snaps"] > 0)]
    snap_tot = snaps.groupby("pfr_player_id").agg(
        snaps=("defense_snaps", "sum"), pct_sum=("defense_pct", "sum"), games=("defense_pct", "count"),
    )

    ps = nd.load_player_stats(season)
    ps = ps[ps["season_type"] == "REG"]
    box = ps.groupby("player_id").agg(tfl=("def_tackles_for_loss", "sum"), pd_=("def_pass_defended", "sum"))
    ids = nd.load_rosters(season)[["gsis_id", "pfr_id"]].dropna().drop_duplicates("pfr_id").set_index("gsis_id")
    box = box.join(ids, how="inner").set_index("pfr_id")

    return snap_tot.join(pfr_tot, how="left").join(box, how="left").fillna(0.0)


def _passer_rating(cmp, att, yds, td, ints):
    att = np.maximum(att, 1e-9)
    clip = lambda x: np.clip(x, 0, 2.375)
    a = clip((cmp / att - 0.3) * 5)
    b = clip((yds / att - 3) * 0.25)
    c = clip(td / att * 20)
    d = clip(2.375 - ints / att * 25)
    return (a + b + c + d) / 6 * 100


def _role(depth_pos: str | None, pos: str | None, prss_rate: float) -> str | None:
    p = depth_pos or ""
    if p in ("DT", "NT"):
        return "IDL"
    if p == "DE":
        return "EDGE"
    if p == "OLB":
        return "EDGE" if prss_rate >= OLB_EDGE_PRESSURE_RATE else "LB"
    if p in ("ILB", "MLB", "LB"):
        return "LB"
    if p == "CB":
        return "CB"
    if p in ("SS", "FS", "S"):
        return "S"
    return {"DL": "IDL", "LB": "LB", "DB": "CB"}.get(pos or "")


def _build_dpi(season: int) -> pd.DataFrame:
    cur = _season_counts(season)
    try:
        prev = _season_counts(season - 1)
    except Exception:
        prev = pd.DataFrame(columns=cur.columns)
    df = cur.add(prev * PRIOR_WEIGHT, fill_value=0.0)

    roster = nd.load_rosters(season)
    roster = roster[roster["status"] == "ACT"].dropna(subset=["pfr_id"]).drop_duplicates("pfr_id").set_index("pfr_id")
    df = df.join(roster[["depth_chart_position", "position"]], how="inner")

    s = df["snaps"].clip(lower=1)
    df["prss_rate"] = df["prss"] / s
    df["sk_rate"] = df["sk"] / s
    df["hit_rate"] = df["hit"] / s
    df["tfl_rate"] = df["tfl"] / s
    df["tkl_rate"] = df["tkl"] / s
    df["plays_rate"] = (df["ints"] + df["pd_"]) / s
    df["snap_pct"] = df["pct_sum"] / df["games"].clip(lower=1)
    df["role"] = [_role(r.depth_chart_position, r.position, r.prss_rate) for r in df.itertuples()]
    df = df[df["role"].notna()].copy()
    df["qualified"] = df["snaps"] >= MIN_SNAPS

    out = []
    for role, g in df.groupby("role"):
        g = g.copy()
        q = g[g["qualified"]]
        # Shrink small-sample coverage / tackling rates toward the role mean.
        tgt_mean = q["tgt"].sum() or 1
        k = COVERAGE_PRIOR_TARGETS
        avg = {c: q[c].sum() / tgt_mean for c in ("cmp", "yds", "td", "ints")}
        adj = {c: g[c] + k * avg[c] for c in avg}
        att = g["tgt"] + k
        g["cmp_pct"] = adj["cmp"] / att
        g["yds_tgt"] = adj["yds"] / att
        g["rating"] = _passer_rating(adj["cmp"], att, adj["yds"], adj["td"], adj["ints"])
        tk_att = q["tkl"].sum() + q["mtkl"].sum()
        mt_mean = q["mtkl"].sum() / tk_att if tk_att else 0.1
        g["mtkl_pct"] = (g["mtkl"] + TACKLE_PRIOR_ATTEMPTS * mt_mean) / (g["tkl"] + g["mtkl"] + TACKLE_PRIOR_ATTEMPTS)

        q = g[g["qualified"]]
        comp = np.zeros(len(g))
        pcts = {}
        for key, w in ROLE_WEIGHTS[role].items():
            col = key.lstrip("-")
            ref = np.sort(q[col].to_numpy())
            p = np.searchsorted(ref, g[col].to_numpy(), side="right") / max(len(ref), 1) * 100
            if key.startswith("-"):
                p = 100 - np.searchsorted(ref, g[col].to_numpy(), side="left") / max(len(ref), 1) * 100
            pcts[col] = p
            comp += w * p
        # Pull low-snap composites toward average so a hot 100-snap sample
        # doesn't outrank a full season of the same quality.
        g["composite"] = (comp * g["snaps"] + 50 * COMPOSITE_PRIOR_SNAPS) / (g["snaps"] + COMPOSITE_PRIOR_SNAPS)
        ref = np.sort(g.loc[g["qualified"], "composite"].to_numpy())
        g["dpi"] = np.round(np.searchsorted(ref, g["composite"].to_numpy(), side="right") / max(len(ref), 1) * 100).clip(1, 99)
        g["role_rank"] = g["composite"].where(g["qualified"]).rank(ascending=False, method="min")
        g["role_count"] = int(g["qualified"].sum())
        g["top_traits"] = [
            [METRIC_LABELS[m] for m, _ in sorted(((m, pcts[m][i]) for m in pcts if m != "snap_pct"), key=lambda x: -x[1])[:2]]
            for i in range(len(g))
        ]
        out.append(g)
    return pd.concat(out) if out else df


# Rebuilt only when a game day's data lands (or the roster refreshes); see
# the cache section of nflverse_data.
@nd.memo_on_data("pfr_def_week", "snap_counts", "player_stats", "rosters")
def _dpi_table(season: int | None = None) -> pd.DataFrame:
    return _build_dpi(season)


@nd.memo_on_data("ff_rankings_week")
def _ecr_table(season: int | None = None) -> pd.DataFrame:
    df = nd.load_ff_rankings_week(season)
    df = df[df["page"].isin(["qb", "ppr-rb", "ppr-wr", "ppr-te"])].copy()
    df["key"] = df["player_name"].map(_norm)
    df["team_nv"] = df["team"].map(nd.kalshi_to_nflverse_team)
    return df


def warm() -> None:
    for fn in (_dpi_table, _ecr_table):
        try:
            fn()
        except Exception as e:
            print(f"cache warm: rankings {fn.__name__} failed: {e}")


def _norm(name: str) -> str:
    s = unicodedata.normalize("NFD", str(name).lower())
    s = re.sub(r"[̀-ͯ.'\-]", "", s)
    s = re.sub(r"\s+(jr|sr|ii|iii|iv|v)$", "", s.strip())
    return re.sub(r"\s+", " ", s)


def attach_rankings(roster: list[dict], team: str) -> list[dict]:
    """Adds `rank` to each roster row: {"kind": "ecr"|"dpi", ...} or None."""
    try:
        ecr = _ecr_table()
        ecr_team = {r.key: r for r in ecr[ecr["team_nv"] == team].itertuples()}
    except Exception:
        ecr_team = {}
    try:
        dpi = _dpi_table()
    except Exception:
        dpi = pd.DataFrame()

    for row in roster:
        row["rank"] = None
        e = ecr_team.get(_norm(row["full_name"]))
        if e is not None:
            row["rank"] = {"kind": "ecr", "label": e.pos_rank, "ecr": round(float(e.ecr), 1),
                           "sd": round(float(e.sd), 1) if e.sd == e.sd else None,
                           "best": int(e.best), "worst": int(e.worst), "opp": e.player_opponent}
            continue
        pid = row.get("pfr_id")
        if pid and not dpi.empty and pid in dpi.index:
            d = dpi.loc[pid]
            ranked = bool(d["qualified"])
            row["rank"] = {
                "kind": "dpi", "role": d["role"], "dpi": int(d["dpi"]), "qualified": ranked,
                "label": f"{d['role']}{int(d['role_rank'])}" if ranked else d["role"],
                "role_count": int(d["role_count"]), "snaps": int(round(d["snaps"])),
                "traits": list(d["top_traits"]),
            }
    return roster
