"""
One-time prep for the Viz Lab's 2021 All-22 replays (Big Data Bowl 2023 data
in data/bdb/2023/):

  1. split each weekly tracking CSV into per-game parquet under cache/bdb/2021/
     (the server would do this lazily, but a cold week takes a few seconds);
  2. score the pocket-pressure index against PFF charting: for every dropback,
     the index's peak between the snap and the throw/sack vs. whether PFF
     credited any defender with a hit, hurry or sack. Written to
     cache/bdb/2021/pressure_validation.json and shown under the pressure chart.

  .venv/bin/python tools/bdb_precompute.py
"""

from __future__ import annotations

import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bdb_tracking as bdb  # noqa: E402
from train_completion import auc  # noqa: E402


def main() -> None:
    if not bdb._has_2021():
        sys.exit("BDB 2023 data not found under data/bdb/2023/")
    peaks, labels = [], []
    for g in bdb.games("2021"):
        bdb.tracking("2021", g["gameId"])
        for p in bdb.play_list("2021", g["gameId"])["plays"]:
            try:
                pr, game, off_side, n, ents, events, snap_f, pff = bdb._play_core("2021", g["gameId"], p["playId"])
            except ValueError:
                continue
            qb = next((e for e in ents if pff.get(e["id"], {}).get("role") == "Pass"), None)
            rushers = [e for e in ents if pff.get(e["id"], {}).get("role") == "Pass Rush"]
            if qb is None or not rushers:
                continue
            end_f = next((e["f"] for e in events if e["name"] in ("pass_forward", "qb_sack", "run", "pass_shovel")), n)
            idx = bdb.pressure_index(qb, rushers, snap_f)[snap_f - 1:end_f]
            if not len(idx) or np.all(np.isnan(idx)):
                continue
            peaks.append(float(np.nanmax(idx)))
            labels.append(int(any(v["events"] for v in pff.values())))
        print(g["gameId"], len(peaks), flush=True)
    peaks, labels = np.array(peaks), np.array(labels)
    edges = np.quantile(peaks, [0, .2, .4, .6, .8, 1])
    buckets = []
    for i in range(5):
        m = (peaks >= edges[i]) & ((peaks < edges[i + 1]) if i < 4 else (peaks <= edges[i + 1]))
        buckets.append({"from": round(float(edges[i]), 2), "to": round(float(edges[i + 1]), 2), "n": int(m.sum()),
                        "pressure_rate": float(labels[m].mean())})
    out = {"dropbacks": int(len(peaks)), "pff_pressure_rate": float(labels.mean()),
           "auc": auc(labels, peaks), "mean_peak_pressured": float(peaks[labels == 1].mean()),
           "mean_peak_clean": float(peaks[labels == 0].mean()), "quintiles": buckets,
           "note": "Peak pressure index (snap to throw/sack, rushers only) vs PFF hit/hurry/sack, 2021 Wk 1-8."}
    os.makedirs(os.path.dirname(bdb.VALIDATION_PATH), exist_ok=True)
    with open(bdb.VALIDATION_PATH, "w") as f:
        json.dump(out, f, indent=1)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
