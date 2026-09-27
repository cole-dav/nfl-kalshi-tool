"""
Depth-chart matchups for the game view: one team's offense against the
other's defense, starters paired with the defender they most often line up
across from, each with Madden OVR and the depth behind them.

Depth charts come from nflverse (ESPN daily snapshots, latest used). The
offense is the 11-personnel chart ("3WR 1TE"); the defense is the team's base
4-3 or 3-4 chart, including its nickel back. Alignment pairings are the
conventional ones (the left tackle faces the defense's right-side edge, the
X receiver the right corner, etc.) -- a feel for who's on the field together,
not a snap-by-snap charting.
"""

from __future__ import annotations

import nflreadpy as nfl
import pandas as pd

import madden_ratings as mr
import nflverse_data as nd
import player_rankings as prk

DEPTH_SHOWN = 3

# Offensive depth-chart slot key: (pos_abb, pos_slot) -> our label. WRs are
# split by slot: 1 = X (left), 2 = Z (right), 8 = slot receiver.
OFFENSE_SLOTS = [
    ("QB", "QB", None), ("RB", "RB", None), ("WR1", "WR", 1), ("WR2", "WR", 2), ("SLOT", "WR", 8),
    ("TE", "TE", None), ("LT", "LT", None), ("LG", "LG", None), ("C", "C", None), ("RG", "RG", None),
    ("RT", "RT", None), ("FB", "FB", None),
]

# (unit, offense label, defense candidates in preference order) per scheme.
PAIRINGS = {
    "4-3": [
        ("PASS GAME", "WR1", ["RCB"]), ("PASS GAME", "WR2", ["LCB"]), ("PASS GAME", "SLOT", ["NB"]),
        ("PASS GAME", "TE", ["SS", "SLB"]),
        ("TRENCHES", "LT", ["RDE"]), ("TRENCHES", "LG", ["RDT"]), ("TRENCHES", "C", []),
        ("TRENCHES", "RG", ["LDT"]), ("TRENCHES", "RT", ["LDE"]),
        ("BACKFIELD", "RB", ["MLB"]), ("BACKFIELD", "QB", ["FS"]),
    ],
    "3-4": [
        ("PASS GAME", "WR1", ["RCB"]), ("PASS GAME", "WR2", ["LCB"]), ("PASS GAME", "SLOT", ["NB"]),
        ("PASS GAME", "TE", ["SS"]),
        # 3-4 OLBs are the edge rushers: weak side sits away from the TE,
        # usually over the left tackle.
        ("TRENCHES", "LT", ["WLB"]), ("TRENCHES", "LG", ["RDE"]), ("TRENCHES", "C", ["NT"]),
        ("TRENCHES", "RG", ["LDE"]), ("TRENCHES", "RT", ["SLB"]),
        ("BACKFIELD", "RB", ["LILB", "RILB"]), ("BACKFIELD", "QB", ["FS"]),
    ],
}


def _latest_depth_charts(season: int | None = None) -> pd.DataFrame:
    season = season or nd.current_season()

    def loader():
        d = nfl.load_depth_charts(seasons=[season]).to_pandas()
        return d[d["dt"] == d.groupby("team")["dt"].transform("max")].reset_index(drop=True)

    return nd._parquet_cache("depth_charts_latest", season, loader)


def _thumb(url) -> str | None:
    """nflverse headshots are full-size (~4MB) NFL Cloudinary images; ask the
    CDN for a face-cropped 96px thumbnail instead."""
    if not isinstance(url, str) or not url:
        return None
    return url.replace("/image/upload/", "/image/upload/c_thumb,g_face,w_96,h_96,", 1) if "/image/upload/" in url else url


