from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

import bet_ledger as bl
import game_review as gr
import nflverse_data as nd
import week_feed as wf
from test_engine_agent import fake_client, fake_price_legs, text, tool_use

import scenario_sim as ss

GID = "2026_10_NE_DET"


def _play(i, posteam, play_type, yards, run_location=None, desc="", **kw):
    d = {"game_id": GID, "posteam": posteam, "defteam": "NE" if posteam == "DET" else "DET",
         "play_type": play_type, "yards_gained": yards, "epa": yards / 10, "success": int(yards >= 4),
         "pass": int(play_type == "pass"), "rush": int(play_type == "run"), "pass_attempt": int(play_type == "pass"),
         "complete_pass": int(play_type == "pass" and yards > 0), "sack": 0, "interception": 0, "fumble_lost": 0,
         "qb_scramble": 0, "cpoe": 1.0, "air_yards": 8.0, "run_location": run_location, "down": 1, "ydstogo": 10,
         "wp": 0.5, "qtr": 1 + i // 4, "pass_oe": 0.0, "third_down_converted": 0, "third_down_failed": 0,
         "fixed_drive": 1 + i // 3, "fixed_drive_result": "Touchdown" if i % 3 == 2 else None, "yardline_100": 30 - i,
         "penalty": 0, "penalty_team": None, "passer_player_name": "J.Goff" if play_type == "pass" else None,
         "rusher_player_name": "J.Gibbs" if play_type == "run" else None,
         "receiver_player_name": "A.St. Brown" if play_type == "pass" else None, "passing_yards": yards,
         "rushing_yards": yards, "receiving_yards": yards, "pass_touchdown": 0, "rush_touchdown": 0, "touchdown": 0,
         "first_down": int(yards >= 10), "time": "10:00", "yrdln": "DET 30", "wpa": 0.01 * (i + 1),
         "desc": desc or f"play {i}", "total_home_score": 7 * (i // 4), "total_away_score": 3 * (i // 6)}
    d.update(kw)
    return d


PBP = pd.DataFrame([_play(0, "DET", "run", 2, "right"), _play(1, "DET", "run", 3, "right"),
                    _play(2, "DET", "pass", 25), _play(3, "DET", "run", 12, "left", desc="Gibbs left end"),
                    _play(4, "NE", "pass", 8), _play(5, "NE", "run", 4, "middle"), _play(6, "NE", "pass", -6, sack=1)])
SCHED = pd.DataFrame([{"game_id": GID, "season": 2026, "week": 10, "away_team": "NE", "home_team": "DET",
                       "away_score": 20, "home_score": 24, "result": 4, "spread_line": 2.5, "total_line": 51.5,
                       "gameday": "2026-11-15", "gametime": "09:30", "roof": "dome", "surface": "", "temp": None,
                       "wind": None}])


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(gr, "REVIEWS_PATH", str(tmp_path / "reviews.json"))
    monkeypatch.setattr(gr, "NOTES_PATH", str(tmp_path / "team_notes.json"))
    monkeypatch.setattr(gr, "AUDIT_DIR", str(tmp_path / "audits"))
    monkeypatch.setattr(wf, "FEED_PATH", str(tmp_path / "posts.json"))
    monkeypatch.setattr(wf, "AB_PATH", str(tmp_path / "ab.json"))
    monkeypatch.setattr(bl, "LEDGER_PATH", str(tmp_path / "ledger.json"))
    monkeypatch.setattr(nd, "load_pbp", lambda season=None: PBP)
    monkeypatch.setattr(nd, "load_schedules", lambda season=None: SCHED)
    monkeypatch.setattr(ss, "price_legs", fake_price_legs)


def test_facts_and_query_plays():
    f = gr.game_facts(PBP, SCHED.iloc[0])
    assert f["final"] == {"NE": 20, "DET": 24}
    assert f["closing_line"] == {"favorite": "DET", "favored_by": 2.5, "total": 51.5}
    det = f["offense"]["DET"]
    assert det["rushing"]["by_direction"]["right"]["plays"] == 2
    assert det["rushing"]["by_direction"]["right"]["ypp"] == 2.5
    assert det["rushing"]["explosive_10plus"] == 1 and det["passing"]["explosive_20plus"] == 1
    q = gr.query_plays(PBP, GID, {"offense": "DET", "play_type": "run", "quarter": 0, "down": 0,
                                  "run_location": "any", "player": "Gibbs", "min_yards": 10, "outcome": "any",
                                  "sort": "yards", "limit": 5})
    assert q["matching"] == 1 and q["plays"][0]["yards"] == 12


def test_review_checks_assumptions_and_rewrites_notebooks():
    post = {"post_id": "p1", "event_ticker": "KXNFLGAME-26NOV15NEDET", "away": "NE", "home": "DET", "week": 10,
            "model": "claude-opus-5-5", "headline": "h", "writeup": "w", "bets": [],
            "assumptions": [{"team": "DET", "claim": "DET under 4.0 YPC running right", "check": "run_location right"}]}
    wf._save({post["event_ticker"]: post})
    gr.save_audit("p1", {"thinking": ["NE's run defense is the edge."], "trace": ["price ..."]})
    publish = {"summary": "DET won.", "engine_review": "", "lessons": ["Thin team totals mislead."],
               "assumption_checks": [{"post_id": "p1", "index": 0, "verdict": "held", "evidence": "2.5 YPC on 2 runs"}],
               "reasoning_review": [{"post_id": "p1", "grade": "sound", "note": "ok"}],
               "team_notes": [{"team": "DET", "notes": "**Identity** run-first (W10)"},
                              {"team": "NE", "notes": "**Identity** (W10)"},
                              {"team": "BUF", "notes": "not in this game: ignored"}]}
    client, msgs = fake_client([
        SimpleNamespace(stop_reason="tool_use", content=[
            SimpleNamespace(type="thinking", thinking="Check the right-side runs."),
            tool_use("q1", "query_plays", {"offense": "DET", "play_type": "run", "quarter": 0, "down": 0,
                                           "run_location": "right", "player": "", "min_yards": -99, "outcome": "any",
                                           "sort": "chrono", "limit": 10})]),
        SimpleNamespace(stop_reason="tool_use", content=[text("Done."), tool_use("p", "publish_review", publish)]),
    ])
    r = gr.review_game(GID, client=client)
    sent = msgs.calls[0]["messages"][0]["content"][0]["text"]
    assert "DET under 4.0 YPC running right" in sent and "NE's run defense is the edge." in sent
    assert msgs.calls[0]["thinking"]["display"] == "summarized"
    assert '"matching": 2' in msgs.calls[1]["messages"][-1]["content"][0]["content"]
    assert r["thinking"] == ["Check the right-side runs."] and r["post_ids"] == ["p1"]
    notes = gr.all_notes()
    assert set(notes) == {"DET", "NE"} and notes["DET"]["week"] == 10
    assert gr.recent_lessons()[0]["lesson"] == "Thin team totals mislead."

    # a second review keeps the earlier notebook in history
    publish["team_notes"] = [{"team": "DET", "notes": "v2"}]
    client, _ = fake_client([SimpleNamespace(stop_reason="tool_use", content=[tool_use("p", "publish_review", publish)])])
    gr.review_game(GID, client=client)
    assert gr.all_notes()["DET"]["notes"] == "v2"
    assert gr.all_notes()["DET"]["history"][0]["notes"].startswith("**Identity**")

    # the feed attaches the verdicts to the post's assumptions
    games = [{"event_ticker": post["event_ticker"], "away": "NE", "home": "DET"}]
    wf._attach_reviews(10, games, [post])
    assert post["review"]["checks"][0]["verdict"] == "held"
    assert games[0]["review"]["summary"] == "DET won."


def test_writer_saves_audit_and_can_pass(monkeypatch):
    ctx = {"matchup": "NE @ DET", "event": "KXNFLGAME-26NOV15NEDET"}
    detail = {"away": "NE", "home": "DET"}
    monkeypatch.setattr(wf, "_schedule", lambda a, h: (10, "2026-11-15T14:30:00+00:00"))
    publish = {"headline": "h", "lean": "pass", "confidence": "low", "writeup": "w", "key_injuries": [],
               "key_matchups": [], "assumptions": [{"team": "DET", "claim": "c", "check": "k"}], "bets": [],
               "pass_reason": "The only edge is a $200 team total my read argues against."}
    client, msgs = fake_client([SimpleNamespace(stop_reason="tool_use", content=[
        SimpleNamespace(type="thinking", thinking="Engine edge conflicts with the matchup read."),
        tool_use("p", "publish_preview", publish)])])
    post = wf.run_writer("KXNFLGAME-26NOV15NEDET", detail, ctx, client=client)
    assert post["bets"] == [] and post["pass_reason"].startswith("The only edge")
    audit = gr.load_audit(post["post_id"])
    assert audit["thinking"] == ["Engine edge conflicts with the matchup read."]
    assert audit["context"]["matchup"] == "NE @ DET"
    assert bl.record(post, "ab") == 0      # no bets, nothing in the ledger
