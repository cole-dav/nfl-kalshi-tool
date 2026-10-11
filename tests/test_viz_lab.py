from __future__ import annotations

import bdb_tracking as bdb
import viz_lab as lab


def test_focus_from_play_text():
    assert bdb._focus_name("(Shotgun) A.Smith pass deep right to T.Hill for 75 yards, TOUCHDOWN.") == "T.Hill"
    assert bdb._focus_name("T.Brady pass incomplete short right to R.Gronkowski.") == "R.Gronkowski"
    assert bdb._focus_name("(No Huddle, Shotgun) J.White up the middle to NE 46 for 3 yards (E.Berry).") == "J.White"
    assert bdb._focus_name("(Shotgun) Direct snap to T.Kelce.  T.Kelce up the middle to KC 23 for 4 yards") == "T.Kelce"
    assert bdb._focus_name("(Shotgun) A.Smith sacked at KC 46 for -3 yards (T.Flowers).") is None


def test_abbreviated_name_match():
    assert bdb._abbr_match("T.Kelce", "Travis Kelce")
    assert bdb._abbr_match("Ty.Montgomery", "Ty Montgomery")
    assert not bdb._abbr_match("T.Kelce", "Jason Kelce")
    assert not bdb._abbr_match("Kelce", "Travis Kelce")


def test_personnel_grouping():
    assert lab._personnel("1 C, 2 G, 1 QB, 1 RB, 2 T, 1 TE, 3 WR") == "11"
    assert lab._personnel("1 C, 1 FB, 2 G, 1 QB, 1 RB, 2 T, 1 TE, 2 WR") == "21"
    assert lab._personnel("") is None


def test_completion_features_without_defenders():
    import numpy as np
    import completion_model as cm
    X = cm.features([50, 51], [20, 20], [0, 1], [0, 0], [40, 40], [26, 26],
                    np.zeros((0, 2)), np.zeros((0, 2)), np.zeros((0, 2)), np.zeros((0, 2)), 45.0, 1)
    assert X.shape == (2, len(cm.FEATURES))
    assert np.isfinite(X).all()
    assert X[0, cm.FEATURES.index("depth")] == 5


def test_completion_fit_learns_separation():
    import numpy as np
    import completion_model as cm
    rng = np.random.default_rng(0)
    X = rng.normal(size=(2000, len(cm.FEATURES)))
    y = (rng.random(2000) < 1 / (1 + np.exp(-2 * X[:, 0]))).astype(float)
    m = cm.fit(X, y)
    assert m["coef"][0] > 1.0
    assert cm.predict(m, X).shape == (2000,)