def _team_depth(team: str) -> dict:
    """{'scheme', 'offense': {label: [players]}, 'defense': {pos_abb: [players]}}"""
    d = _latest_depth_charts()
    d = d[d["team"] == team]
    roster = nd.load_rosters()
    roster = roster[roster["team"] == team].drop_duplicates("gsis_id").set_index("gsis_id")

    def players(rows: pd.DataFrame) -> list[dict]:
        out = []
        for r in rows.sort_values("pos_rank").head(DEPTH_SHOWN).itertuples():
            ro = roster.loc[r.gsis_id] if r.gsis_id in roster.index else None
            out.append({
                "full_name": ro["full_name"] if ro is not None else r.player_name,
                "position": ro["position"] if ro is not None else r.pos_abb,
                "jersey_number": ro["jersey_number"] if ro is not None else None,
                "pfr_id": ro["pfr_id"] if ro is not None else None,
                "gsis_id": r.gsis_id,
                "status": ro["status"] if ro is not None else None,
                "headshot_url": _thumb(ro["headshot_url"]) if ro is not None else None,
            })
        return out

    off_rows = d[d["pos_grp"] == "3WR 1TE"]
    offense = {}
    for label, abb, slot in OFFENSE_SLOTS:
        rows = off_rows[(off_rows["pos_abb"] == abb) & ((off_rows["pos_slot"] == slot) if slot else True)]
        if not rows.empty:
            offense[label] = players(rows)

    def_grps = [g for g in d["pos_grp"].unique() if g.startswith("Base")]
    def_grp = def_grps[0] if def_grps else None
    scheme = "3-4" if def_grp and "3-4" in def_grp else "4-3"
    def_rows = d[d["pos_grp"] == def_grp] if def_grp else d.iloc[0:0]
    defense = {abb: players(rows) for abb, rows in def_rows.groupby("pos_abb")}

    # Madden + ECR/DPI for everyone shown.
    flat = [p for group in (*offense.values(), *defense.values()) for p in group]
    mr.attach_ratings(flat, team)
    prk.attach_rankings(flat, team)
    return {"scheme": scheme, "offense": offense, "defense": defense}


def _ovr(slot: list[dict] | None) -> int | None:
    if not slot:
        return None
    m = slot[0].get("madden")
    return m["ovr"] if m else None


def _avg(vals: list[int | None]) -> float | None:
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def matchup(off_team: str, def_team: str, off_depth: dict, def_depth: dict) -> dict:
    scheme = def_depth["scheme"]
    offense, defense = off_depth["offense"], def_depth["defense"]
    used = set()
    rows = []
    for unit, off_label, candidates in PAIRINGS[scheme]:
        def_label = next((c for c in candidates if c in defense and c not in used), None)
        if def_label:
            used.add(def_label)
        o, dd = offense.get(off_label), defense.get(def_label) if def_label else None
        oo, do = _ovr(o), _ovr(dd)
        rows.append({
            "unit": unit, "off_pos": off_label, "def_pos": def_label,
            "offense": o or [], "defense": dd or [],
            "edge": oo - do if oo is not None and do is not None else None,
        })
    # Defensive starters no offensive slot was paired with (e.g. the 4-3 WLB/SLB).
    extras = [{"def_pos": k, "defense": v} for k, v in defense.items() if k not in used]

    def unit_avg(unit, side):
        key = "offense" if side == "off" else "defense"
        return _avg([_ovr(r[key]) for r in rows if r["unit"] == unit and r[key]])

    summary = []
    for unit, off_name, def_name in (("PASS GAME", "Pass catchers", "Coverage"),
                                     ("TRENCHES", "O-line", "D-line / edge"),
                                     ("BACKFIELD", "QB / RB", "LB / FS")):
        a, b = unit_avg(unit, "off"), unit_avg(unit, "def")
        summary.append({"unit": unit, "off_label": off_name, "def_label": def_name,
                        "off_avg": a, "def_avg": b,
                        "edge": round(a - b, 1) if a is not None and b is not None else None})
    return {"offense_team": off_team, "defense_team": def_team, "scheme": scheme,
            "rows": rows, "extras": extras, "summary": summary}


def game_matchups(away: str, home: str) -> list[dict]:
    """away/home are nflverse codes. Returns [away OFF vs home DEF, home OFF vs away DEF]."""
    a, h = _team_depth(away), _team_depth(home)
    return [
        matchup(nd.nflverse_to_kalshi_team(away), nd.nflverse_to_kalshi_team(home), a, h),
        matchup(nd.nflverse_to_kalshi_team(home), nd.nflverse_to_kalshi_team(away), h, a),
    ]
