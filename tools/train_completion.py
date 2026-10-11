"""
Train the Viz Lab's "if thrown now" completion model (completion_model.py).

Data: Big Data Bowl 2026 Analytics (Kaggle), unzipped under data/bdb/2026/.
One row per thrown pass: the targeted receiver at the release frame (the last
pre-throw frame), against the tracked coverage defenders. Label: complete (C)
vs incomplete / intercepted (I, IN). Sacks, scrambles and nullified plays are
dropped.

Holdout: train on 2023 Weeks 1-14, score Weeks 15-18, then refit on all weeks
and write data/models/completion_v1.json with the holdout metrics attached.

  .venv/bin/python tools/train_completion.py
"""

from __future__ import annotations

import glob
import json
import os
import sys

import numpy as np
import polars as pl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import completion_model as cm  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRAIN_DIR = glob.glob(os.path.join(ROOT, "data", "bdb", "2026", "*", "train"))
FPS = 10


def auc(y: np.ndarray, p: np.ndarray) -> float:
    order = np.argsort(p)
    ranks = np.empty(len(p)); ranks[order] = np.arange(1, len(p) + 1)
    pos = y == 1
    return float((ranks[pos].sum() - pos.sum() * (pos.sum() + 1) / 2) / (pos.sum() * (~pos).sum()))


def logloss(y, p):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean())


def build() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if not TRAIN_DIR:
        sys.exit("BDB 2026 data not found under data/bdb/2026/ -- download it with the kaggle CLI first.")
    d = TRAIN_DIR[0]
    supp = pl.read_csv(os.path.join(os.path.dirname(d), "supplementary_data.csv"), null_values="NA",
                       infer_schema_length=50000)
    supp = supp.filter(pl.col("pass_result").is_in(["C", "I", "IN"]) &
                       (pl.col("play_nullified_by_penalty") != "Y"))
    labels = {(g, p): (1 if r == "C" else 0, w) for g, p, r, w in
              supp.select("game_id", "play_id", "pass_result", "week").iter_rows()}

    t = (pl.scan_csv(os.path.join(d, "input_2023_w*.csv"), null_values="NA")
         .select("game_id", "play_id", "nfl_id", "frame_id", "play_direction", "absolute_yardline_number",
                 "player_role", "x", "y")
         .sort(["game_id", "play_id", "nfl_id", "frame_id"])
         .with_columns(vx=(pl.col("x").diff() * FPS).over(["game_id", "play_id", "nfl_id"]),
                       vy=(pl.col("y").diff() * FPS).over(["game_id", "play_id", "nfl_id"]))
         .with_columns(last=pl.col("frame_id").max().over(["game_id", "play_id"]))
         .filter(pl.col("frame_id") == pl.col("last"))
         .collect())

    X, y, wk = [], [], []
    for (g, p), grp in t.group_by(["game_id", "play_id"]):
        lab = labels.get((g, p))
        if lab is None:
            continue
        rec = grp.filter(pl.col("player_role") == "Targeted Receiver")
        qb = grp.filter(pl.col("player_role") == "Passer")
        de = grp.filter(pl.col("player_role") == "Defensive Coverage")
        if rec.height != 1 or qb.height != 1:
            continue
        direction = 1 if grp["play_direction"][0] == "right" else -1
        r = rec.row(0, named=True); q = qb.row(0, named=True)
        f = cm.features([r["x"]], [r["y"]], [r["vx"] or 0], [r["vy"] or 0], [q["x"]], [q["y"]],
                        de["x"].to_numpy()[:, None], de["y"].to_numpy()[:, None],
                        de["vx"].fill_null(0).to_numpy()[:, None], de["vy"].fill_null(0).to_numpy()[:, None],
                        float(grp["absolute_yardline_number"][0]), direction)
        X.append(f[0]); y.append(lab[0]); wk.append(lab[1])
    return np.array(X), np.array(y, float), np.array(wk)


def main() -> None:
    X, y, wk = build()
    tr, te = wk <= 14, wk >= 15
    m = cm.fit(X[tr], y[tr])
    p = cm.predict(m, X[te])
    base = np.full(te.sum(), y[tr].mean())
    bins = np.clip((p * 10).astype(int), 0, 9)
    calib = [{"bin": f"{b * 10}-{b * 10 + 10}%", "n": int((bins == b).sum()),
              "predicted": float(p[bins == b].mean()), "actual": float(y[te][bins == b].mean())}
             for b in range(10) if (bins == b).sum() >= 20]
    metrics = {"train_passes": int(tr.sum()), "holdout_passes": int(te.sum()),
               "holdout_weeks": "2023 Weeks 15-18", "auc": auc(y[te], p), "log_loss": logloss(y[te], p),
               "baseline_log_loss": logloss(y[te], base), "completion_rate": float(y.mean()),
               "calibration": calib}
    final = cm.fit(X, y)
    final["metrics"] = metrics
    final["source"] = "NFL Big Data Bowl 2026 Analytics (Kaggle), 2023 season pass plays"
    os.makedirs(os.path.dirname(cm.MODEL_PATH), exist_ok=True)
    with open(cm.MODEL_PATH, "w") as f:
        json.dump(final, f, indent=1)
    print(json.dumps(metrics, indent=1))
    for name, c in zip(cm.FEATURES, final["coef"]):
        print(f"  {name:16s} {c:+.3f}")


if __name__ == "__main__":
    main()
