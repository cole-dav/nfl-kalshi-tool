"""
"If thrown now" completion probability for a receiver, from tracking geometry.

Trained by tools/train_completion.py on the Big Data Bowl 2026 Analytics data
(2023 season pass plays): at the release frame, the targeted receiver's
geometry vs. the coverage defenders, labelled complete / incomplete. It's a
regularized logistic regression on a dozen hand-built features -- small and
inspectable, in the spirit of Palepu's 2026 finalist (which used a GAT+BiLSTM).

Applied in the Viz Lab replays to every eligible receiver on every frame
between the snap and the throw, it reads as "if the QB threw to him right
now". Caveats, shown in the UI:
  * it learned from passes that were actually thrown to that receiver
    (selection bias: QBs throw to open men);
  * the 2026 data tracks only coverage defenders, so callers pass coverage
    defenders only (pass rushers excluded) to keep features comparable.

Viz Lab only -- historical seasons, never used for betting pages.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache

import numpy as np

MODEL_PATH = os.path.join(os.path.dirname(__file__), "data", "models", "completion_v1.json")
FIELD_W = 53.3
FEATURES = ["log_sep1", "log_sep2", "n_def_5", "depth", "depth_sq", "qb_dist", "near_sideline",
            "sideline", "rec_speed", "rec_downfield_v", "closing", "behind_los"]


def features(rx, ry, rvx, rvy, qx, qy, dx, dy, dvx, dvy, los: float, direction: int) -> np.ndarray:
    """Feature matrix (frames, len(FEATURES)) for one receiver.

    rx..rvy, qx, qy: (n,) receiver / QB per frame. dx..dvy: (k, n) coverage
    defenders. NaN-tolerant: frames with no defender get the far-away defaults.
    """
    rx, ry, rvx, rvy, qx, qy = (np.asarray(a, float) for a in (rx, ry, rvx, rvy, qx, qy))
    n = rx.shape[0]
    dx = np.asarray(dx, float).reshape(-1, n)
    dy = np.asarray(dy, float).reshape(-1, n)
    dvx = np.asarray(dvx, float).reshape(-1, n)
    dvy = np.asarray(dvy, float).reshape(-1, n)
    if dx.shape[0]:
        d = np.hypot(dx - rx, dy - ry)
        d = np.where(np.isnan(d), np.inf, d)
        order = np.sort(d, axis=0)
        sep1 = order[0]
        sep2 = order[1] if d.shape[0] > 1 else np.full(n, 20.0)
        n5 = (d < 5).sum(axis=0).astype(float)
        idx = np.argmin(d, axis=0)
        cols = np.arange(n)
        ndx, ndy = dx[idx, cols], dy[idx, cols]
        nvx, nvy = dvx[idx, cols], dvy[idx, cols]
        with np.errstate(invalid="ignore", divide="ignore"):
            ux, uy = (rx - ndx) / sep1, (ry - ndy) / sep1
        closing = np.nan_to_num(nvx * ux + nvy * uy)
    else:
        sep1 = sep2 = np.full(n, 20.0)
        n5 = np.zeros(n)
        closing = np.zeros(n)
    sep1 = np.clip(np.nan_to_num(sep1, posinf=20.0), 0, 20)
    sep2 = np.clip(np.nan_to_num(sep2, posinf=20.0), 0, 20)
    depth = np.clip((rx - los) * direction, -10, 60)
    side = np.clip(np.minimum(ry, FIELD_W - ry), 0, FIELD_W / 2)
    X = np.column_stack([
        np.log1p(sep1), np.log1p(sep2), n5, depth, depth ** 2 / 100.0,
        np.clip(np.hypot(rx - qx, ry - qy), 0, 70), (side < 2).astype(float), np.minimum(side, 10),
        np.clip(np.hypot(rvx, rvy), 0, 12), np.clip(rvx * direction, -12, 12), np.clip(closing, -12, 12),
        (depth < 0).astype(float),
    ])
    return np.nan_to_num(X)


def fit(X: np.ndarray, y: np.ndarray, l2: float = 1.0, iters: int = 50) -> dict:
    """L2-regularized logistic regression by Newton's method on standardized X."""
    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd[sd == 0] = 1.0
    Z = np.column_stack([np.ones(len(X)), (X - mu) / sd])
    w = np.zeros(Z.shape[1])
    reg = np.full(Z.shape[1], l2); reg[0] = 0.0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-Z @ w))
        g = Z.T @ (p - y) + reg * w
        H = (Z * (p * (1 - p))[:, None]).T @ Z + np.diag(reg)
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-8:
            break
    return {"features": FEATURES, "mean": mu.tolist(), "std": sd.tolist(), "intercept": float(w[0]),
            "coef": w[1:].tolist()}


def predict(model: dict, X: np.ndarray) -> np.ndarray:
    z = model["intercept"] + ((X - np.array(model["mean"])) / np.array(model["std"])) @ np.array(model["coef"])
    return 1 / (1 + np.exp(-z))


@lru_cache(maxsize=1)
def load() -> dict | None:
    if not os.path.exists(MODEL_PATH):
        return None
    with open(MODEL_PATH) as f:
        return json.load(f)
