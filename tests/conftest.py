"""Shared fixtures: synthetic Kalshi markets, no network."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fair_value as fv  # noqa: E402

SUFFIX = "26OCT04SEAARI"
AWAY, HOME = "SEA", "ARI"


@pytest.fixture(autouse=True)
def flat_key_weights(monkeypatch):
    """Hermetic: don't read nflverse schedules; use the built-in fallback shape."""
    def boom():
        raise RuntimeError("no history in tests")
    monkeypatch.setattr(fv, "_key_weights_from_history", boom)


def raw_market(series: str, last: str, strike: float | None, bid: float, ask: float, volume: float = 50_000,
               player: str | None = None) -> dict:
    m = {
        "ticker": f"{series}-{SUFFIX}-{last}",
        "event_ticker": f"{series}-{SUFFIX}",
        "floor_strike": strike,
        "yes_bid_dollars": f"{bid:.4f}",
        "yes_ask_dollars": f"{ask:.4f}",
        "volume_fp": str(volume),
        "status": "active",
        "title": last,
    }
    if player:
        m["custom_strike"] = {"football_player": player}
    return m


def synthetic_game(mu_home: float = -4.0, sd: float = 12.7, total: float = 45.0, half_spread: float = 0.01,
                   overrides: dict | None = None) -> list[dict]:
    """A full ML + spread + total ladder priced off a known model. SEA (away)
    is favored by -mu_home. `overrides` maps title -> (bid, ask)."""
    model = fv.GameModel(away=AWAY, home=HOME, mu_m=mu_home, sd_m=sd, mu_t=total, sd_t=13.1,
                         pmf_m=fv.margin_pmf(mu_home, sd)[0], pmf_t=fv.total_pmf(total, 13.1)[0])
    raws = []
    for team in (AWAY, HOME):
        raws.append(raw_market("KXNFLGAME", team, None, 0, 0))
        for x in (1.5, 2.5, 3.5, 4.5, 6.5, 7.5, 9.5, 10.5, 13.5):
            raws.append(raw_market("KXNFLSPREAD", f"{team}{int(x + 0.5)}", x, 0, 0))
    for x in (37.5, 40.5, 43.5, 44.5, 45.5, 47.5, 50.5):
        raws.append(raw_market("KXNFLTOTAL", str(int(x + 0.5)), x, 0, 0))
    for team in (AWAY, HOME):
        for x in (17.5, 20.5, 24.5):
            raws.append(raw_market("KXNFLTEAMTOTAL", f"{team}{int(x + 0.5)}", x, 0, 0))
    out = []
    for r in raws:
        m = fv.normalize_market(r, teams=(AWAY, HOME))
        p = model.fair(m)
        bid, ask = max(0.01, round(p - half_spread, 2)), min(0.99, round(p + half_spread, 2))
        if ask <= bid:
            ask = bid + 0.01
        if overrides and m["title"] in overrides:
            bid, ask = overrides[m["title"]]
        m["yes_bid"], m["yes_ask"] = bid, ask
        out.append(m)
    return out
